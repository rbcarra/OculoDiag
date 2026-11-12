#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OculoDiag -- registro diagnóstico de motilidade ocular com duas câmeras IR
near-eye (HBVCAM-1914S V44 / sensor GC0308) e uma câmera extra opcional.

Quatro telas (F1 / F2 / F3 / F4):
  1. Câmeras     qual câmera é de cada olho (e a extra, opcional), rotação,
                 espelhamento e recorte (ROI) de cada olho (salvo em
                 ~/.oculodiag.json e reaplicado na próxima vez)
  2. Motilidade  8 direções + longe (PPO) + perto; ESPAÇO registra os dois
                 olhos; ao final salva a página de resultado (PNG, PDF e HTML)
  3. Gravação    gravar / pausar / continuar / salvar / descartar, com FPS
                 alvo; salva um vídeo por câmera, OD|OE lado a lado,
                 OD|OE|extra lado a lado e os tempos de cada quadro;
                 opcionalmente mede a pupila em cada quadro
  4. Pupila      mede a pupila de cada olho (relativa à íris) durante a
                 avaliação e guarda o maior (menos luz) e o menor (mais luz)
                 diâmetro de cada olho, com a foto de cada um

Toda imagem é mostrada e salva na VISTA DO EXAMINADOR (de frente para o
paciente): OD à esquerda, OE à direita.

Uso:
  python3 oculo_diag.py                  câmeras reais
  python3 oculo_diag.py --simular        olhos sintéticos, sem hardware
  python3 oculo_diag.py --saida ~/Exames
  python3 oculo_diag.py --cam-backend v4l2|gstreamer|dshow|msmf|any

Dependências: opencv-python, numpy, pillow, tkinter
  Raspberry Pi OS:  sudo apt install python3-opencv python3-pil python3-pil.imagetk python3-tk
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox
    from PIL import ImageTk
except ImportError:  # permite usar/testar o núcleo sem interface
    tk = None

VERSION = "1.2"
CONFIG_PATH = Path.home() / ".oculodiag.json"

EYES = ("OD", "OE")
EXTRA = "EXT"                  # câmera extra opcional (rosto, cena...), só gravada
ROLES = EYES + (EXTRA,)
EYE_NAMES = {"OD": "olho direito", "OE": "olho esquerdo", EXTRA: "opcional"}
ROLE_TITLES = {"OD": "OD", "OE": "OE", EXTRA: "Extra"}

# Modos de captura. "fps" é o que se PEDE ao driver (ficha técnica); o que
# a câmera entrega de verdade é medido e mostrado ao vivo. Em bancada:
# 320x240 -> ~123 fps, 640x480 -> ~29 fps.
# "janela" = parte do sensor que o modo entrega (x, y, w, h em frações do
# sensor inteiro). O GC0308 em 320×240 NÃO reduz a imagem: entrega o CENTRO
# do sensor na mesma escala (medido nas duas câmeras: deslocamento exato
# de 160, 120 px). Por ser centrada, a janela é a mesma com giro/espelho.
MODES = {
    "rapido":  {"w": 320, "h": 240, "fps": 128, "rotulo": "320×240 · rápido", "janela": (0.25, 0.25, 0.5, 0.5)},
    "detalhe": {"w": 640, "h": 480, "fps": 30,  "rotulo": "640×480 · detalhe", "janela": (0.0, 0.0, 1.0, 1.0)},
}
MOTILITY_MODE = "detalhe"   # fotos estáticas: resolução vale mais que FPS
PUPIL_MODE = "detalhe"      # reflexo pupilar é lento: resolução vale mais que FPS
EXTRA_MODE = "detalhe"      # câmera extra: modo que qualquer webcam UVC aceita

# As 10 posições do exame. "rc" = linha/coluna na página de resultado, na
# VISTA DO EXAMINADOR: o lado DIREITO do paciente fica à ESQUERDA da página.
# "vec" = para onde a pupila vai na tela (x: +direita da tela, y: +baixo).
POSITIONS = [
    {"key": "longe", "nome": "Longe · PPO", "descr": "posição primária, olhar distante",
     "comando": "Olhe para um ponto distante, bem à sua frente", "rc": (1, 1), "vec": (0, 0)},
    {"key": "supra", "nome": "Supraversão", "descr": "para cima",
     "comando": "Olhe para cima", "rc": (0, 1), "vec": (0, -1)},
    {"key": "supradextro", "nome": "Supradextroversão", "descr": "para cima e à direita do paciente",
     "comando": "Olhe para cima e para a sua direita", "rc": (0, 0), "vec": (-1, -1)},
    {"key": "dextro", "nome": "Dextroversão", "descr": "à direita do paciente",
     "comando": "Olhe para a sua direita", "rc": (1, 0), "vec": (-1, 0)},
    {"key": "infradextro", "nome": "Infradextroversão", "descr": "para baixo e à direita do paciente",
     "comando": "Olhe para baixo e para a sua direita", "rc": (2, 0), "vec": (-1, 1)},
    {"key": "infra", "nome": "Infraversão", "descr": "para baixo",
     "comando": "Olhe para baixo", "rc": (2, 1), "vec": (0, 1)},
    {"key": "infralevo", "nome": "Infralevoversão", "descr": "para baixo e à esquerda do paciente",
     "comando": "Olhe para baixo e para a sua esquerda", "rc": (2, 2), "vec": (1, 1)},
    {"key": "levo", "nome": "Levoversão", "descr": "à esquerda do paciente",
     "comando": "Olhe para a sua esquerda", "rc": (1, 2), "vec": (1, 0)},
    {"key": "supralevo", "nome": "Supralevoversão", "descr": "para cima e à esquerda do paciente",
     "comando": "Olhe para cima e para a sua esquerda", "rc": (0, 2), "vec": (1, -1)},
    {"key": "perto", "nome": "Perto · convergência", "descr": "alvo a ~30 cm, na linha média",
     "comando": "Olhe para a ponta do meu dedo", "rc": (3, 1), "vec": None},
]


# ════════════════════════════════════════════════════════════════════════
#  Configuração
# ════════════════════════════════════════════════════════════════════════

def default_config(sim: bool = False) -> dict:
    rot = 0 if sim else 90
    return {
        "cameras": {e: {"id": None, "index": None, "rotation": 0 if e == EXTRA else rot, "flip": False,
                        "roi": None} for e in ROLES},
        "saida": str(Path.home() / "OculoDiag"),
        "gravacao": {"modo": "rapido", "fps": 123, "pupila": False},
    }


def load_config(path: Path, sim: bool = False) -> dict:
    cfg = default_config(sim)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for e in ROLES:
            cfg["cameras"][e].update(data.get("cameras", {}).get(e, {}))
        cfg["saida"] = data.get("saida", cfg["saida"])
        cfg["gravacao"].update(data.get("gravacao", {}))
    except FileNotFoundError:
        pass
    except Exception as ex:  # arquivo corrompido: começa do padrão
        print(f"[config] ignorando {path}: {ex}")
    for e in ROLES:
        cfg["cameras"][e]["roi"] = clean_roi(cfg["cameras"][e].get("roi"))
    cfg["cameras"][EXTRA]["roi"] = None
    if cfg["gravacao"].get("modo") not in MODES:
        cfg["gravacao"]["modo"] = "rapido"
    return cfg


# ── recorte (ROI) ──────────────────────────────────────────────────────
# O recorte é guardado em FRAÇÕES DO SENSOR INTEIRO já orientado (x, y, w, h
# em 0..1) -- o quadro de 640×480. Cada modo vê só a sua janela do sensor
# (MODES[...]["janela"]): roi_to_frame/roi_from_frame convertem. Assim o
# mesmo recorte cai sobre o mesmo pedaço do olho nos dois modos; em 320×240
# a parte que fica fora da janela é cortada. Recortes de versões antigas
# foram desenhados na tela Câmeras em 640×480, onde quadro = sensor.
ROI_MIN = 0.03   # menor lado aceito, em fração (≈ 19 px do sensor)


def clean_roi(r):
    try:
        x, y, w, h = (float(v) for v in r)
    except (TypeError, ValueError):
        return None
    x, y = min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)
    w, h = min(w, 1.0 - x), min(h, 1.0 - y)
    if w < ROI_MIN or h < ROI_MIN or (w > 0.999 and h > 0.999):
        return None
    return [round(x, 5), round(y, 5), round(w, 5), round(h, 5)]


def roi_rotate(r, deg: int):
    """Leva o recorte junto quando a imagem gira (múltiplos de 90°)."""
    if r is None:
        return None
    x, y, w, h = r
    for _ in range((deg // 90) % 4):          # 90° horário por vez
        x, y, w, h = 1.0 - (y + h), x, h, w
    return clean_roi([x, y, w, h])


def roi_flip(r):
    if r is None:
        return None
    x, y, w, h = r
    return clean_roi([1.0 - (x + w), y, w, h])


def roi_to_frame(r, mode_key: str | None):
    """Recorte no sensor -> recorte no quadro deste modo (None = inteiro)."""
    if r is None or mode_key not in MODES:
        return r
    wx, wy, ww, wh = MODES[mode_key]["janela"]
    x0, y0 = max(0.0, (r[0] - wx) / ww), max(0.0, (r[1] - wy) / wh)
    x1, y1 = min(1.0, (r[0] + r[2] - wx) / ww), min(1.0, (r[1] + r[3] - wy) / wh)
    if x1 - x0 < ROI_MIN or y1 - y0 < ROI_MIN:      # recorte fora da janela: quadro inteiro
        return None
    if x1 - x0 > 0.999 and y1 - y0 > 0.999:
        return None
    return [x0, y0, x1 - x0, y1 - y0]


def roi_from_frame(r, mode_key: str | None):
    """Recorte desenhado no quadro deste modo -> recorte no sensor."""
    if r is None or mode_key not in MODES:
        return clean_roi(r)
    wx, wy, ww, wh = MODES[mode_key]["janela"]
    return clean_roi([wx + r[0] * ww, wy + r[1] * wh, r[2] * ww, r[3] * wh])


def roi_pixels(r, W: int, H: int):
    """(x0, y0, x1, y1) em pixels, ou None para quadro inteiro."""
    if r is None:
        return None
    x0, y0 = int(round(r[0] * W)), int(round(r[1] * H))
    x1, y1 = int(round((r[0] + r[2]) * W)), int(round((r[1] + r[3]) * H))
    x0, y0 = min(max(x0, 0), W - 16), min(max(y0, 0), H - 16)
    x1, y1 = min(max(x1, x0 + 16), W), min(max(y1, y0 + 16), H)
    return x0, y0, x1, y1


def roi_text(r, W: int, H: int) -> str:
    px = roi_pixels(r, W, H)
    if px is None:
        return f"quadro inteiro {W}×{H}"
    return f"recorte {px[2] - px[0]}×{px[3] - px[1]} de {W}×{H}"


def save_config(path: Path, cfg: dict) -> None:
    try:
        path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as ex:
        print(f"[config] não foi possível salvar {path}: {ex}")


def safe_name(text: str) -> str:
    s = re.sub(r"[^\w\-]+", "_", (text or "").strip()).strip("_")
    return s[:40] or "sem_id"


def open_folder(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as ex:
        print(f"[abrir pasta] {ex}")


# ════════════════════════════════════════════════════════════════════════
#  Câmeras
# ════════════════════════════════════════════════════════════════════════

BACKENDS = {
    "v4l2": "CAP_V4L2", "gstreamer": "CAP_GSTREAMER", "dshow": "CAP_DSHOW",
    "msmf": "CAP_MSMF", "avfoundation": "CAP_AVFOUNDATION", "ffmpeg": "CAP_FFMPEG",
    "any": "CAP_ANY",
}


def backend_api(name: str = "auto") -> int:
    """V4L2 no Linux (o GStreamer do Raspberry Pi OS ignora os ajustes e
    derruba o streaming), DirectShow no Windows, AVFoundation no macOS."""
    if name != "auto":
        return getattr(cv2, BACKENDS[name])
    if sys.platform.startswith("linux"):
        return cv2.CAP_V4L2
    if sys.platform.startswith("win"):
        return cv2.CAP_DSHOW
    if sys.platform == "darwin":
        return cv2.CAP_AVFOUNDATION
    return cv2.CAP_ANY


def _read_text(p: str) -> str:
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def list_cameras(backend: str = "auto", in_use: tuple = ()) -> list[dict]:
    """Lista as câmeras. No Linux usa o sysfs (não abre nada) e identifica
    cada câmera pela PORTA USB (/dev/v4l/by-path), que é estável mesmo com
    duas câmeras idênticas -- o número /dev/videoN pode mudar entre boots."""
    if sys.platform.startswith("linux") and os.path.isdir("/sys/class/video4linux"):
        by_path: dict[str, str] = {}
        for link in sorted(glob.glob("/dev/v4l/by-path/*")):
            by_path.setdefault(os.path.realpath(link), os.path.basename(link))
        usb, other = [], []
        for sysdir in glob.glob("/sys/class/video4linux/video*"):
            m = re.search(r"video(\d+)$", sysdir)
            if not m:
                continue
            n = int(m.group(1))
            if _read_text(sysdir + "/index") not in ("", "0"):
                continue  # nó de metadados da mesma câmera UVC
            name = _read_text(sysdir + "/name") or f"video{n}"
            pid = by_path.get(f"/dev/video{n}")
            port = re.search(r"usb-\d+:([\d.]+):", pid or "")
            cam = {"index": n, "id": pid or f"video{n}",
                   "label": f"#{n} · {name}" + (f" · USB {port.group(1)}" if port else "")}
            # O Pi 5 expõe vários /dev/video internos (pispbe, hevc...):
            # só interessam as câmeras USB.
            (usb if "usb" in os.path.realpath(sysdir + "/device") else other).append(cam)
        found = usb or other
        return sorted(found, key=lambda c: c["index"])

    api = backend_api(backend)
    found = []
    for i in range(6):
        if i in in_use:
            found.append({"index": i, "id": f"idx{i}", "label": f"#{i} · câmera"})
            continue
        cap = cv2.VideoCapture(i, api)
        ok = cap.isOpened()
        cap.release()
        if ok:
            found.append({"index": i, "id": f"idx{i}", "label": f"#{i} · câmera"})
    return found


@dataclass
class Frame:
    img: np.ndarray      # já rotacionada/espelhada (vista do examinador)
    t: float             # time.monotonic() na chegada do quadro
    t_drv: float         # timestamp do driver V4L2 em s (nan se indisponível)
    n: int               # contador de quadros da câmera
    full: np.ndarray | None = None   # quadro inteiro antes do recorte (só para a tela de recorte)


class SimCapture:
    """Imita cv2.VideoCapture com um olho sintético (modo --simular).
    `target` é ajustado pela tela de motilidade; None = sacadas aleatórias.
    O movimento é conjugado: as duas câmeras simuladas seguem o mesmo alvo.
    A pupila dilata e contrai devagar (para testar a tela Pupila).
    `scene=True` (câmera extra) desenha um rosto em vez de um olho."""
    target = None
    _goal = (0.0, 0.0)
    _next_sacc = 0.0

    def __init__(self, index: int, w: int, h: int, fps: float, scene: bool = False):
        self.index, self.w, self.h, self.fps, self.scene = index, w, h, fps, scene
        self.t_next = time.monotonic()
        self.t_last = self.t_next
        self.gx = self.gy = 0.0
        rng = np.random.default_rng(index)
        self.noise = [rng.integers(0, 14, (h, w, 3), dtype=np.uint8) for _ in range(5)]
        self.k = 0
        # olho: mesmo tamanho em pixels nos dois modos, como o GC0308 (que em
        # 320×240 entrega o centro do sensor); o rosto da extra escala
        s = h / 240 if scene else 1.0
        self.s = s
        self.skin = np.full((h, w, 3), 62, np.uint8)
        mask = np.zeros((h, w), np.uint8)
        cv2.ellipse(mask, (w // 2, h // 2), (int(125 * s), int(68 * s)), 0, 0, 360, 255, -1, cv2.LINE_AA)
        self.mask = cv2.merge([mask] * 3).astype(np.float32) / 255.0
        self.rng = rng

    def isOpened(self):
        return True

    def set(self, *a):
        return True

    def get(self, prop):
        return 0.0

    def release(self):
        pass

    def read(self):
        now = time.monotonic()
        if self.t_next > now:
            time.sleep(self.t_next - now)
        now = time.monotonic()
        self.t_next = max(self.t_next + 1.0 / self.fps, now - 0.05)
        dt, self.t_last = now - self.t_last, now

        if SimCapture.target is None:
            if now >= SimCapture._next_sacc:
                SimCapture._goal = (float(self.rng.uniform(-0.9, 0.9)), float(self.rng.uniform(-0.6, 0.6)))
                SimCapture._next_sacc = now + float(self.rng.uniform(0.5, 1.2))
            goal = SimCapture._goal
        else:
            goal = SimCapture.target
        a = 1.0 - math.exp(-dt / 0.015)
        self.gx += (goal[0] - self.gx) * a
        self.gy += (goal[1] - self.gy) * a

        s, w, h = self.s, self.w, self.h
        if self.scene:
            img = self._face(now)
        else:
            eye = np.full((h, w, 3), 200, np.uint8)
            ix, iy = int(w / 2 + self.gx * 60 * s), int(h / 2 + self.gy * 34 * s)
            pr = 15 * s * (1 + 0.45 * math.sin(2 * math.pi * now / 7.0))
            cv2.circle(eye, (ix, iy), int(54 * s), (92, 92, 92), -1, cv2.LINE_AA)
            cv2.circle(eye, (ix, iy), int(pr), (28, 28, 28), -1, cv2.LINE_AA)
            cv2.circle(eye, (ix + int(9 * s), iy - int(9 * s)), max(2, int(4 * s)), (250, 250, 250), -1, cv2.LINE_AA)
            img = (eye * self.mask + self.skin * (1 - self.mask)).astype(np.uint8)
        img = cv2.add(img, self.noise[self.k % len(self.noise)])
        self.k += 1
        cv2.putText(img, f"SIM #{self.index}", (int(8 * s), int(22 * s)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5 * s, (170, 170, 170), 1, cv2.LINE_AA)
        return True, img

    def _face(self, now):
        s, w, h = self.s, self.w, self.h
        img = np.full((h, w, 3), 48, np.uint8)
        cx, cy = w // 2 + int(10 * s * math.sin(now / 3)), h // 2
        cv2.ellipse(img, (cx, cy), (int(80 * s), int(105 * s)), 0, 0, 360, (150, 150, 150), -1, cv2.LINE_AA)
        for sx in (-1, 1):
            ex, ey = cx + sx * int(34 * s), cy - int(18 * s)
            cv2.ellipse(img, (ex, ey), (int(18 * s), int(9 * s)), 0, 0, 360, (215, 215, 215), -1, cv2.LINE_AA)
            cv2.circle(img, (ex + int(self.gx * 9 * s), ey + int(self.gy * 4 * s)), int(6 * s), (40, 40, 40), -1)
        cv2.ellipse(img, (cx, cy + int(48 * s)), (int(24 * s), int(8 * s)), 0, 0, 180, (90, 90, 90), 2, cv2.LINE_AA)
        return img


class Camera:
    """Uma câmera em thread própria. Cada quadro recebe o instante de
    chegada e é entregue a) como `latest` para a interface e b) aos
    `listeners` (o gravador), sem perder quadros."""

    def __init__(self, eye: str, backend: str = "auto", sim: bool = False):
        self.eye = eye
        self.backend = backend
        self.sim = sim
        self.cap = None
        self.thread = None
        self.running = False
        self.lock = threading.Lock()
        self.latest: Frame | None = None
        self.listeners: list = []
        self.rotation = 0
        self.flip = False
        self.roi = None          # [x, y, w, h] em frações do sensor, ou None
        self.index: int | None = None
        self.mode_key: str | None = None
        self.status = "fechada"
        self.note = ""
        self.n = 0
        self._times: deque = deque(maxlen=300)

    # -- abrir / fechar ---------------------------------------------------
    def _open_cap(self, index: int, m: dict):
        if self.sim:
            return SimCapture(index, m["w"], m["h"], m["fps"], scene=self.eye == EXTRA), ""
        api = backend_api(self.backend)
        # 1a tentativa pede o FPS; se o driver engasgar, reabre sem pedir.
        for set_fps in (True, False):
            cap = cv2.VideoCapture(index, api)
            if not cap.isOpened():
                cap.release()
                return None, "não abriu"
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, m["w"])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, m["h"])
            if set_fps:
                cap.set(cv2.CAP_PROP_FPS, m["fps"])
            t0 = time.monotonic()
            while time.monotonic() - t0 < 3.0:
                ok, img = cap.read()
                if ok and img is not None and img.size:
                    return cap, ("" if set_fps else "o driver recusou o pedido de FPS")
            cap.release()
        return None, "abriu mas não entrega quadros"

    def open(self, index: int | None, mode_key: str) -> bool:
        """Bloqueante (chamar fora da thread da interface)."""
        self.close()
        self.index, self.mode_key = index, mode_key
        if index is None:
            self.status = "sem câmera"
            return False
        self.status = "abrindo"
        cap, note = self._open_cap(index, MODES[mode_key])
        self.note = note
        if cap is None:
            self.status = "falhou: " + note
            return False
        self.cap = cap
        self.n = 0
        self._times.clear()
        self.running = True
        self.thread = threading.Thread(target=self._loop, name=f"cam-{index}", daemon=True)
        self.thread.start()
        self.status = "ok"
        return True

    def close(self) -> None:
        self.running = False
        if self.thread is not None:
            self.thread.join(timeout=2.0)
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
        self.cap = None
        self.thread = None
        with self.lock:
            self.latest = None
        self._times.clear()
        self.status = "fechada"

    # -- laço de captura --------------------------------------------------
    def _orient(self, img: np.ndarray) -> np.ndarray:
        r = self.rotation % 360
        if r == 90:
            img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
        elif r == 180:
            img = cv2.rotate(img, cv2.ROTATE_180)
        elif r == 270:
            img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
        if self.flip:
            img = cv2.flip(img, 1)
        return img

    def _loop(self) -> None:
        cap = self.cap
        use_drv = (not self.sim) and sys.platform.startswith("linux")
        last_ok = time.monotonic()
        while self.running:
            ok, img = cap.read()
            t = time.monotonic()
            if not ok or img is None:
                if t - last_ok > 1.0:
                    self.status = "sem sinal"
                time.sleep(0.005)
                continue
            last_ok = t
            if self.status != "ok":
                self.status = "ok"
            t_drv = float("nan")
            if use_drv:
                v = cap.get(cv2.CAP_PROP_POS_MSEC)
                if v and v > 0:
                    t_drv = v / 1000.0
            if img.ndim == 2:
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
            full = self._orient(img)
            px = roi_pixels(roi_to_frame(self.roi, self.mode_key), full.shape[1], full.shape[0])
            img = full if px is None else np.ascontiguousarray(full[px[1]:px[3], px[0]:px[2]])
            self.n += 1
            fr = Frame(img, t, t_drv, self.n, full)
            with self.lock:
                self.latest = fr
            self._times.append(t)
            for cb in list(self.listeners):
                try:
                    cb(fr)
                except Exception as ex:
                    print(f"[{self.eye}] listener: {ex}")

    # -- consulta ---------------------------------------------------------
    def latest_frame(self) -> Frame | None:
        with self.lock:
            return self.latest

    @property
    def fps(self) -> float:
        try:
            ts = tuple(self._times)
        except RuntimeError:
            return 0.0
        now = time.monotonic()
        ts = [t for t in ts if now - t < 1.5]
        if len(ts) < 3 or ts[-1] - ts[0] <= 0:
            return 0.0
        return (len(ts) - 1) / (ts[-1] - ts[0])

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.latest is not None


class CameraRig:
    """As câmeras dos dois olhos, a extra (opcional) e a configuração.
    A extra só abre se o usuário escolheu uma câmera para ela, e nunca tira
    a câmera de um olho. Abrir/fechar é lento e às vezes
    trava por segundos, então toda operação de hardware roda serializada
    numa thread própria (nunca duas aberturas simultâneas do mesmo
    dispositivo -- causa de 'Device busy' numa versão anterior)."""

    def __init__(self, cfg: dict, save_cb=None, sim: bool = False, backend: str = "auto"):
        self.cfg = cfg
        self.save_cb = save_cb or (lambda: None)
        self.sim = sim
        self.backend = backend
        self.cams = {e: Camera(e, backend, sim) for e in ROLES}
        for e in ROLES:
            self.cams[e].rotation = int(cfg["cameras"][e].get("rotation", 0))
            self.cams[e].flip = bool(cfg["cameras"][e].get("flip", False))
            self.cams[e].roi = clean_roi(cfg["cameras"][e].get("roi"))
        self.mode_key: str | None = None
        self.available: list[dict] = []
        self._jobs: queue.Queue = queue.Queue()
        self._pending = 0
        self._plock = threading.Lock()
        threading.Thread(target=self._worker, name="rig", daemon=True).start()

    # -- fila de trabalho -------------------------------------------------
    def _worker(self):
        while True:
            fn = self._jobs.get()
            if fn is None:
                break
            try:
                fn()
            except Exception as ex:
                print(f"[câmeras] {ex}")
            finally:
                with self._plock:
                    self._pending -= 1

    def _submit(self, fn):
        with self._plock:
            self._pending += 1
        self._jobs.put(fn)

    @property
    def busy(self) -> bool:
        return self._pending > 0

    def wait_idle(self, timeout: float = 15.0) -> bool:
        t0 = time.monotonic()
        while self.busy and time.monotonic() - t0 < timeout:
            time.sleep(0.05)
        return not self.busy

    # -- descoberta e atribuição -----------------------------------------
    def scan(self):
        if self.sim:
            self.available = [{"index": i, "id": f"sim{i}", "label": f"#{i} · câmera simulada"} for i in range(3)]
        else:
            in_use = tuple(c.index for c in self.cams.values() if c.cap is not None)
            self.available = list_cameras(self.backend, in_use)

    def _resolve(self) -> dict:
        by_id = {c["id"]: c["index"] for c in self.available}
        idxs = [c["index"] for c in self.available]
        used, out = set(), {}
        for e in ROLES:
            cc = self.cfg["cameras"][e]
            idx = by_id.get(cc.get("id")) if cc.get("id") else None
            # a extra só volta pelo número se não tinha id (o número muda entre boots)
            if idx is None and cc.get("index") in idxs and cc.get("index") not in used \
                    and (e != EXTRA or not cc.get("id")):
                idx = cc.get("index")
            if idx in used:
                idx = None
            out[e] = idx
            if idx is not None:
                used.add(idx)
        for e in EYES:
            if out[e] is None:
                free = [i for i in idxs if i not in used]
                if free:
                    out[e] = free[0]
                    used.add(free[0])
        return out

    def _assign(self, eye: str, index: int | None):
        cc = self.cfg["cameras"][eye]
        cc["index"] = index
        cc["id"] = next((c["id"] for c in self.available if c["index"] == index), None)

    def label_for(self, index) -> str:
        for c in self.available:
            if c["index"] == index:
                return c["label"]
        return "—" if index is None else f"#{index}"

    # -- operações --------------------------------------------------------
    def start(self, mode_key: str):
        """(Re)descobre as câmeras e abre as duas no modo pedido."""
        def job():
            for c in self.cams.values():
                c.close()
            self.scan()
            idx = self._resolve()
            self.mode_key = mode_key
            for e in ROLES:
                self._assign(e, idx[e])
                self.cams[e].open(idx[e], EXTRA_MODE if e == EXTRA else mode_key)
            self.save_cb()
        self.mode_key = mode_key
        self._submit(job)

    def set_mode(self, mode_key: str, force: bool = False):
        if mode_key == self.mode_key and not force:
            return
        self.mode_key = mode_key

        def job():
            # fecha as duas antes de abrir: libera banda USB e evita 'busy'
            # (a extra fica como está: o modo dela não muda)
            idx = {e: self.cams[e].index for e in EYES}
            for e in EYES:
                self.cams[e].close()
            for e in EYES:
                self.cams[e].open(idx[e], mode_key)
        self._submit(job)

    def swap(self):
        """Troca OD <-> OE. Rotação e espelhamento acompanham a câmera física
        (dependem de como ELA está montada), por isso trocam junto."""
        a, b = self.cams["OD"], self.cams["OE"]
        self.cams = {"OD": b, "OE": a}
        b.eye, a.eye = "OD", "OE"
        c = self.cfg["cameras"]
        c["OD"], c["OE"] = c["OE"], c["OD"]
        self.save_cb()

    def set_orientation(self, eye: str, rotation: int | None = None, flip: bool | None = None):
        """Gira/espelha e leva o recorte junto (ele continua sobre o olho)."""
        cam, cc = self.cams[eye], self.cfg["cameras"][eye]
        roi = cam.roi
        if rotation is not None:
            roi = roi_rotate(roi, (rotation - cam.rotation) % 360)
            cam.rotation = rotation % 360
            cc["rotation"] = cam.rotation
        if flip is not None and bool(flip) != cam.flip:
            roi = roi_flip(roi)
            cam.flip = bool(flip)
            cc["flip"] = cam.flip
        cam.roi = cc["roi"] = roi
        self.save_cb()

    def set_roi(self, eye: str, roi):
        roi = clean_roi(roi)
        self.cams[eye].roi = roi
        self.cfg["cameras"][eye]["roi"] = roi
        self.save_cb()

    def cycle(self, eye: str, step: int):
        """Passa este olho para a próxima câmera da lista. Se ela já é a do
        outro olho, equivale a trocar OD <-> OE; se é a da extra, a extra
        fica sem câmera. Na extra, a lista inclui 'nenhuma' e pula as
        câmeras dos olhos."""
        idxs = [c["index"] for c in self.available]
        if eye == EXTRA:
            eyes_idx = {self.cams[e].index for e in EYES}
            idxs = [None] + [i for i in idxs if i not in eyes_idx]
        if not idxs:
            return
        cur = self.cams[eye].index
        pos = idxs.index(cur) if cur in idxs else -1
        new = idxs[(pos + step) % len(idxs)]
        if eye != EXTRA:
            other = "OE" if eye == "OD" else "OD"
            if new == self.cams[other].index:
                self.swap()
                return
        if new == cur:
            return
        mode = EXTRA_MODE if eye == EXTRA else self.mode_key
        steal = eye != EXTRA and new is not None and new == self.cams[EXTRA].index

        def job():
            if steal:
                self.cams[EXTRA].open(None, EXTRA_MODE)
                self._assign(EXTRA, None)
            self._assign(eye, new)
            self.cams[eye].open(new, mode)
            self.save_cb()
        self._submit(job)

    def snapshot(self) -> dict:
        return {e: self.cams[e].latest_frame() for e in EYES}

    def shutdown(self):
        for c in self.cams.values():
            c.close()
        self._jobs.put(None)


# ════════════════════════════════════════════════════════════════════════
#  Pupila
# ════════════════════════════════════════════════════════════════════════

# Tamanho do olho na posição atual das câmeras, em PIXELS: raio da íris
# ~54 px (gravações de teste em 320×240: 43–67 px, mediana 54; fotos em
# 640×480: 54 px). É o mesmo nos dois modos porque o GC0308 em 320×240 NÃO
# reduz a imagem: entrega o CENTRO do sensor na mesma escala (1 px = 1 px,
# medido) -- só o campo de visão muda. Também não muda com recorte nem
# giro. Círculos fora dessa faixa (pele, sobrancelha, a órbita inteira)
# são descartados; depois de ~15 medidas boas a faixa se estreita em
# volta da íris real do paciente.
IRIS_R_PX = 54.0
IRIS_R_RANGE = (0.70, 1.35)       # tolerância sobre o esperado: 38–73 px
PUPIL_IRIS_RANGE = (0.12, 0.72)   # pupila / íris: ~1,4 a 8,4 mm numa íris de 11,7 mm (6,5–39 px de raio)
HVID_MM = 11.7                    # diâmetro horizontal médio da íris (adulto)
PUPIL_MIN_CONF = 0.35


@dataclass
class PupilReading:
    x: float              # centro da pupila (px do quadro analisado)
    y: float
    r: float              # raio da pupila = semieixo MAIOR da elipse (o olhar oblíquo encurta o menor)
    ellipse: tuple        # ((cx, cy), (eixo1, eixo2), ângulo) para desenhar
    iris: tuple           # (cx, cy, raio) -- raio = mediana aprendida
    ratio: float          # diâmetro pupila / diâmetro íris
    mm: float             # estimativa: ratio × HVID_MM
    conf: float           # 0..1


def _disk_mean(g, cx, cy, r0, r1):
    """(média, pixels) do anel r0..r1 em volta de (cx, cy), ou None."""
    h, w = g.shape[:2]
    x0, x1 = max(0, int(cx - r1)), min(w, int(cx + r1) + 1)
    y0, y1 = max(0, int(cy - r1)), min(h, int(cy + r1) + 1)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    yy, xx = np.ogrid[y0:y1, x0:x1]
    d2 = (xx - cx) ** 2 + (yy - cy) ** 2
    m = (d2 >= r0 * r0) & (d2 <= r1 * r1)
    if m.sum() < 8:
        return None
    px = g[y0:y1, x0:x1][m]
    return float(px.mean()), px


def _has_glint(g, x, y, r, base) -> bool:
    """Reflexo corneano dos LEDs IR dentro da íris: pele, pálpebra fechada e
    sobrancelha não têm. `base` = nível de cinza da íris."""
    inner = _disk_mean(g, x, y, 0, r * 0.85)
    if inner is None:
        return False
    bright = inner[1] >= max(150.0, base + 70)
    return bool(bright.any()) and bright.mean() < 0.05


class PupilTracker:
    """Acha íris e pupila quadro a quadro (um por olho, guarda estado).

    1. Íris: círculo de Hough só na faixa de tamanho esperada, mais escuro
       que a volta e com o reflexo dos LEDs dentro.
    2. Pupila: 'starburst' a partir de alguns pontos de partida dentro da
       íris; em cada raio, a borda escuro->claro mais forte na faixa
       possível; elipse nas bordas coerentes. Reflexos são apagados antes.
    O raio da íris não muda: a mediana das medidas serve para estreitar a
    busca e é a régua da pupila (mm = razão × 11,7 mm)."""

    def __init__(self, iris_r_px: float = IRIS_R_PX):
        self.r_prior = float(iris_r_px)
        self.iris_r: deque = deque(maxlen=150)
        self.prev = None
        self.miss = 0

    @property
    def learned(self) -> bool:
        return len(self.iris_r) >= 15

    @property
    def r_iris(self) -> float:
        return float(np.median(self.iris_r)) if self.learned else self.r_prior

    def _find_iris(self, g):
        r = self.r_iris
        lo, hi = (0.85, 1.18) if self.learned else IRIS_R_RANGE
        s = min(1.0, 36.0 / r)
        small = cv2.resize(g, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else g
        small = cv2.medianBlur(small, 5)
        rs = r * s
        c = cv2.HoughCircles(small, cv2.HOUGH_GRADIENT, dp=1.5, minDist=max(4.0, rs * 0.5), param1=60,
                             param2=18, minRadius=max(3, int(rs * lo)), maxRadius=int(rs * hi) + 1)
        best, best_sc = None, -1e9
        if c is not None:
            for x, y, rr in c[0][:8]:
                a = _disk_mean(small, x, y, 0, rr * 0.85)
                b = _disk_mean(small, x, y, rr * 1.1, rr * 1.4)
                if a is None or b is None or b[0] - a[0] < 6:
                    continue
                if not _has_glint(g, x / s, y / s, rr / s, a[0]):
                    continue
                sc = b[0] - a[0]
                if self.prev is not None and self.miss < 15:
                    sc -= 20 * math.hypot(x / s - self.prev[0], y / s - self.prev[1]) / r
                if sc > best_sc:
                    best, best_sc = (x / s, y / s, rr / s), sc
        return best

    def _find_pupil(self, g, iris):
        icx, icy, ir = iris
        R = int(ir * 0.95)
        h, w = g.shape[:2]
        x0, y0 = max(0, int(icx - R)), max(0, int(icy - R))
        x1, y1 = min(w, int(icx + R) + 1), min(h, int(icy + R) + 1)
        crop = g[y0:y1, x0:x1]
        if crop.shape[0] < 12 or crop.shape[1] < 12:
            return None
        med = float(np.median(crop))
        glint = (crop >= max(200, med + 90)).astype(np.uint8)
        seeds = []
        if glint.any():
            glint = cv2.dilate(glint, np.ones((5, 5), np.uint8))
            crop = cv2.inpaint(crop, glint, 3, cv2.INPAINT_TELEA)
            m = cv2.moments(glint, True)
            seeds.append((m["m10"] / m["m00"], m["m01"] / m["m00"]))
        blur = cv2.GaussianBlur(crop, (0, 0), max(1.0, ir * 0.025))
        dark = cv2.GaussianBlur(crop, (0, 0), max(1.5, ir * 0.08))
        cx, cy = icx - x0, icy - y0
        yy, xx = np.ogrid[:crop.shape[0], :crop.shape[1]]
        allowed = ((xx - cx) ** 2 + (yy - cy) ** 2) <= (0.5 * ir) ** 2
        _, _, dmin, _ = cv2.minMaxLoc(np.where(allowed, dark, 255).astype(np.uint8))
        best = None
        for seed in [dmin, (cx, cy)] + seeds:
            fit = self._fit_pupil(blur, glint, seed, (cx, cy), ir)
            if fit is not None and (best is None or fit[4] > best[4]):
                best = fit
        if best is None:
            return None
        px, py, rp, ((ex, ey), ax, ea), conf = best
        return px + x0, py + y0, rp, ((ex + x0, ey + y0), ax, ea), conf

    @staticmethod
    def _fit_pupil(blur, glint, seed, center, ir):
        """Raios a partir do `seed`; raios cuja borda cai sobre um reflexo são
        ignorados. Elipse nas bordas coerentes, duas passadas (a 2ª parte do
        centro da 1ª elipse)."""
        px, py = float(seed[0]), float(seed[1])
        rmin = max(2, int(PUPIL_IRIS_RANGE[0] * ir))
        rmax = max(rmin + 3, int(PUPIL_IRIS_RANGE[1] * ir))
        n_ang = 64
        fl = cv2.WARP_POLAR_LINEAR + cv2.INTER_LINEAR + cv2.WARP_FILL_OUTLIERS
        for _ in range(2):
            pol = cv2.warpPolar(blur, (rmax + 3, n_ang), (px, py), rmax + 3, fl).astype(np.float32)
            band = (pol[:, 2:] - pol[:, :-2])[:, rmin - 1:rmax - 1]
            idx = band.argmax(axis=1) + rmin
            strength = band.max(axis=1)
            good = strength > max(1.0, 0.35 * float(np.median(strength)))
            if glint.any():
                gp = cv2.warpPolar(glint * 255, (rmax + 3, n_ang), (px, py), rmax + 3, fl)
                hit = np.concatenate([np.zeros((n_ang, 1), int), np.cumsum(gp > 0, axis=1)], 1)
                a_, b_ = np.maximum(idx - 3, 0), np.minimum(idx + 4, rmax + 3)
                good &= (hit[np.arange(n_ang), b_] - hit[np.arange(n_ang), a_]) == 0
            if good.sum() < n_ang * 0.3:
                return None
            rmed = float(np.median(idx[good]))
            inl = good & (np.abs(idx - rmed) <= max(2.0, 0.2 * rmed))
            if inl.sum() < 10:
                return None
            ang = np.arange(n_ang)[inl] * 2 * np.pi / n_ang
            pts = np.stack([px + idx[inl] * np.cos(ang), py + idx[inl] * np.sin(ang)], 1).astype(np.float32)
            (ex, ey), (a1, a2), ea = cv2.fitEllipse(pts)
            if not (np.isfinite(a1) and np.isfinite(a2)) or min(a1, a2) < 0.6 * max(a1, a2):
                return None
            px, py = ex, ey
        rp = max(a1, a2) / 2
        if not (PUPIL_IRIS_RANGE[0] * ir <= rp <= PUPIL_IRIS_RANGE[1] * ir):
            return None
        if math.hypot(px - center[0], py - center[1]) > 0.5 * ir:
            return None
        a = _disk_mean(blur, px, py, 0, rp * 0.7)
        b = _disk_mean(blur, px, py, rp * 1.15, rp * 1.45)
        if a is None or b is None or b[0] - a[0] < 4:
            return None
        # contraste em relação ao ruído da íris: vale em imagem escura e clara
        noise = max(1.5, float(np.std(b[1])))
        conf = float(inl.sum() / n_ang * min(1.0, (b[0] - a[0]) / (2.5 * noise)))
        return px, py, rp, ((ex, ey), (a1, a2), ea), conf

    def update(self, img: np.ndarray) -> PupilReading | None:
        g = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        iris = self._find_iris(g)
        hough = iris is not None
        if iris is None and self.prev is not None and self.miss < 10:
            a = _disk_mean(g, self.prev[0], self.prev[1], 0, self.prev[2] * 0.85)
            if a is not None and _has_glint(g, *self.prev, a[0]):
                iris = self.prev
        if iris is None:
            self.miss += 1
            return None
        p = self._find_pupil(g, (iris[0], iris[1], self.r_iris if self.learned else iris[2]))
        if p is None or p[4] < PUPIL_MIN_CONF:
            self.miss += 1
            return None
        self.miss = 0
        self.prev = iris
        if hough:
            self.iris_r.append(iris[2])
        ir = self.r_iris if self.learned else iris[2]
        ratio = p[2] / ir
        return PupilReading(p[0], p[1], p[2], p[3], (iris[0], iris[1], ir), ratio, ratio * HVID_MM, p[4])


def draw_pupil(img: np.ndarray, rd: PupilReading | None, label: bool = True) -> np.ndarray:
    """Desenha íris (verde), pupila (laranja) e o diâmetro, NO PRÓPRIO `img`."""
    h, w = img.shape[:2]
    s = max(1, int(round(min(h, w) / 240)))
    if rd is not None:
        ix, iy, ir = rd.iris
        cv2.circle(img, (int(ix), int(iy)), int(ir), (90, 200, 90), s, cv2.LINE_AA)
        cv2.ellipse(img, rd.ellipse, (40, 160, 255), s, cv2.LINE_AA)
        cv2.drawMarker(img, (int(rd.x), int(rd.y)), (40, 160, 255), cv2.MARKER_CROSS, 6 * s, s)
    if label:
        txt = f"pupila {rd.mm:.1f} mm  P/I {rd.ratio:.2f}" if rd is not None else "pupila --"
        fs = 0.42 * max(1.0, min(h, w) / 240)
        cv2.putText(img, txt, (6, h - 8), cv2.FONT_HERSHEY_SIMPLEX, fs, (0, 0, 0), 3 * s, cv2.LINE_AA)
        cv2.putText(img, txt, (6, h - 8), cv2.FONT_HERSHEY_SIMPLEX, fs, (40, 160, 255) if rd else (170, 170, 170),
                    s, cv2.LINE_AA)
    return img


class PupilMonitor:
    """Mede a pupila de uma câmera ao vivo numa thread própria. Pega sempre
    o quadro mais recente e pula os que chegam enquanto está ocupada (nunca
    atrasa a captura nem a gravação). `max_hz` limita o uso de CPU.
    `on_reading(frame, leitura)` é chamado na thread do monitor."""

    def __init__(self, cam: Camera, max_hz: float = 30.0, on_reading=None):
        self.cam, self.max_hz, self.on_reading = cam, max_hz, on_reading
        self.latest: tuple | None = None       # (Frame, PupilReading | None)
        # um tracker só: o olho tem o mesmo tamanho em pixels nos dois modos
        self.tracker = PupilTracker()
        self._slot = None
        self._ev = threading.Event()
        self.running = True
        cam.listeners.append(self._on_frame)
        self.thread = threading.Thread(target=self._run, name=f"pupila-{cam.eye}", daemon=True)
        self.thread.start()

    def _on_frame(self, fr: Frame):
        self._slot = fr
        self._ev.set()

    def _run(self):
        while self.running:
            if not self._ev.wait(0.25):
                continue
            self._ev.clear()
            fr, t0 = self._slot, time.monotonic()
            if fr is None:
                continue
            try:
                rd = self.tracker.update(fr.img)
            except Exception as ex:
                print(f"[pupila {self.cam.eye}] {ex}")
                rd = None
            self.latest = (fr, rd)
            if self.on_reading:
                self.on_reading(fr, rd)
            wait = 1.0 / self.max_hz - (time.monotonic() - t0)
            if wait > 0:
                time.sleep(wait)

    def reading(self, max_age: float = 0.5) -> PupilReading | None:
        lt = self.latest
        if lt is None or time.monotonic() - lt[0].t > max_age:
            return None
        return lt[1]

    def stop(self):
        self.running = False
        try:
            self.cam.listeners.remove(self._on_frame)
        except ValueError:
            pass
        self._ev.set()


def analyze_pupil_video(path: Path, progress=None, total: int = 0) -> list:
    """Mede a pupila em todos os quadros de um vídeo: lista de
    PupilReading | None, um por quadro (na ordem do vídeo)."""
    cap = cv2.VideoCapture(str(path), cv2.CAP_OPENCV_MJPEG)
    if not cap.isOpened():
        cap = cv2.VideoCapture(str(path))
    tr, out = PupilTracker(), []
    while True:
        ok, img = cap.read()
        if not ok:
            break
        out.append(tr.update(img))
        if progress and total and len(out) % 50 == 0:
            progress(len(out) / total)
    cap.release()
    return out


# ════════════════════════════════════════════════════════════════════════
#  Gravação
# ════════════════════════════════════════════════════════════════════════

def patch_avi_fps(path: Path, fps: float) -> bool:
    """Reescreve a taxa no cabeçalho do AVI. O FPS real só é conhecido no
    fim (depende do que a câmera entregou e das pausas); gravar o vídeo
    com o FPS medido faz ele tocar em TEMPO REAL."""
    if not fps or fps <= 0 or not path.exists():
        return False
    with open(path, "r+b") as f:
        head = f.read(1 << 16)
        i = head.find(b"avih")
        if i < 0:
            return False
        f.seek(i + 8)
        f.write(struct.pack("<I", int(round(1e6 / fps))))
        j = head.find(b"strh")
        while j >= 0:
            if head[j + 8:j + 12] == b"vids":
                f.seek(j + 8 + 20)   # dwScale, dwRate
                f.write(struct.pack("<II", 1000, int(round(fps * 1000))))
                break
            j = head.find(b"strh", j + 4)
    return True


def new_writer(path: Path, fps: float, size: tuple) -> cv2.VideoWriter:
    # CAP_OPENCV_MJPEG = gravador AVI embutido do OpenCV: igual em qualquer
    # sistema, não depende de FFmpeg/GStreamer, e o layout do cabeçalho é
    # conhecido (patch_avi_fps).
    return cv2.VideoWriter(str(path), cv2.CAP_OPENCV_MJPEG, cv2.VideoWriter_fourcc(*"MJPG"),
                           float(fps), size, True)


class EyeWriter:
    """Codifica os quadros de um olho numa thread própria, para a captura
    nunca esperar o disco."""

    def __init__(self, path: Path, header_fps: float):
        self.path = path
        self.header_fps = header_fps
        self.q: queue.Queue = queue.Queue(maxsize=600)
        self.rows: list[tuple] = []   # (quadro_camera, t, t_drv, segmento)
        self.dropped = 0
        self.size = None
        self.error = None
        self.writer = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def put(self, fr: Frame, seg: int):
        try:
            self.q.put_nowait((Frame(fr.img, fr.t, fr.t_drv, fr.n), seg))   # sem o quadro inteiro
        except queue.Full:
            self.dropped += 1

    def _run(self):
        while True:
            item = self.q.get()
            if item is None:
                break
            fr, seg = item
            img = fr.img
            h, w = img.shape[:2]
            if self.writer is None and self.error is None:
                self.size = (w, h)
                self.writer = new_writer(self.path, self.header_fps, self.size)
                if not self.writer.isOpened():
                    self.error = f"não foi possível criar {self.path.name}"
                    self.writer = None
            if self.writer is None or (w, h) != self.size:
                self.dropped += 1
                continue
            self.writer.write(img)
            self.rows.append((fr.n, fr.t, fr.t_drv, seg))
        if self.writer is not None:
            self.writer.release()

    def finish(self):
        self.q.put(None)
        self.thread.join()


def _open_avi(path: Path):
    cap = cv2.VideoCapture(str(path), cv2.CAP_OPENCV_MJPEG)
    if not cap.isOpened():
        cap = cv2.VideoCapture(str(path))
    return cap


def build_side_by_side(streams: list, out_path: Path, fps: float, t0: float, progress=None) -> int:
    """Vídeo com os streams lado a lado (ex. OD | OE | extra). O 1º stream
    é a referência de tempo: cada quadro dele é pareado com o quadro mais
    próximo no tempo de cada um dos outros (as câmeras não são
    sincronizadas por hardware; a extra, mais lenta, repete quadros).
    Altura = a do 1º stream; os outros são reescalados.

    streams: [{"label", "path", "ts": [t por quadro], "pupil": None | [leitura por quadro]}]"""
    caps = [_open_avi(st["path"]) for st in streams]
    cur, idx = [None] * len(streams), [0] * len(streams)
    for k in range(1, len(streams)):
        ok, cur[k] = caps[k].read()
        if not ok:
            return 0
    n, writer = 0, None
    ref = streams[0]["ts"]
    total = max(1, len(ref))
    for i, t in enumerate(ref):
        ok, cur[0] = caps[0].read()
        if not ok:
            break
        idx[0] = i
        for k in range(1, len(streams)):
            ts = streams[k]["ts"]
            while idx[k] + 1 < len(ts) and abs(ts[idx[k] + 1] - t) <= abs(ts[idx[k]] - t):
                ok2, img = caps[k].read()
                if not ok2:
                    break
                idx[k] += 1
                cur[k] = img
        H = cur[0].shape[0]
        s = max(0.4, H / 480)
        tiles, xs = [], []
        for k, st in enumerate(streams):
            img = cur[k]
            if st.get("pupil") is not None:
                rd = st["pupil"][idx[k]] if idx[k] < len(st["pupil"]) else None
                img = draw_pupil(img.copy(), rd)
            if img.shape[0] != H:
                img = cv2.resize(img, (int(img.shape[1] * H / img.shape[0]), H), interpolation=cv2.INTER_AREA)
            if tiles:
                tiles.append(np.full((H, 4, 3), 40, np.uint8))
            xs.append(sum(tl.shape[1] for tl in tiles))
            tiles.append(img)
        frame = np.hstack(tiles)
        for st, x in zip(streams, xs):
            cv2.putText(frame, st["label"], (x + 6, int(20 * s + 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * s,
                        (255, 255, 255), max(1, int(2 * s)), cv2.LINE_AA)
        cv2.putText(frame, f"{t - t0:8.3f} s", (frame.shape[1] - int(125 * s), H - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5 * s, (220, 220, 220), 1, cv2.LINE_AA)
        if writer is None:
            writer = new_writer(out_path, fps, (frame.shape[1], frame.shape[0]))
        writer.write(frame)
        n += 1
        if progress and i % 25 == 0:
            progress(i / total)
    if writer is not None:
        writer.release()
    for c in caps:
        c.release()
    return n


VIDEO_FILES = {"OD": "OD.avi", "OE": "OE.avi", EXTRA: "extra.avi"}
PAIR_FILE = "OD_OE_lado_a_lado.avi"
TRIPLE_FILE = "OD_OE_extra_lado_a_lado.avi"


class Recorder:
    """Um registro: pode ser pausado e continuado várias vezes (cada trecho
    é um 'segmento'), e no fim é salvo ou descartado. Grava os dois olhos e,
    se estiver com imagem, a câmera extra.

    FPS: guarda o maior FPS possível até o alvo. Se a câmera entrega mais
    que o alvo, os quadros são dizimados por relógio (próximo quadro devido
    a cada 1/alvo s), o que dá o alvo em média; se entrega menos, guarda
    todos. O FPS gravado no arquivo é o EFETIVO medido.

    `pupil=True`: ao salvar, mede a pupila em cada quadro de cada olho
    (pupila.csv) e desenha a medida nos vídeos lado a lado. Os vídeos de
    cada olho ficam sem marcação."""

    def __init__(self, rig: CameraRig, out_root: Path, patient: str, target_fps: float, mode_key: str,
                 pupil: bool = False):
        self.rig = rig
        self.out_root = Path(out_root).expanduser()
        self.patient = patient
        self.target = float(target_fps)
        self.mode_key = mode_key
        self.pupil = pupil
        self.state = "idle"     # idle rec paused saving saved discarded error
        self.progress = 0.0
        self.error = None
        self.saved_path: Path | None = None
        self.summary: dict = {}
        self.roles = EYES + ((EXTRA,) if rig.cams[EXTRA].ok else ())

    # -- controle ---------------------------------------------------------
    def start(self):
        self.started_at = datetime.now()
        self.out_root.mkdir(parents=True, exist_ok=True)
        self.tmp = self.out_root / f".gravando_{self.started_at:%Y%m%d_%H%M%S}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.cam_fps = {e: self.rig.cams[e].fps for e in self.roles}
        self.cam_info = {e: {"camera": self.rig.label_for(self.rig.cams[e].index),
                             "rotacao": self.rig.cams[e].rotation,
                             "espelhado": self.rig.cams[e].flip,
                             "recorte_fracao_xywh": self.rig.cams[e].roi} for e in self.roles}
        hdr = {e: (min(self.target, self.cam_fps[e]) if self.cam_fps[e] > 1 else self.target) for e in self.roles}
        self.writers = {e: EyeWriter(self.tmp / VIDEO_FILES[e], hdr[e]) for e in self.roles}
        self.cam_period = {e: 1.0 / self.cam_fps[e] if self.cam_fps[e] > 1 else 1.0 / 120 for e in self.roles}
        self.next_due = {e: None for e in self.roles}
        self.segment = 1
        self.t0 = time.monotonic()
        self.seg_start = self.t0
        self.active = 0.0
        self._hooks = []
        for e in self.roles:
            cam = self.rig.cams[e]
            fn = (lambda fr, e=e: self._on_frame(e, fr))
            cam.listeners.append(fn)
            self._hooks.append((cam, fn))
        self.state = "rec"

    def _on_frame(self, eye: str, fr: Frame):
        if self.state != "rec" or fr.t < self.seg_start:
            return
        P = 1.0 / self.target
        nd = self.next_due[eye]
        if nd is None or fr.t >= nd - 0.5 * self.cam_period[eye]:
            base = fr.t if nd is None else nd
            nd = base + P
            if nd < fr.t - P:          # ficou para trás (câmera travou): ressincroniza
                nd = fr.t + P
            self.next_due[eye] = nd
            self.writers[eye].put(fr, self.segment)

    def pause(self):
        if self.state == "rec":
            self.state = "paused"
            self.active += time.monotonic() - self.seg_start

    def resume(self):
        if self.state == "paused":
            self.segment += 1
            self.next_due = {e: None for e in self.roles}
            self.seg_start = time.monotonic()
            self.state = "rec"

    def elapsed(self) -> float:
        if self.state == "rec":
            return self.active + time.monotonic() - self.seg_start
        return self.active

    def frames(self, eye: str) -> int:
        return len(self.writers[eye].rows) if eye in getattr(self, "writers", {}) else 0

    def dropped(self) -> int:
        return sum(w.dropped for w in self.writers.values()) if hasattr(self, "writers") else 0

    @property
    def active_registro(self) -> bool:
        return self.state in ("rec", "paused", "saving")

    def _stop_capture(self):
        if self.state == "rec":
            self.active += time.monotonic() - self.seg_start
        self.state = "stopping"
        for cam, fn in self._hooks:
            try:
                cam.listeners.remove(fn)
            except ValueError:
                pass
        self._hooks = []
        for w in self.writers.values():
            w.finish()

    # -- descartar / salvar ----------------------------------------------
    def discard(self):
        if self.state in ("idle", "discarded", "saved"):
            return
        self._stop_capture()
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.state = "discarded"

    def save_async(self):
        self.progress = 0.0
        self.pause()              # fecha a contagem do trecho atual
        self.state = "saving"
        threading.Thread(target=self._save, name="salvar", daemon=True).start()

    def _pupil_pass(self, p0: float, p1: float) -> dict:
        """Mede a pupila em todos os quadros dos dois olhos (uma thread por
        olho) e escreve pupila.csv. Devolve {olho: [leitura | None]}."""
        eyes = [e for e in EYES if self.writers[e].rows]
        part = {e: 0.0 for e in eyes}
        res: dict = {}

        def run(e):
            def prog(f):
                part[e] = f
                self.progress = p0 + (p1 - p0) * sum(part.values()) / len(eyes)
            res[e] = analyze_pupil_video(self.writers[e].path, prog, len(self.writers[e].rows))

        threads = [threading.Thread(target=run, args=(e,), daemon=True) for e in eyes]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with open(self.tmp / "pupila.csv", "w", encoding="utf-8") as f:
            f.write("olho,quadro_video,t_s,pupila_diam_px,iris_diam_px,razao_pupila_iris,pupila_mm_estimado,"
                    "confianca,pupila_x,pupila_y\n")
            for e in eyes:
                rows = self.writers[e].rows
                for k, rd in enumerate(res[e]):
                    t = f"{rows[k][1] - self.t0:.6f}" if k < len(rows) else ""
                    if rd is None:
                        f.write(f"{e},{k},{t},,,,,,,\n")
                    else:
                        f.write(f"{e},{k},{t},{2 * rd.r:.2f},{2 * rd.iris[2]:.2f},{rd.ratio:.4f},{rd.mm:.2f},"
                                f"{rd.conf:.2f},{rd.x:.1f},{rd.y:.1f}\n")
        return res

    @staticmethod
    def _pupil_stats(readings: list) -> dict:
        mm = np.array([rd.mm for rd in readings if rd is not None])
        st = {"quadros_medidos": int(len(mm)), "quadros_total": len(readings)}
        if len(mm):
            p5, p50, p95 = np.percentile(mm, [5, 50, 95])
            st.update(mediana_mm=round(float(p50), 2), p5_mm=round(float(p5), 2), p95_mm=round(float(p95), 2))
        return st

    def _save(self):
        try:
            self._stop_capture()
            self.state = "saving"
            self.progress = 0.08
            active = max(self.active, 1e-3)
            eff = {}
            for e, w in self.writers.items():
                n = len(w.rows)
                eff[e] = n / active if n else 0.0
                if n:
                    patch_avi_fps(w.path, eff[e])
            self.progress = 0.12

            with open(self.tmp / "quadros.csv", "w", encoding="utf-8") as f:
                f.write("olho,quadro_video,quadro_camera,t_s,t_driver_s,segmento\n")
                for e, w in self.writers.items():
                    for k, (ncam, t, tdrv, seg) in enumerate(w.rows):
                        td = "" if math.isnan(tdrv) else f"{tdrv:.6f}"
                        f.write(f"{e},{k},{ncam},{t - self.t0:.6f},{td},{seg}\n")

            pupil = self._pupil_pass(0.12, 0.40) if self.pupil else {}
            p0 = self.progress = 0.40 if self.pupil else 0.12

            def stream(e):
                w = self.writers[e]
                return {"label": "extra" if e == EXTRA else e, "path": w.path, "ts": [r[1] for r in w.rows],
                        "pupil": pupil.get(e)}

            both = all(self.writers[e].rows for e in EYES)
            jobs = []
            if both:
                jobs.append((PAIR_FILE, [stream("OD"), stream("OE")]))
                if EXTRA in self.writers and self.writers[EXTRA].rows:
                    jobs.append((TRIPLE_FILE, [stream("OD"), stream("OE"), stream(EXTRA)]))
            made = []
            for j, (name, streams) in enumerate(jobs):
                a, b = p0 + (0.96 - p0) * j / len(jobs), p0 + (0.96 - p0) * (j + 1) / len(jobs)
                n = build_side_by_side(streams, self.tmp / name, eff["OD"], self.t0,
                                       progress=lambda p, a=a, b=b: setattr(self, "progress", a + (b - a) * p))
                if n:
                    patch_avi_fps(self.tmp / name, n / active)
                    made.append(name)
            self.progress = 0.96

            def info(e):
                return {
                    "arquivo": VIDEO_FILES[e] if self.writers[e].rows else None,
                    "quadros": len(self.writers[e].rows),
                    "fps_efetivo": round(eff[e], 2),
                    "fps_camera_no_inicio": round(self.cam_fps[e], 2),
                    "resolucao": list(self.writers[e].size) if self.writers[e].size else None,
                    "perdidos_fila_disco": self.writers[e].dropped,
                    "erro": self.writers[e].error,
                    **self.cam_info[e]}

            self.summary = {
                "programa": f"OculoDiag {VERSION}",
                "paciente": self.patient,
                "inicio": self.started_at.isoformat(timespec="seconds"),
                "duracao_gravada_s": round(self.active, 3),
                "segmentos": self.segment,
                "fps_alvo": self.target,
                "modo": MODES[self.mode_key]["rotulo"],
                "video_lado_a_lado": PAIR_FILE if PAIR_FILE in made else None,
                "video_lado_a_lado_com_extra": TRIPLE_FILE if TRIPLE_FILE in made else None,
                "olhos": {e: info(e) for e in EYES},
                "camera_extra": info(EXTRA) if EXTRA in self.writers else None,
                "observacao": ("Vídeos na vista do examinador (OD à esquerda). Pausas não aparecem nos "
                               "vídeos: veja t_s e 'segmento' em quadros.csv (olho EXT = câmera extra). "
                               "t_s é o relógio do computador na chegada do quadro; t_driver_s, quando "
                               "existe, é o timestamp do driver V4L2."),
            }
            if self.pupil:
                self.summary["pupila"] = {
                    "arquivo": "pupila.csv",
                    "olhos": {e: self._pupil_stats(r) for e, r in pupil.items()},
                    "observacao": (f"Medida marcada só nos vídeos lado a lado. mm estimado = razão "
                                   f"pupila/íris × {HVID_MM} mm (diâmetro médio da íris de adulto)."),
                }
            (self.tmp / "registro.json").write_text(json.dumps(self.summary, indent=2, ensure_ascii=False),
                                                    encoding="utf-8")
            final = self.out_root / f"{self.started_at:%Y-%m-%d_%H%M%S}_{safe_name(self.patient)}_gravacao"
            k = 2
            while final.exists():
                final = final.with_name(final.name.rsplit("~", 1)[0] + f"~{k}")
                k += 1
            os.replace(self.tmp, final)
            self.saved_path = final
            self.progress = 1.0
            self.state = "saved"
        except Exception as ex:
            self.error = f"{type(ex).__name__}: {ex}"
            self.state = "error"


# ════════════════════════════════════════════════════════════════════════
#  Página de resultado da motilidade
# ════════════════════════════════════════════════════════════════════════

_FONT_CACHE: dict = {}


def load_font(size: int, bold: bool = False):
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    names = (["DejaVuSans-Bold.ttf", "segoeuib.ttf", "arialbd.ttf", "Arial Bold.ttf", "LiberationSans-Bold.ttf"]
             if bold else ["DejaVuSans.ttf", "segoeui.ttf", "arial.ttf", "Arial.ttf", "LiberationSans-Regular.ttf"])
    font = None
    for n in names:
        try:
            font = ImageFont.truetype(n, size)
            break
        except OSError:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:
            font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _wrap(d: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    out, line = [], ""
    for word in text.split():
        cand = f"{line} {word}".strip()
        if line and d.textlength(cand, font=font) > width:
            out.append(line)
            line = "  " + word if text.startswith("•") else word
        else:
            line = cand
    return out + ([line] if line else [])


def _fit(img: np.ndarray, w: int, h: int) -> Image.Image:
    """Encaixa sem cortar (tarja preta nas sobras)."""
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    ih, iw = rgb.shape[:2]
    s = min(w / iw, h / ih)
    nw, nh = max(1, int(iw * s)), max(1, int(ih * s))
    pim = Image.fromarray(rgb).resize((nw, nh), Image.LANCZOS)
    box = Image.new("RGB", (w, h), (0, 0, 0))
    box.paste(pim, ((w - nw) // 2, (h - nh) // 2))
    return box


def compose_motility_report(captures: dict, info: dict) -> Image.Image:
    sample = next((c[e].img for c in captures.values() for e in EYES if c.get(e) is not None), None)
    ar = (sample.shape[1] / sample.shape[0]) if sample is not None else 4 / 3
    ih = 250 if ar < 1.1 else 200                  # retrato (câmera girada) x paisagem
    iw = int(round(ih * ar))
    gap, pad, label_h = 6, 16, 54
    cell_w = 2 * iw + gap + 2 * pad
    cell_h = pad + label_h + ih + pad
    margin, gx, gy = 44, 18, 18
    header_h, footer_h = 132, 64
    W = 2 * margin + 3 * cell_w + 2 * gx
    H = header_h + 4 * cell_h + 3 * gy + footer_h + margin // 2

    ink, muted, card, accent = (17, 24, 39), (107, 114, 128), (243, 245, 248), (37, 99, 235)
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    f_title, f_h, f_b, f_s, f_tag = load_font(30, True), load_font(18, True), load_font(15), load_font(13), load_font(12, True)

    d.text((margin, 34), "Motilidade ocular · versões", font=f_title, fill=ink)
    d.text((margin, 78), "Vista do examinador (como se o paciente olhasse para quem lê): "
                         "OD à esquerda, OE à direita · o lado DIREITO do paciente fica à ESQUERDA da página",
           font=f_s, fill=muted)
    right = [f"Paciente: {info.get('paciente') or '—'}", info.get("data", "")]
    for k, line in enumerate(right):
        tw = d.textlength(line, font=f_h if k == 0 else f_b)
        d.text((W - margin - tw, 34 + 28 * k), line, font=f_h if k == 0 else f_b, fill=ink if k == 0 else muted)
    d.line((margin, header_h - 18, W - margin, header_h - 18), fill=(226, 230, 236), width=2)

    def cell_xy(r, c):
        return margin + c * (cell_w + gx), header_h + r * (cell_h + gy)

    for pos in POSITIONS:
        r, c = pos["rc"]
        x, y = cell_xy(r, c)
        special = pos["key"] in ("longe", "perto")
        d.rounded_rectangle((x, y, x + cell_w, y + cell_h), radius=14, fill=card,
                            outline=accent if special else None, width=2 if special else 0)
        d.text((x + pad, y + pad - 2), pos["nome"], font=f_h, fill=ink)
        d.text((x + pad, y + pad + 24), pos["descr"], font=f_s, fill=muted)
        cap = captures.get(pos["key"])
        for k, e in enumerate(EYES):
            ex, ey = x + pad + k * (iw + gap), y + pad + label_h
            fr = cap.get(e) if cap else None
            if fr is None:
                d.rounded_rectangle((ex, ey, ex + iw, ey + ih), radius=6, fill=(225, 228, 233))
                msg = "não registrado" if cap is None else "sem imagem"
                tw = d.textlength(msg, font=f_s)
                d.text((ex + (iw - tw) / 2, ey + ih / 2 - 8), msg, font=f_s, fill=muted)
            else:
                im.paste(_fit(fr.img, iw, ih), (ex, ey))
            d.rounded_rectangle((ex + 6, ey + 6, ex + 36, ey + 26), radius=4, fill=(17, 24, 39))
            d.text((ex + 10, ey + 8), e, font=f_tag, fill="white")

    # linha 4: legenda à esquerda e dados técnicos à direita do "perto"
    def note(col, title, items):
        x, y = cell_xy(3, col)
        d.text((x + pad, y + pad), title, font=f_h, fill=ink)
        yy = y + pad + 32
        for item in items:
            for ln in _wrap(d, item, f_s, cell_w - 2 * pad):
                d.text((x + pad, yy), ln, font=f_s, fill=muted)
                yy += 20
            yy += 4

    note(0, "Como ler", ["• Cada quadro mostra os dois olhos na mesma direção do olhar.",
                         "• Colunas: direita do paciente à esquerda da página.",
                         "• Centro = posição primária (longe).",
                         "• Abaixo = perto, para avaliar a convergência."])
    note(2, "Registro", info.get("tecnico", []))

    d.text((margin, H - footer_h + 14),
           f"OculoDiag {VERSION} · gerado em {datetime.now():%d/%m/%Y %H:%M} · imagens originais sem "
           f"compressão na pasta imagens/", font=f_s, fill=muted)
    return im


def motility_html(captures: dict, info: dict, files: dict) -> str:
    cells = []
    for pos in POSITIONS:
        r, c = pos["rc"]
        figs = []
        for e in EYES:
            fn = files.get((pos["key"], e))
            if fn:
                figs.append(f'<figure><a href="{fn}"><img src="{fn}" alt="{e} {pos["nome"]}"></a>'
                            f'<span class="tag">{e}</span></figure>')
            else:
                msg = "não registrado" if pos["key"] not in captures else "sem imagem"
                figs.append(f'<figure class="empty"><span>{msg}</span><span class="tag">{e}</span></figure>')
        cls = "cell hl" if pos["key"] in ("longe", "perto") else "cell"
        cells.append(f'<div class="{cls}" style="grid-row:{r + 1};grid-column:{c + 1}">'
                     f'<h2>{pos["nome"]}</h2><p>{pos["descr"]}</p><div class="pair">{"".join(figs)}</div></div>')
    tech = "".join(f"<li>{t.lstrip('• ')}</li>" for t in info.get("tecnico", []))
    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Motilidade ocular — {info.get('paciente') or 'sem ID'}</title>
<style>
body{{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:28px;color:#111827;background:#fff}}
header{{display:flex;justify-content:space-between;align-items:flex-start;gap:24px;border-bottom:2px solid #e2e6ec;padding-bottom:14px;margin-bottom:18px}}
h1{{margin:0 0 6px;font-size:26px}} .sub{{color:#6b7280;font-size:13px;margin:0}}
.pt{{text-align:right}} .pt b{{font-size:17px}} .pt div{{color:#6b7280;font-size:14px}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}
.cell{{background:#f3f5f8;border-radius:12px;padding:12px;border:2px solid transparent}} .cell.hl{{border-color:#2563eb}}
.cell h2{{font-size:15px;margin:0}} .cell p{{margin:3px 0 8px;color:#6b7280;font-size:12px}}
.pair{{display:flex;gap:6px}} figure{{margin:0;flex:1;position:relative;background:#000;border-radius:6px;overflow:hidden}}
figure img{{display:block;width:100%}} figure.empty{{background:#e1e4e9;min-height:120px;display:flex;align-items:center;justify-content:center;color:#6b7280;font-size:12px}}
.tag{{position:absolute;top:6px;left:6px;background:#111827;color:#fff;font:600 11px system-ui;padding:2px 6px;border-radius:4px}}
.note{{font-size:12px;color:#6b7280}} .note ul{{margin:4px 0 0;padding-left:16px}}
@media print{{body{{margin:10mm}} .cell{{break-inside:avoid}}}}
</style></head><body>
<header><div><h1>Motilidade ocular · versões</h1>
<p class="sub">Vista do examinador: OD à esquerda, OE à direita · o lado DIREITO do paciente fica à ESQUERDA da página</p></div>
<div class="pt"><b>Paciente: {info.get('paciente') or '—'}</b><div>{info.get('data', '')}</div></div></header>
<div class="grid">{''.join(cells)}
<div class="note" style="grid-row:4;grid-column:1"><b>Como ler</b><ul><li>Cada quadro: os dois olhos na mesma direção do olhar.</li>
<li>Centro = posição primária (longe); abaixo = perto.</li><li>Clique numa imagem para abrir o original.</li></ul></div>
<div class="note" style="grid-row:4;grid-column:3"><b>Registro</b><ul>{tech}</ul></div>
</div></body></html>"""


def save_motility_exam(folder: Path, captures: dict, info: dict) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    imgdir = folder / "imagens"
    imgdir.mkdir(exist_ok=True)
    files, meta = {}, {"programa": f"OculoDiag {VERSION}", "paciente": info.get("paciente"),
                       "data": info.get("data"), "vista": "examinador (OD à esquerda)", "posicoes": []}
    for i, pos in enumerate(POSITIONS):
        cap = captures.get(pos["key"])
        entry = {"ordem": i + 1, "posicao": pos["key"], "nome": pos["nome"], "registrado": cap is not None}
        if cap:
            entry["hora"] = cap["hora"]
            for e in EYES:
                fr = cap.get(e)
                if fr is None:
                    entry[e] = None
                    continue
                fn = f"imagens/{i + 1:02d}_{pos['key']}_{e}.png"
                cv2.imwrite(str(folder / fn), fr.img)
                files[(pos["key"], e)] = fn
                entry[e] = {"arquivo": fn, "quadro": fr.n, "t_monotonic": round(fr.t, 6),
                            "resolucao": [fr.img.shape[1], fr.img.shape[0]]}
            if cap.get("OD") is not None and cap.get("OE") is not None:
                entry["defasagem_OD_OE_ms"] = round(abs(cap["OD"].t - cap["OE"].t) * 1000, 1)
        meta["posicoes"].append(entry)
    meta["cameras"] = info.get("cameras", {})
    report = compose_motility_report(captures, info)
    report.save(folder / "resultado.png")
    report.save(folder / "resultado.pdf", resolution=150)
    (folder / "resultado.html").write_text(motility_html(captures, info, files), encoding="utf-8")
    (folder / "exame.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"png": folder / "resultado.png", "pdf": folder / "resultado.pdf",
            "html": folder / "resultado.html", "pasta": folder}


# ════════════════════════════════════════════════════════════════════════
#  Página de resultado da pupila
# ════════════════════════════════════════════════════════════════════════

PUPIL_EXTREMES = (("max", "Maior pupila", "menos luz · midríase"), ("min", "Menor pupila", "mais luz · miose"))
EYE_RGB = {"OD": (37, 99, 235), "OE": (234, 88, 12)}


def _pupil_chart(d: ImageDraw.ImageDraw, box: tuple, series: dict, extremes: dict, fonts: tuple):
    """Diâmetro (mm) × tempo, um traço por olho, com os extremos marcados."""
    x0, y0, x1, y1 = box
    f_s, f_tag = fonts
    muted, grid = (107, 114, 128), (226, 230, 236)
    vals = [v[1] for e in EYES for v in series.get(e, [])]
    ts = [v[0] for e in EYES for v in series.get(e, [])]
    if not vals:
        d.text((x0, (y0 + y1) / 2), "sem medidas", font=f_s, fill=muted)
        return
    lo, hi = math.floor(min(vals) - 0.3), math.ceil(max(vals) + 0.3)
    T = max(1.0, max(ts))
    px0, py0, px1, py1 = x0 + 46, y0 + 8, x1 - 10, y1 - 28
    X = lambda t: px0 + (px1 - px0) * t / T
    Y = lambda v: py1 - (py1 - py0) * (v - lo) / max(1e-6, hi - lo)
    step = 1 if hi - lo <= 8 else 2
    for v in range(int(lo), int(hi) + 1, step):
        d.line((px0, Y(v), px1, Y(v)), fill=grid, width=1)
        d.text((x0, Y(v) - 8), f"{v} mm", font=f_s, fill=muted)
    tstep = next(s for s in (1, 2, 5, 10, 15, 30, 60, 120, 300) if T / s <= 12)
    for k in range(0, int(T) + 1, tstep):
        d.line((X(k), py1, X(k), py1 + 4), fill=muted)
        d.text((X(k) - 8, py1 + 8), f"{k} s", font=f_s, fill=muted)
    for e in EYES:
        pts = series.get(e, [])
        seg = []
        for i, (t, mm) in enumerate((p[0], p[1]) for p in pts):
            if seg and t - pts[i - 1][0] > 0.5:       # falha de medida (piscada): quebra a linha
                if len(seg) > 1:
                    d.line(seg, fill=EYE_RGB[e], width=2)
                seg = []
            seg.append((X(t), Y(mm)))
        if len(seg) > 1:
            d.line(seg, fill=EYE_RGB[e], width=2)
        for kind, _, _ in PUPIL_EXTREMES:
            ex = extremes.get(e, {}).get(kind)
            if ex:
                cx, cy = X(ex["t"]), Y(ex["mm"])
                d.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), outline=EYE_RGB[e], width=2, fill="white")
    lx = px1 - 150
    for k, e in enumerate(EYES):
        d.line((lx + k * 75, py0 + 8, lx + k * 75 + 22, py0 + 8), fill=EYE_RGB[e], width=3)
        d.text((lx + k * 75 + 28, py0), e, font=f_tag, fill=(17, 24, 39))


def compose_pupil_report(exam: dict, info: dict) -> Image.Image:
    sample = next((x["frame"].img for e in EYES for x in exam["extremes"].get(e, {}).values() if x), None)
    ar = (sample.shape[1] / sample.shape[0]) if sample is not None else 4 / 3
    ih = 230 if ar < 1.1 else 190
    iw = int(round(ih * ar))
    margin, pad, gap, gx = 44, 18, 10, 24
    card_w = 2 * iw + gap + 2 * pad
    W = max(2 * margin + 2 * card_w + gx, 1100)
    card_w = (W - 2 * margin - gx) // 2
    header_h, card_h, chart_h, footer_h = 132, pad + 34 + ih + 84 + 54 + pad, 300, 54
    H = header_h + card_h + 24 + chart_h + footer_h + margin // 2

    ink, muted, card = (17, 24, 39), (107, 114, 128), (243, 245, 248)
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    f_title, f_h, f_b, f_s, f_tag = load_font(30, True), load_font(18, True), load_font(15), load_font(13), load_font(12, True)
    f_big = load_font(26, True)

    d.text((margin, 34), "Pupila · resposta à luz", font=f_title, fill=ink)
    d.text((margin, 78), "Vista do examinador: OD à esquerda, OE à direita · diâmetro relativo à íris "
                         f"(mm estimado com íris de {HVID_MM} mm)", font=f_s, fill=muted)
    right = [f"Paciente: {info.get('paciente') or '—'}", info.get("data", "")]
    for k, line in enumerate(right):
        tw = d.textlength(line, font=f_h if k == 0 else f_b)
        d.text((W - margin - tw, 34 + 28 * k), line, font=f_h if k == 0 else f_b, fill=ink if k == 0 else muted)
    d.line((margin, header_h - 18, W - margin, header_h - 18), fill=(226, 230, 236), width=2)

    for c, e in enumerate(EYES):
        x, y = margin + c * (card_w + gx), header_h
        d.rounded_rectangle((x, y, x + card_w, y + card_h), radius=14, fill=card)
        d.text((x + pad, y + pad - 2), f"{e} · {EYE_NAMES[e]}", font=f_h, fill=EYE_RGB[e])
        ex = exam["extremes"].get(e, {})
        tw_ = (card_w - 2 * pad - gap) // 2
        for k, (kind, title, sub) in enumerate(PUPIL_EXTREMES):
            ex_, ey_ = x + pad + k * (tw_ + gap), y + pad + 34
            r = ex.get(kind)
            if r is None:
                d.rounded_rectangle((ex_, ey_, ex_ + tw_, ey_ + ih), radius=6, fill=(225, 228, 233))
                msg = "sem medida"
                d.text((ex_ + (tw_ - d.textlength(msg, font=f_s)) / 2, ey_ + ih / 2 - 8), msg, font=f_s, fill=muted)
            else:
                im.paste(_fit(draw_pupil(r["frame"].img.copy(), r["reading"], label=False), tw_, ih), (ex_, ey_))
            ty = ey_ + ih + 8
            d.text((ex_, ty), title, font=f_b, fill=ink)
            d.text((ex_, ty + 20), sub, font=f_s, fill=muted)
            if r is not None:
                d.text((ex_, ty + 40), f"{r['mm']:.1f} mm", font=f_big, fill=ink)
                d.text((ex_ + d.textlength(f"{r['mm']:.1f} mm", font=f_big) + 10, ty + 50),
                       f"P/I {r['ratio']:.2f} · aos {r['t']:.1f} s", font=f_s, fill=muted)
        mx, mn = ex.get("max"), ex.get("min")
        if mx and mn:
            amp = mx["mm"] - mn["mm"]
            line = f"Amplitude {amp:.1f} mm  ·  constrição {100 * amp / mx['mm']:.0f}% do maior diâmetro"
        else:
            line = "Amplitude: sem medidas suficientes"
        d.text((x + pad, y + card_h - pad - 22), line, font=f_b, fill=ink)

    cy = header_h + card_h + 24
    d.rounded_rectangle((margin, cy, W - margin, cy + chart_h), radius=14, fill=card)
    d.text((margin + pad, cy + pad - 2), "Diâmetro ao longo da avaliação", font=f_h, fill=ink)
    _pupil_chart(d, (margin + pad, cy + 50, W - margin - pad, cy + chart_h - 8), exam["series"],
                 exam["extremes"], (f_s, f_tag))

    tech = "  ·  ".join(info.get("tecnico", []))
    d.text((margin, H - footer_h + 8), tech, font=f_s, fill=muted)
    d.text((margin, H - footer_h + 28),
           f"OculoDiag {VERSION} · gerado em {datetime.now():%d/%m/%Y %H:%M} · medidas quadro a quadro em "
           f"pupila.csv · fotos originais em imagens/", font=f_s, fill=muted)
    return im


def save_pupil_exam(folder: Path, exam: dict, info: dict) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    imgdir = folder / "imagens"
    imgdir.mkdir(exist_ok=True)
    meta = {"programa": f"OculoDiag {VERSION}", "paciente": info.get("paciente"), "data": info.get("data"),
            "duracao_s": round(exam["duracao"], 2), "vista": "examinador (OD à esquerda)",
            "regua": f"mm = (diâmetro pupila / diâmetro íris) × {HVID_MM} mm", "olhos": {}}
    for e in EYES:
        ent = {"quadros_medidos": len(exam["series"].get(e, []))}
        for kind, title, sub in PUPIL_EXTREMES:
            r = exam["extremes"].get(e, {}).get(kind)
            if r is None:
                ent[kind] = None
                continue
            name = "maior" if kind == "max" else "menor"
            raw, marked = f"imagens/{e}_{name}.png", f"imagens/{e}_{name}_marcada.png"
            cv2.imwrite(str(folder / raw), r["frame"].img)
            cv2.imwrite(str(folder / marked), draw_pupil(r["frame"].img.copy(), r["reading"]))
            rd = r["reading"]
            ent[kind] = {"descricao": f"{title} ({sub})", "mm_estimado": round(r["mm"], 2),
                         "razao_pupila_iris": round(r["ratio"], 4), "pupila_diam_px": round(2 * rd.r, 2),
                         "iris_diam_px": round(2 * rd.iris[2], 2), "t_s": round(r["t"], 3),
                         "imagem": raw, "imagem_marcada": marked}
        if ent.get("max") and ent.get("min"):
            ent["amplitude_mm"] = round(ent["max"]["mm_estimado"] - ent["min"]["mm_estimado"], 2)
        meta["olhos"][e] = ent
    meta["cameras"] = info.get("cameras", {})
    with open(folder / "pupila.csv", "w", encoding="utf-8") as f:
        f.write("olho,t_s,pupila_mm_estimado,razao_pupila_iris,pupila_diam_px,iris_diam_px,confianca\n")
        for e in EYES:
            for t, mm, ratio, dp, di, conf in exam["series"].get(e, []):
                f.write(f"{e},{t:.3f},{mm:.2f},{ratio:.4f},{dp:.2f},{di:.2f},{conf:.2f}\n")
    report = compose_pupil_report(exam, info)
    report.save(folder / "resultado.png")
    report.save(folder / "resultado.pdf", resolution=150)
    (folder / "exame.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"png": folder / "resultado.png", "pdf": folder / "resultado.pdf", "pasta": folder}


class PupilExam:
    """Estado de uma avaliação de pupila: série de medidas e, por olho, o
    maior e o menor diâmetro com a foto. Um extremo só é aceito pela
    MEDIANA das últimas medidas (~0,2 s), não por um quadro isolado -- uma
    medida errada solta não vira recorde. A foto guardada é a do quadro cuja
    medida está mais perto dessa mediana. Chamado das threads dos monitores."""

    WINDOW = 5
    MIN_CONF = 0.5

    def __init__(self):
        self.lock = threading.Lock()
        self.series = {e: [] for e in EYES}      # (t, mm, razão, diam_px, iris_px, conf)
        self.extremes = {e: {"max": None, "min": None} for e in EYES}
        self.recent = {e: deque(maxlen=self.WINDOW) for e in EYES}
        self.t0 = time.monotonic()
        self.t_end = None
        self.changed = 0                          # conta trocas de extremo (a tela redesenha)

    @property
    def duration(self) -> float:
        return (self.t_end or time.monotonic()) - self.t0

    def add(self, eye: str, fr: Frame, rd: PupilReading | None):
        if self.t_end is not None:
            return
        t = fr.t - self.t0
        with self.lock:
            rec = self.recent[eye]
            if rd is None or rd.conf < self.MIN_CONF:
                rec.clear()                       # piscada / perda: recomeça a janela
                return
            self.series[eye].append((t, rd.mm, rd.ratio, 2 * rd.r, 2 * rd.iris[2], rd.conf))
            rec.append((t, fr, rd))
            if len(rec) < self.WINDOW:
                return
            med = float(np.median([x[2].mm for x in rec]))
            t_, fr_, rd_ = min(rec, key=lambda x: abs(x[2].mm - med))
            for kind in ("max", "min"):
                cur = self.extremes[eye][kind]
                if cur is None or (med > cur["mm"] if kind == "max" else med < cur["mm"]):
                    self.extremes[eye][kind] = {"mm": med, "ratio": med / HVID_MM, "t": t_,
                                                "frame": Frame(fr_.img.copy(), fr_.t, fr_.t_drv, fr_.n),
                                                "reading": rd_}
                    self.changed += 1

    def finish(self):
        self.t_end = time.monotonic()

    def snapshot(self) -> dict:
        with self.lock:
            return {"series": {e: list(v) for e, v in self.series.items()},
                    "extremes": {e: dict(v) for e, v in self.extremes.items()},
                    "duracao": self.duration}


# ════════════════════════════════════════════════════════════════════════
#  Interface
# ════════════════════════════════════════════════════════════════════════

C = {
    "bg": "#0e1116", "panel": "#141920", "card": "#1a2029", "card2": "#232a35", "card3": "#2d3543",
    "border": "#2a3140", "text": "#e9edf3", "muted": "#8b95a7", "faint": "#586274",
    "accent": "#4c8dff", "accent_h": "#6ea3ff", "rec": "#ff4d5e", "rec_h": "#ff6d7b",
    "ok": "#34d399", "ok_h": "#5fe0b0", "warn": "#f5b041",
    "dis_bg": "#181d25", "dis_fg": "#475064", "video": "#06080b",
}


def rounded_rect(cv, x1, y1, x2, y2, r, **kw):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, splinesteps=16, **kw)


if tk is not None:

    class RoundButton(tk.Canvas):
        KINDS = {  # fundo, texto, fundo com o mouse em cima
            "primary": ("accent", "#ffffff", "accent_h"),
            "danger": ("rec", "#ffffff", "rec_h"),
            "success": ("ok", "#05261a", "ok_h"),
            "neutral": ("card2", "text", "card3"),
            "ghost": ("panel", "muted", "card2"),
        }

        def __init__(self, parent, text, command=None, kind="neutral", width=None, height=38,
                     hint=None, font=None, hint_font=None, radius=10):
            self.bgc = parent.cget("bg")
            self.text, self.command, self.kind, self.hint = text, command, kind, hint
            self.font, self.hint_font, self.radius = font, hint_font or font, radius
            if width is None:
                width = self.font.measure(text) + 34
                if hint:
                    width += self.hint_font.measure(hint) + 22
            super().__init__(parent, width=width, height=height, bg=self.bgc, highlightthickness=0,
                             bd=0, cursor="hand2")
            self.enabled, self.hover, self.pressed, self.selected = True, False, False, False
            self.bind("<Enter>", lambda e: self._set(hover=True))
            self.bind("<Leave>", lambda e: self._set(hover=False, pressed=False))
            self.bind("<ButtonPress-1>", lambda e: self._set(pressed=True) if self.enabled else None)
            self.bind("<ButtonRelease-1>", self._release)
            self.bind("<Configure>", lambda e: self._draw())
            self._draw()

        def _set(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)
            self._draw()

        def _release(self, e):
            was = self.pressed
            self._set(pressed=False)
            if was and self.enabled and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
                if self.command:
                    self.command()

        def _colors(self):
            if not self.enabled:
                return C["dis_bg"], C["dis_fg"]
            kind = "primary" if self.selected else self.kind
            bg, fg, hv = self.KINDS[kind]
            return C[hv] if self.hover else C[bg], C.get(fg, fg)

        def _draw(self):
            self.delete("all")
            w = self.winfo_width() if self.winfo_width() > 1 else int(self["width"])
            h = self.winfo_height() if self.winfo_height() > 1 else int(self["height"])
            bg, fg = self._colors()
            o = 1 if self.pressed else 0
            rounded_rect(self, 1, 1 + o, w - 1, h - 1 + o, self.radius, fill=bg, outline="")
            if self.hint:
                tw = self.font.measure(self.text)
                hw = self.hint_font.measure(self.hint) + 12
                x0 = (w - (tw + 10 + hw)) / 2
                self.create_text(x0, h / 2 + o, text=self.text, anchor="w", fill=fg, font=self.font)
                hx = x0 + tw + 10
                rounded_rect(self, hx, h / 2 - 10 + o, hx + hw, h / 2 + 10 + o, 5, fill="", outline=fg)
                self.create_text(hx + hw / 2, h / 2 + o, text=self.hint, fill=fg, font=self.hint_font)
            else:
                self.create_text(w / 2, h / 2 + o, text=self.text, fill=fg, font=self.font)

        def set_enabled(self, en: bool):
            en = bool(en)
            if en != self.enabled:
                self.enabled = en
                self.configure(cursor="hand2" if en else "arrow")
                self._draw()

        def set_selected(self, sel: bool):
            if bool(sel) != self.selected:
                self.selected = bool(sel)
                self._draw()

        def set_text(self, text: str):
            if text != self.text:
                self.text = text
                self._draw()

    class Segmented(tk.Frame):
        def __init__(self, parent, options, command, font, height=34, width=None):
            super().__init__(parent, bg=parent.cget("bg"))
            self.command, self.value, self.btns = command, None, {}
            for key, label in options:
                b = RoundButton(self, label, command=lambda k=key: self._click(k), kind="ghost",
                                height=height, font=font, width=width)
                b.pack(side="left", padx=2)
                self.btns[key] = b

        def _click(self, k):
            if k != self.value:
                self.command(k)

        def set(self, k):
            self.value = k
            for key, b in self.btns.items():
                b.set_selected(key == k)

        def set_enabled(self, en):
            for b in self.btns.values():
                b.set_enabled(en)

    class Slider(tk.Canvas):
        def __init__(self, parent, from_, to, value, command=None, width=300, height=34):
            super().__init__(parent, width=width, height=height, bg=parent.cget("bg"),
                             highlightthickness=0, cursor="hand2")
            self.from_, self.to, self.value, self.command, self.enabled = from_, to, value, command, True
            self.bind("<Button-1>", self._drag)
            self.bind("<B1-Motion>", self._drag)
            self.bind("<MouseWheel>", lambda e: self._step(1 if e.delta > 0 else -1))
            self.bind("<Button-4>", lambda e: self._step(1))
            self.bind("<Button-5>", lambda e: self._step(-1))
            self.bind("<Configure>", lambda e: self._draw())

        def _span(self):
            w = self.winfo_width() if self.winfo_width() > 1 else int(self["width"])
            return 12, w - 12

        def _draw(self):
            self.delete("all")
            h = self.winfo_height() if self.winfo_height() > 1 else int(self["height"])
            x0, x1 = self._span()
            cy = h / 2
            frac = 0 if self.to == self.from_ else (self.value - self.from_) / (self.to - self.from_)
            xv = x0 + frac * (x1 - x0)
            col = C["accent"] if self.enabled else C["faint"]
            self.create_line(x0, cy, x1, cy, width=6, fill=C["card3"], capstyle="round")
            self.create_line(x0, cy, xv, cy, width=6, fill=col, capstyle="round")
            self.create_oval(xv - 9, cy - 9, xv + 9, cy + 9, fill="#ffffff" if self.enabled else C["card3"],
                             outline=col, width=2)

        def _apply(self, v):
            v = int(round(min(self.to, max(self.from_, v))))
            if v != self.value:
                self.value = v
                self._draw()
                if self.command:
                    self.command(v)

        def _drag(self, e):
            if self.enabled:
                x0, x1 = self._span()
                self._apply(self.from_ + (e.x - x0) / max(1, x1 - x0) * (self.to - self.from_))

        def _step(self, d):
            if self.enabled:
                self._apply(self.value + d)

        def set(self, v):
            self.value = int(round(min(self.to, max(self.from_, v))))
            self._draw()

        def set_range(self, from_, to):
            self.from_, self.to = from_, to
            self.set(self.value)

        def set_enabled(self, en):
            self.enabled = bool(en)
            self.configure(cursor="hand2" if en else "arrow")
            self._draw()

    class Preview(tk.Frame):
        """Imagem ao vivo de um olho, reescalada para caber."""

        def __init__(self, parent, eye, fonts, min_w=320, min_h=240):
            super().__init__(parent, bg=C["card"], highlightthickness=2,
                             highlightbackground=C["card"], highlightcolor=C["card"])
            head = tk.Frame(self, bg=C["card"])
            head.pack(fill="x", padx=12, pady=(10, 6))
            tk.Label(head, text=ROLE_TITLES[eye], font=fonts["h2"], fg=C["text"], bg=C["card"]).pack(side="left")
            tk.Label(head, text=EYE_NAMES[eye], font=fonts["small"], fg=C["muted"], bg=C["card"]).pack(
                side="left", padx=(8, 0), pady=(3, 0))
            self.info = tk.Label(head, text="", font=fonts["mono"], fg=C["muted"], bg=C["card"])
            self.info.pack(side="right")
            self.cv = tk.Canvas(self, bg=C["video"], highlightthickness=0, width=min_w, height=min_h)
            self.cv.pack(fill="both", expand=True, padx=12, pady=(0, 12))
            self.fonts = fonts
            self.photo = None
            self.img_id = None
            self.last = (None, None)
            self.map = None          # (ox, oy, largura, altura) da imagem dentro do canvas
            self.cv.bind("<Configure>", lambda e: self.invalidate())

        def invalidate(self):
            self.last = (None, None)

        def to_norm(self, x, y):
            """Canvas -> fração do quadro (limitada a 0..1)."""
            if not self.map:
                return None
            ox, oy, dw, dh = self.map
            return min(max((x - ox) / dw, 0.0), 1.0), min(max((y - oy) / dh, 0.0), 1.0)

        def to_canvas(self, u, v):
            ox, oy, dw, dh = self.map
            return ox + u * dw, oy + v * dh

        def show(self, fr: Frame | None, info: str = "", msg: str = "", edit=None, pupil=False):
            """`edit` = None (normal) ou {"rect": [x,y,w,h] | None}: mostra o
            quadro INTEIRO com o recorte desenhado por cima. `pupil` = False
            ou a leitura (PupilReading | None) a desenhar sobre o quadro."""
            if self.info.cget("text") != info:
                self.info.configure(text=info)
            cw, ch = max(2, self.cv.winfo_width()), max(2, self.cv.winfo_height())
            if fr is None:
                if self.last != ("msg", msg):
                    self.cv.delete("img", "msg")
                    self.img_id = None
                    self.cv.create_text(cw / 2, ch / 2, text=msg, fill=C["muted"], font=self.fonts["body"],
                                        tags="msg", justify="center")
                    self.last = ("msg", msg)
                return
            rect = tuple(edit["rect"]) if edit and edit.get("rect") else None
            key = (fr.n, (cw, ch), edit is not None, rect, id(pupil) if pupil is not False else False)
            if self.last == key and self.img_id is not None:
                return
            src = fr.full if (edit is not None and fr.full is not None) else fr.img
            if pupil is not False and edit is None:
                src = draw_pupil(src.copy(), pupil, label=False)
            h, w = src.shape[:2]
            s = min(cw / w, ch / h)
            nw, nh = max(1, int(w * s)), max(1, int(h * s))
            small = cv2.resize(src, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
            self.map = ((cw - nw) / 2, (ch - nh) / 2, nw, nh)
            if rect is not None:            # escurece o que fica fora do recorte
                px = roi_pixels(rect, nw, nh)
                dim = (small * 0.35).astype(np.uint8)
                dim[px[1]:px[3], px[0]:px[2]] = small[px[1]:px[3], px[0]:px[2]]
                small = dim
            self.photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)))
            if self.img_id is None:
                self.cv.delete("msg")
                self.img_id = self.cv.create_image(cw / 2, ch / 2, image=self.photo, tags="img")
            else:
                self.cv.coords(self.img_id, cw / 2, ch / 2)
                self.cv.itemconfigure(self.img_id, image=self.photo)
            self._draw_roi(edit, rect, w, h)
            self.cv.tag_raise("badge")
            self.last = key

        def _draw_roi(self, edit, rect, W, H):
            self.cv.delete("roi")
            if edit is None:
                return
            ox, oy, dw, dh = self.map
            if rect is not None:
                x0, y0 = self.to_canvas(rect[0], rect[1])
                x1, y1 = self.to_canvas(rect[0] + rect[2], rect[1] + rect[3])
                self.cv.create_rectangle(x0, y0, x1, y1, outline=C["accent"], width=2, tags="roi")
                for gx in (1 / 3, 2 / 3):   # terços: ajudam a centralizar o olho
                    self.cv.create_line(x0 + (x1 - x0) * gx, y0, x0 + (x1 - x0) * gx, y1,
                                        fill=C["accent"], dash=(3, 5), tags="roi")
                    self.cv.create_line(x0, y0 + (y1 - y0) * gx, x1, y0 + (y1 - y0) * gx,
                                        fill=C["accent"], dash=(3, 5), tags="roi")
                for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
                    self.cv.create_rectangle(cx - 6, cy - 6, cx + 6, cy + 6, fill="#ffffff",
                                             outline=C["accent"], width=2, tags="roi")
                label = roi_text(rect, W, H)
            else:
                label = "arraste sobre a imagem para marcar o olho"
            f = self.fonts["small"]
            tw = f.measure(label) + 20
            cx = ox + dw / 2
            rounded_rect(self.cv, cx - tw / 2, oy + dh - 34, cx + tw / 2, oy + dh - 8, 8,
                         fill=C["card3"], outline="", tags="roi")
            self.cv.create_text(cx, oy + dh - 21, text=label, fill=C["text"], font=f, tags="roi")

        def set_highlight(self, color=None):
            c = color or C["card"]
            if self.cget("highlightbackground") != c:
                self.configure(highlightbackground=c, highlightcolor=c)

        def set_badge(self, text=None, color=None):
            self.cv.delete("badge")
            if text:
                f = self.fonts["btn"]
                w = f.measure(text) + 20
                rounded_rect(self.cv, 10, 10, 10 + w, 38, 8, fill=color or C["card3"], outline="", tags="badge")
                self.cv.create_text(10 + w / 2, 24, text=text, fill="#ffffff", font=f, tags="badge")

    class GazeDiagram(tk.Canvas):
        """Mostra a posição pedida na vista do examinador."""

        def __init__(self, parent, fonts, size=150):
            super().__init__(parent, width=size, height=size, bg=parent.cget("bg"), highlightthickness=0)
            self.size, self.fonts = size, fonts

        def draw(self, pos):
            self.delete("all")
            s = self.size
            rounded_rect(self, 2, 2, s - 2, s - 2, 16, fill=C["card2"], outline="")
            x0 = 38
            step = (s - 2 * x0) / 2

            def xy(c, r):
                return x0 + c * step, x0 + r * step

            for r in range(3):
                for c in range(3):
                    x, y = xy(c, r)
                    self.create_oval(x - 4, y - 4, x + 4, y + 4, fill=C["faint"], outline="")
            self.create_text(14, s / 2, text="D", fill=C["muted"], font=self.fonts["btn"])
            self.create_text(s - 14, s / 2, text="E", fill=C["muted"], font=self.fonts["btn"])
            cx, cy = xy(1, 1)
            if pos["vec"] is None:  # perto: convergência
                for sx in (-1, 1):
                    self.create_line(cx + sx * 34, cy + 22, cx + sx * 12, cy + 4, fill=C["accent"], width=3,
                                     arrow="last", arrowshape=(9, 10, 4))
                self.create_oval(cx - 8, cy - 8, cx + 8, cy + 8, fill=C["accent"], outline="#ffffff", width=2)
                return
            vx, vy = pos["vec"]
            tx, ty = xy(1 + vx, 1 + vy)
            if (vx, vy) != (0, 0):
                self.create_line(cx, cy, tx, ty, fill=C["accent"], width=3, arrow="last", arrowshape=(10, 12, 4))
                self.create_oval(tx - 9, ty - 9, tx + 9, ty + 9, fill=C["accent"], outline="#ffffff", width=2)
            else:
                self.create_oval(cx - 14, cy - 14, cx + 14, cy + 14, outline=C["accent"], width=2)
                self.create_oval(cx - 8, cy - 8, cx + 8, cy + 8, fill=C["accent"], outline="#ffffff", width=2)

    class Toast:
        def __init__(self, root, font):
            self.root = root
            self.lbl = tk.Label(root, font=font, bg=C["card3"], fg=C["text"], padx=18, pady=10)
            self.job = None

        def show(self, msg, kind="info", ms=3800):
            col = {"info": C["text"], "ok": C["ok"], "warn": C["warn"], "erro": C["rec"]}[kind]
            self.lbl.configure(text=msg, fg=col)
            self.lbl.place(relx=0.5, rely=1.0, y=-44, anchor="s")
            self.lbl.lift()
            if self.job:
                self.root.after_cancel(self.job)
            self.job = self.root.after(ms, self.lbl.place_forget)

    # ─── telas ──────────────────────────────────────────────────────────

    class Screen(tk.Frame):
        def __init__(self, parent, app):
            super().__init__(parent, bg=C["bg"])
            self.app = app

        def on_show(self):
            pass

        def on_hide(self):
            pass

        def tick(self):
            pass

        def on_key(self, key: str) -> bool:
            return False

        def can_leave(self):
            return True, ""

        def cam_view(self, eye):
            """(frame, info, mensagem) para um Preview."""
            cam = self.app.rig.cams[eye]
            fr = cam.latest_frame()
            if fr is not None:
                h, w = fr.img.shape[:2]
                return fr, f"{cam.fps:5.1f} fps · {w}×{h}", ""
            st = cam.status
            if self.app.rig.busy or st == "abrindo":
                msg = "abrindo câmera…"
            elif st == "sem câmera":
                msg = ("câmera extra desligada\n(opcional · escolha com ◀ ▶ na tela Câmeras)" if eye == EXTRA
                       else "nenhuma câmera atribuída\n(use ◀ ▶ na tela Câmeras)")
            else:
                msg = f"sem imagem\n({st})"
            return None, "", msg

    class CamerasScreen(Screen):
        def __init__(self, parent, app):
            super().__init__(parent, app)
            F = app.fonts
            info = tk.Frame(self, bg=C["card"])
            info.pack(fill="x", padx=20, pady=(16, 12))
            tk.Label(info, text="Identifique e oriente cada câmera", font=F["h2"], fg=C["text"],
                     bg=C["card"]).pack(anchor="w", padx=16, pady=(12, 4))
            steps = [
                ("1", "Peça ao paciente que olhe para CIMA → a pupila deve SUBIR na tela. Se não, gire 90° com ↺ / ↻."),
                ("2", "Peça que olhe para a DIREITA DELE → a pupila deve ir para a ESQUERDA da tela "
                      "(vista de frente). Se for para o outro lado, use Espelhar."),
                ("3", "O olho direito (OD) fica à esquerda da tela. Se estiverem trocados, use ⇄ Trocar."),
                ("4", "Recorte (✂) a região de cada olho para centralizá-lo. O recorte vale para a "
                      "motilidade, a pupila e a gravação."),
                ("5", "Câmera extra (opcional, ex. rosto ou cena): escolha com ◀ ▶. Ela só é gravada "
                      "na tela Gravação, junto com os olhos."),
            ]
            for n, s in steps:
                row = tk.Frame(info, bg=C["card"])
                row.pack(fill="x", padx=16, pady=2)
                tk.Label(row, text=n, font=F["btn"], fg=C["accent"], bg=C["card"], width=2).pack(side="left", anchor="n")
                tk.Label(row, text=s, font=F["body"], fg=C["muted"], bg=C["card"], justify="left",
                         anchor="w").pack(side="left", fill="x")
            tk.Frame(info, bg=C["card"], height=10).pack()

            row = tk.Frame(self, bg=C["bg"])
            row.pack(fill="both", expand=True, padx=20)
            for c in (0, 2, 3):
                row.columnconfigure(c, weight=1, uniform="cam")
            row.rowconfigure(0, weight=1)
            self.previews, self.dev_labels, self.flip_btns = {}, {}, {}
            self.roi_btn, self.roi_clear_btn, self.roi_rows = {}, {}, {}
            self.editing = None        # olho cujo recorte está sendo editado
            self.edit_rect = None
            self._drag = None
            for eye, col in (("OD", 0), ("OE", 2), (EXTRA, 3)):
                box = tk.Frame(row, bg=C["bg"])
                box.grid(row=0, column=col, sticky="nsew", padx=(16, 0) if eye == EXTRA else 0)
                p = Preview(box, eye, F, min_w=260, min_h=120)
                p.pack(fill="both", expand=True)
                self.previews[eye] = p
                ctl = tk.Frame(box, bg=C["bg"])
                ctl.pack(fill="x", pady=(10, 0))
                RoundButton(ctl, "↺", lambda e=eye: self.rotate(e, -90), font=F["title"], width=42).pack(side="left")
                RoundButton(ctl, "↻", lambda e=eye: self.rotate(e, 90), font=F["title"], width=42).pack(
                    side="left", padx=6)
                fb = RoundButton(ctl, "Espelhar", lambda e=eye: self.flip(e), font=F["btn"])
                fb.pack(side="left")
                self.flip_btns[eye] = fb
                sel = tk.Frame(box, bg=C["bg"])
                sel.pack(fill="x", pady=(8, 0))
                RoundButton(sel, "◀", lambda e=eye: self.cycle(e, -1), font=F["btn"], width=38).pack(side="left")
                lbl = tk.Label(sel, text="", font=F["small"], fg=C["muted"], bg=C["bg"], width=24)
                lbl.pack(side="left", padx=6, fill="x", expand=True)
                self.dev_labels[eye] = lbl
                RoundButton(sel, "▶", lambda e=eye: self.cycle(e, 1), font=F["btn"], width=38).pack(side="left")
                if eye == EXTRA:      # sem recorte: a extra é gravada inteira
                    continue

                # linha do recorte: modo normal / modo de edição
                rr = tk.Frame(box, bg=C["bg"])
                rr.pack(fill="x", pady=(8, 0))
                normal, editing = tk.Frame(rr, bg=C["bg"]), tk.Frame(rr, bg=C["bg"])
                self.roi_btn[eye] = RoundButton(normal, "✂  Recortar região", lambda e=eye: self.roi_start(e),
                                                font=F["btn"])
                self.roi_btn[eye].pack(side="left")
                self.roi_clear_btn[eye] = RoundButton(normal, "Quadro inteiro", lambda e=eye: self.roi_clear(e),
                                                      font=F["btn"])
                self.roi_clear_btn[eye].pack(side="left", padx=6)
                RoundButton(editing, "✓  Aplicar", self.roi_apply, kind="primary", hint="Enter",
                            font=F["btn"], hint_font=F["hint"]).pack(side="left")
                RoundButton(editing, "Cancelar", self.roi_cancel, hint="Esc", font=F["btn"],
                            hint_font=F["hint"]).pack(side="left", padx=6)
                RoundButton(editing, "Limpar", self.roi_reset_edit, kind="ghost", font=F["btn"]).pack(side="left")
                normal.pack(fill="x")
                self.roi_rows[eye] = (normal, editing)

                p.cv.bind("<ButtonPress-1>", lambda ev, e=eye: self._roi_press(e, ev))
                p.cv.bind("<B1-Motion>", lambda ev, e=eye: self._roi_motion(e, ev))
                p.cv.bind("<ButtonRelease-1>", lambda ev, e=eye: self._roi_release(e, ev))

            mid = tk.Frame(row, bg=C["bg"])
            mid.grid(row=0, column=1, padx=16)
            self.swap_btn = RoundButton(mid, "⇄", self.swap, kind="primary", width=58, height=58,
                                        font=F["h1"], radius=29)
            self.swap_btn.pack()
            tk.Label(mid, text="Trocar\nOD ↔ OE", font=F["small"], fg=C["muted"], bg=C["bg"]).pack(pady=(6, 0))

            bottom = tk.Frame(self, bg=C["bg"])
            bottom.pack(fill="x", padx=20, pady=14)
            RoundButton(bottom, "Procurar / reconectar câmeras", self.reconnect, font=F["btn"]).pack(side="left")
            RoundButton(bottom, "Alterar pasta", self.choose_dir, font=F["btn"]).pack(side="right")
            self.dir_lbl = tk.Label(bottom, text="", font=F["small"], fg=C["muted"], bg=C["bg"])
            self.dir_lbl.pack(side="right", padx=10)

        # -- recorte ----------------------------------------------------------
        def roi_start(self, eye):
            if self.editing and self.editing != eye:
                self.roi_cancel()
            if self.app.rig.cams[eye].latest_frame() is None:
                self.app.toast.show("Essa câmera está sem imagem.", "warn")
                return
            self.editing = eye
            cam = self.app.rig.cams[eye]       # o editor trabalha no quadro do modo atual
            self.edit_rect = roi_to_frame(cam.roi, cam.mode_key)
            normal, editing = self.roi_rows[eye]
            normal.pack_forget()
            editing.pack(fill="x")
            self.previews[eye].cv.configure(cursor="crosshair")
            self.previews[eye].invalidate()

        def _roi_end(self):
            eye = self.editing
            if eye is None:
                return
            normal, editing = self.roi_rows[eye]
            editing.pack_forget()
            normal.pack(fill="x")
            self.previews[eye].cv.configure(cursor="")
            self.previews[eye].cv.delete("roi")
            self.previews[eye].invalidate()
            self.editing, self.edit_rect, self._drag = None, None, None

        def roi_apply(self):
            if self.editing is None:
                return
            eye = self.editing
            rect = roi_from_frame(clean_roi(self.edit_rect), self.app.rig.cams[eye].mode_key)
            self.app.rig.set_roi(eye, rect)
            self._roi_end()
            self.app.toast.show(f"{eye}: recorte aplicado." if rect else f"{eye}: quadro inteiro.", "ok", 1800)

        def roi_cancel(self):
            self._roi_end()

        def roi_reset_edit(self):
            self.edit_rect = None
            if self.editing:
                self.previews[self.editing].invalidate()

        def roi_clear(self, eye):
            if self.app.rig.cams[eye].roi is not None:
                self.app.rig.set_roi(eye, None)
                self.app.toast.show(f"{eye}: quadro inteiro.", "ok", 1600)

        def _roi_press(self, eye, ev):
            if self.editing != eye:
                return
            p = self.previews[eye]
            n = p.to_norm(ev.x, ev.y)
            if n is None:
                return
            r = self.edit_rect
            self._drag = {"prev": list(r) if r else None, "mode": "new", "anchor": n}
            if r:
                x0, y0, x1, y1 = r[0], r[1], r[0] + r[2], r[1] + r[3]
                corners = {(x0, y0): (x1, y1), (x1, y0): (x0, y1), (x0, y1): (x1, y0), (x1, y1): (x0, y0)}
                for (cx, cy), opp in corners.items():
                    px, py = p.to_canvas(cx, cy)
                    if abs(px - ev.x) <= 12 and abs(py - ev.y) <= 12:
                        self._drag.update(mode="resize", anchor=opp)
                        return
                if x0 <= n[0] <= x1 and y0 <= n[1] <= y1:
                    self._drag.update(mode="move", off=(n[0] - x0, n[1] - y0))

        def _roi_motion(self, eye, ev):
            if self.editing != eye or not self._drag:
                return
            p = self.previews[eye]
            n = p.to_norm(ev.x, ev.y)
            if n is None:
                return
            d = self._drag
            if d["mode"] == "move":
                w, h = self.edit_rect[2], self.edit_rect[3]
                x = min(max(n[0] - d["off"][0], 0.0), 1.0 - w)
                y = min(max(n[1] - d["off"][1], 0.0), 1.0 - h)
                self.edit_rect = [x, y, w, h]
            else:
                ax, ay = d["anchor"]
                self.edit_rect = [min(ax, n[0]), min(ay, n[1]), abs(n[0] - ax), abs(n[1] - ay)]
            p.invalidate()

        def _roi_release(self, eye, ev):
            if self.editing != eye or not self._drag:
                return
            r = self.edit_rect
            if r is None or r[2] < ROI_MIN or r[3] < ROI_MIN:   # clique solto / área minúscula
                self.edit_rect = self._drag["prev"]
            self._drag = None
            self.previews[eye].invalidate()

        def on_key(self, key):
            if self.editing and key in ("Return", "KP_Enter"):
                self.roi_apply()
                return True
            if self.editing and key == "Escape":
                self.roi_cancel()
                return True
            return False

        def on_hide(self):
            self._roi_end()

        def rotate(self, eye, d):
            if self.editing:
                self.roi_cancel()
            cam = self.app.rig.cams[eye]
            self.app.rig.set_orientation(eye, rotation=cam.rotation + d)

        def flip(self, eye):
            if self.editing:
                self.roi_cancel()
            self.app.rig.set_orientation(eye, flip=not self.app.rig.cams[eye].flip)

        def cycle(self, eye, step):
            if self.app.rig.busy:
                return
            self.roi_cancel()
            rig = self.app.rig
            if eye == EXTRA:
                eyes_idx = {rig.cams[e].index for e in EYES}
                if not any(c["index"] not in eyes_idx for c in rig.available):
                    self.app.toast.show("Nenhuma câmera livre para a extra (as outras estão nos olhos).", "warn")
                    return
            elif len(rig.available) < 2 and rig.cams[eye].index is not None:
                self.app.toast.show("Só uma câmera encontrada.", "warn")
            rig.cycle(eye, step)

        def swap(self):
            if not self.app.rig.busy:
                self.roi_cancel()
                self.app.rig.swap()
                self.app.toast.show("OD e OE trocados.", "ok", 1800)

        def reconnect(self):
            if not self.app.rig.busy:
                self.app.rig.start(self.app.rig.mode_key or MOTILITY_MODE)
                self.app.toast.show("Procurando câmeras…", "info", 2000)

        def choose_dir(self):
            d = filedialog.askdirectory(initialdir=str(Path(self.app.cfg["saida"]).expanduser()),
                                        title="Pasta onde os exames serão salvos")
            if d:
                self.app.cfg["saida"] = d
                self.app.save_cfg()

        def tick(self):
            rig = self.app.rig
            for eye in ROLES:
                fr, info, msg = self.cam_view(eye)
                cam = rig.cams[eye]
                if fr is not None:          # curto: três colunas; a resolução está na barra de status
                    info = f"{cam.fps:4.0f} fps"
                extra = f" · {cam.rotation}°" + (" · espelhado" if cam.flip else "")
                edit = {"rect": self.edit_rect} if self.editing == eye else None
                if edit is not None and fr is not None and fr.full is not None:
                    info = f"{cam.fps:5.1f} fps · editando recorte"
                elif fr is not None and cam.roi:
                    extra += " · ✂"
                self.previews[eye].show(fr, info + extra if fr is not None else info, msg, edit=edit)
                if eye in self.roi_clear_btn:
                    self.roi_clear_btn[eye].set_enabled(cam.roi is not None)
                self.dev_labels[eye].configure(text=rig.label_for(cam.index) if cam.index is not None
                                               else ("nenhuma" if eye == EXTRA else "—"))
                self.flip_btns[eye].set_selected(cam.flip)
            self.swap_btn.set_enabled(not rig.busy)
            self.dir_lbl.configure(text=f"Salvar em: {self.app.cfg['saida']}")

    class MotilityScreen(Screen):
        def __init__(self, parent, app):
            super().__init__(parent, app)
            F = app.fonts
            self.caps: dict = {}
            self.order: list = []
            self.cur = 0
            self.folder: Path | None = None
            self.saving = False
            self.result_img = None
            self.result_photo = None
            self.columnconfigure(1, weight=1)
            self.rowconfigure(0, weight=1)

            # barra lateral: sequência
            side = tk.Frame(self, bg=C["panel"], width=290)
            side.grid(row=0, column=0, sticky="ns")
            side.pack_propagate(False)
            hd = tk.Frame(side, bg=C["panel"])
            hd.pack(fill="x", padx=18, pady=(18, 10))
            tk.Label(hd, text="Sequência", font=F["h2"], fg=C["text"], bg=C["panel"]).pack(side="left")
            self.count_lbl = tk.Label(hd, text="", font=F["mono"], fg=C["muted"], bg=C["panel"])
            self.count_lbl.pack(side="right")
            self.rows = []
            for i, pos in enumerate(POSITIONS):
                r = tk.Frame(side, bg=C["panel"], cursor="hand2")
                r.pack(fill="x", padx=10, pady=1)
                dot = tk.Canvas(r, width=16, height=16, bg=C["panel"], highlightthickness=0)
                dot.pack(side="left", padx=(8, 8), pady=8)
                num = tk.Label(r, text=f"{i + 1:>2}", font=F["mono"], fg=C["faint"], bg=C["panel"])
                num.pack(side="left")
                name = tk.Label(r, text=pos["nome"], font=F["body"], fg=C["text"], bg=C["panel"], anchor="w")
                name.pack(side="left", padx=(8, 0), fill="x", expand=True)
                for w in (r, dot, num, name):
                    w.bind("<Button-1>", lambda e, k=i: self.select(k))
                self.rows.append((r, dot, num, name))
            btns = tk.Frame(side, bg=C["panel"])
            btns.pack(side="bottom", fill="x", padx=16, pady=16)
            self.finish_btn = RoundButton(btns, "Gerar resultado", self.finalize, kind="success",
                                          font=F["btn"], width=256)
            self.finish_btn.pack(fill="x", pady=(0, 6))
            self.undo_btn = RoundButton(btns, "Refazer anterior", self.redo_previous, hint="⌫",
                                        font=F["btn"], hint_font=F["hint"], width=256)
            self.undo_btn.pack(fill="x", pady=(0, 6))
            RoundButton(btns, "Novo exame", self.reset, kind="ghost", font=F["btn"], width=256).pack(fill="x")

            # área principal: captura / resultado
            main = tk.Frame(self, bg=C["bg"])
            main.grid(row=0, column=1, sticky="nsew")
            main.rowconfigure(0, weight=1)
            main.columnconfigure(0, weight=1)
            self.capture_view = cv_ = tk.Frame(main, bg=C["bg"])
            self.result_view = rv = tk.Frame(main, bg=C["bg"])
            for f in (cv_, rv):
                f.grid(row=0, column=0, sticky="nsew")

            guide = tk.Frame(cv_, bg=C["card"])
            guide.pack(fill="x", padx=20, pady=(16, 12))
            self.diagram = GazeDiagram(guide, F, size=150)
            self.diagram.pack(side="left", padx=14, pady=14)
            txt = tk.Frame(guide, bg=C["card"])
            txt.pack(side="left", fill="both", expand=True, pady=14)
            self.step_lbl = tk.Label(txt, text="", font=F["small"], fg=C["muted"], bg=C["card"], anchor="w")
            self.step_lbl.pack(fill="x")
            self.name_lbl = tk.Label(txt, text="", font=F["h1"], fg=C["text"], bg=C["card"], anchor="w")
            self.name_lbl.pack(fill="x", pady=(2, 0))
            self.cmd_lbl = tk.Label(txt, text="", font=F["title"], fg=C["accent"], bg=C["card"], anchor="w")
            self.cmd_lbl.pack(fill="x", pady=(4, 0))
            self.descr_lbl = tk.Label(txt, text="", font=F["small"], fg=C["muted"], bg=C["card"], anchor="w")
            self.descr_lbl.pack(fill="x", pady=(4, 0))
            self.cap_btn = RoundButton(guide, "Registrar", self.capture, kind="primary", hint="Espaço",
                                       font=F["title"], hint_font=F["hint"], height=56, width=230, radius=14)
            self.cap_btn.pack(side="right", padx=18)

            pv = tk.Frame(cv_, bg=C["bg"])
            pv.pack(fill="both", expand=True, padx=20)
            pv.columnconfigure(0, weight=1, uniform="p")
            pv.columnconfigure(1, weight=1, uniform="p")
            pv.rowconfigure(0, weight=1)
            self.previews = {}
            for k, eye in enumerate(EYES):
                p = Preview(pv, eye, F)
                p.grid(row=0, column=k, sticky="nsew", padx=(0, 8) if k == 0 else (8, 0))
                self.previews[eye] = p
            tk.Label(cv_, text="Vista do examinador · OD à esquerda, OE à direita · "
                                "↑ ↓ ou clique na lista para escolher a posição",
                     font=F["small"], fg=C["faint"], bg=C["bg"]).pack(pady=(8, 12))

            # resultado
            top = tk.Frame(rv, bg=C["bg"])
            top.pack(fill="x", padx=20, pady=(16, 10))
            tk.Label(top, text="Resultado salvo", font=F["h1"], fg=C["text"], bg=C["bg"]).pack(side="left")
            RoundButton(top, "Novo exame", self.reset, kind="primary", font=F["btn"]).pack(side="right")
            RoundButton(top, "Voltar e refazer uma posição", self.back_to_capture,
                        font=F["btn"]).pack(side="right", padx=8)
            RoundButton(top, "Abrir pasta", lambda: self.folder and open_folder(self.folder),
                        font=F["btn"]).pack(side="right")
            self.path_lbl = tk.Label(rv, text="", font=F["small"], fg=C["muted"], bg=C["bg"], anchor="w")
            self.path_lbl.pack(fill="x", padx=20)
            self.res_cv = tk.Canvas(rv, bg=C["bg"], highlightthickness=0)
            self.res_cv.pack(fill="both", expand=True, padx=20, pady=12)
            self.res_cv.bind("<Configure>", lambda e: self._draw_result())

            self.view = "capture"
            self._show_view("capture")
            self._refresh()

        # -- estado ---------------------------------------------------------
        def _show_view(self, v):
            self.view = v
            (self.capture_view if v == "capture" else self.result_view).tkraise()

        def _refresh(self):
            pos = POSITIONS[self.cur]
            self.diagram.draw(pos)
            self.step_lbl.configure(text=f"POSIÇÃO {self.cur + 1} DE {len(POSITIONS)}"
                                         + ("  ·  já registrada — Espaço substitui" if pos["key"] in self.caps else ""))
            self.name_lbl.configure(text=pos["nome"])
            self.cmd_lbl.configure(text=f"“{pos['comando']}”")
            self.descr_lbl.configure(text=f"Olhar {pos['descr']}")
            for i, (r, dot, num, name) in enumerate(self.rows):
                sel = i == self.cur
                bg = C["card2"] if sel else C["panel"]
                for w in (r, dot, num, name):
                    w.configure(bg=bg)
                dot.delete("all")
                if POSITIONS[i]["key"] in self.caps:
                    dot.create_oval(2, 2, 14, 14, fill=C["ok"], outline="")
                    dot.create_line(5, 8, 7, 11, 11, 5, fill="#05261a", width=2)
                else:
                    dot.create_oval(3, 3, 13, 13, outline=C["accent"] if sel else C["faint"], width=2)
                name.configure(fg=C["text"] if sel or POSITIONS[i]["key"] in self.caps else C["muted"])
            self.count_lbl.configure(text=f"{len(self.caps)}/{len(POSITIONS)}")
            self.undo_btn.set_enabled(bool(self.order))
            self.finish_btn.set_enabled(bool(self.caps) and not self.saving)
            if self.app.rig.sim:
                v = pos["vec"]
                SimCapture.target = (0.0, 0.3) if v is None else (float(v[0]), float(v[1]))

        def select(self, i):
            if self.saving:
                return
            self.cur = i % len(POSITIONS)
            self._show_view("capture")
            self._refresh()

        def on_show(self):
            self.app.rig.set_mode(MOTILITY_MODE)
            self._refresh()

        def on_hide(self):
            SimCapture.target = None

        def on_key(self, key):
            if self.saving:
                return False
            if key == "space":
                self.capture()
                return True
            if key == "BackSpace":
                self.redo_previous()
                return True
            if key in ("Up", "Down"):
                self.select(self.cur + (1 if key == "Down" else -1))
                return True
            return False

        # -- ações ----------------------------------------------------------
        def capture(self):
            if self.saving or self.view != "capture":
                return
            if self.app.rig.busy:
                self.app.toast.show("As câmeras ainda estão sendo configuradas…", "warn")
                return
            snap = self.app.rig.snapshot()
            if all(fr is None for fr in snap.values()):
                self.app.toast.show("Nenhuma câmera com imagem — nada foi registrado.", "erro")
                return
            pos = POSITIONS[self.cur]
            self.caps[pos["key"]] = {e: (Frame(fr.img.copy(), fr.t, fr.t_drv, fr.n) if fr else None)
                                     for e, fr in snap.items()}
            self.caps[pos["key"]]["hora"] = datetime.now().isoformat(timespec="milliseconds")
            if pos["key"] in self.order:
                self.order.remove(pos["key"])
            self.order.append(pos["key"])
            missing = [e for e, fr in snap.items() if fr is None]
            if missing:
                self.app.toast.show(f"{pos['nome']}: registrado sem o {missing[0]} (câmera sem imagem).", "warn")
            for p in self.previews.values():
                p.set_highlight(C["ok"])
            self.after(180, lambda: [p.set_highlight() for p in self.previews.values()])
            nxt = next((i for i in list(range(self.cur + 1, len(POSITIONS))) + list(range(0, self.cur))
                        if POSITIONS[i]["key"] not in self.caps), None)
            if nxt is None:
                self._refresh()
                self.finalize(auto=True)
            else:
                self.cur = nxt
                self._refresh()

        def redo_previous(self):
            if not self.order or self.saving:
                return
            key = self.order.pop()
            self.caps.pop(key, None)
            self.cur = next(i for i, p in enumerate(POSITIONS) if p["key"] == key)
            self._show_view("capture")
            self._refresh()
            self.app.toast.show(f"{POSITIONS[self.cur]['nome']} apagada — registre de novo.", "info", 2200)

        def reset(self):
            if self.saving:
                return
            if self.caps and self.folder is None:
                if not messagebox.askyesno("Novo exame", "Descartar as posições registradas e começar de novo?"):
                    return
            self.caps, self.order, self.cur, self.folder = {}, [], 0, None
            self.result_img = None
            self._show_view("capture")
            self._refresh()

        def back_to_capture(self):
            self._show_view("capture")
            self._refresh()

        def finalize(self, auto=False):
            if self.saving or not self.caps:
                return
            missing = len(POSITIONS) - len(self.caps)
            if missing and not auto:
                if not messagebox.askyesno("Gerar resultado",
                                           f"Faltam {missing} posição(ões). Gerar a página assim mesmo?"):
                    return
            app = self.app
            now = datetime.now()
            if self.folder is None:
                root = Path(app.cfg["saida"]).expanduser()
                self.folder = root / f"{now:%Y-%m-%d_%H%M%S}_{safe_name(app.patient_raw())}_motilidade"
            rig = app.rig
            info = {
                "paciente": app.patient_raw(),
                "data": now.strftime("%d/%m/%Y %H:%M"),
                "tecnico": [f"• {e}: {rig.label_for(rig.cams[e].index)}, {rig.cams[e].rotation}°"
                            + (", espelhado" if rig.cams[e].flip else "")
                            + (", recortado" if rig.cams[e].roi else "") for e in EYES]
                           + [f"• Modo: {MODES[MOTILITY_MODE]['rotulo']}",
                              f"• Posições registradas: {len(self.caps)}/{len(POSITIONS)}"],
                "cameras": {e: {"camera": rig.label_for(rig.cams[e].index), "rotacao": rig.cams[e].rotation,
                                "espelhado": rig.cams[e].flip,
                                "recorte_fracao_xywh": rig.cams[e].roi} for e in EYES},
            }
            caps = dict(self.caps)
            folder = self.folder
            self.saving = True
            self._refresh()
            self.app.toast.show("Gerando a página de resultado…", "info", 2000)

            def work():
                try:
                    res = save_motility_exam(folder, caps, info)
                    img = Image.open(res["png"])
                    img.load()
                    self.after(0, lambda: self._saved(img, res))
                except Exception as ex:
                    msg = f"{type(ex).__name__}: {ex}"
                    self.after(0, lambda: self._save_failed(msg))
            threading.Thread(target=work, daemon=True).start()

        def _saved(self, img, res):
            self.saving = False
            self.result_img = img
            self.path_lbl.configure(text=f"{res['pasta'].name}   ·   resultado.png · .pdf · .html · imagens/")
            self._show_view("result")
            self._draw_result()
            self._refresh()
            self.app.toast.show("Resultado salvo.", "ok")

        def _save_failed(self, msg):
            self.saving = False
            self._refresh()
            self.app.toast.show(f"Erro ao salvar: {msg}", "erro", 8000)

        def _draw_result(self):
            if self.result_img is None:
                return
            cw, ch = self.res_cv.winfo_width(), self.res_cv.winfo_height()
            if cw < 10 or ch < 10:
                return
            iw, ih = self.result_img.size
            s = min(cw / iw, ch / ih)
            im = self.result_img.resize((max(1, int(iw * s)), max(1, int(ih * s))), Image.LANCZOS)
            self.result_photo = ImageTk.PhotoImage(im)
            self.res_cv.delete("all")
            self.res_cv.create_image(cw / 2, 0, image=self.result_photo, anchor="n")

        def tick(self):
            for eye in EYES:
                fr, info, msg = self.cam_view(eye)
                self.previews[eye].show(fr, info, msg)
            self.cap_btn.set_enabled(not self.saving and not self.app.rig.busy)

    class RecordingScreen(Screen):
        def __init__(self, parent, app):
            super().__init__(parent, app)
            F = app.fonts
            self.rec: Recorder | None = None
            self.last_saved: Path | None = None
            self._save_job = None
            self.monitors: dict = {}       # olho -> PupilMonitor (só com "Medir pupila" ligado)
            self._extra_shown = None

            self.pv = pv = tk.Frame(self, bg=C["bg"])
            pv.pack(fill="both", expand=True, padx=20, pady=(16, 12))
            pv.rowconfigure(0, weight=1)
            self.previews = {e: Preview(pv, e, F, min_w=260, min_h=195) for e in ROLES}

            card = tk.Frame(self, bg=C["card"])
            card.pack(fill="x", padx=20, pady=(0, 16))
            r1 = tk.Frame(card, bg=C["card"])
            r1.pack(fill="x", padx=16, pady=(14, 6))
            tk.Label(r1, text="Resolução", font=F["small"], fg=C["muted"], bg=C["card"]).pack(side="left")
            self.mode_seg = Segmented(r1, [(k, m["rotulo"]) for k, m in MODES.items()], self.on_mode, F["btn"])
            self.mode_seg.pack(side="left", padx=(10, 28))
            tk.Label(r1, text="FPS alvo", font=F["small"], fg=C["muted"], bg=C["card"]).pack(side="left")
            self.slider = Slider(r1, 1, 128, 123, self.on_fps, width=260)
            self.slider.pack(side="left", padx=8)
            self.fps_lbl = tk.Label(r1, text="", font=F["title"], fg=C["text"], bg=C["card"], width=4, anchor="w")
            self.fps_lbl.pack(side="left")
            self.pupil_btn = RoundButton(r1, "◎  Medir pupila", self.toggle_pupil, font=F["btn"])
            self.pupil_btn.pack(side="right")
            self.eff_lbl = tk.Label(r1, text="", font=F["small"], fg=C["muted"], bg=C["card"], anchor="w",
                                    justify="left")
            self.eff_lbl.pack(side="left", padx=(10, 0), fill="x", expand=True)

            tk.Frame(card, bg=C["border"], height=1).pack(fill="x", padx=16, pady=6)

            r2 = tk.Frame(card, bg=C["card"])
            r2.pack(fill="x", padx=16, pady=(6, 14))
            stk = tk.Frame(r2, bg=C["card"])
            stk.pack(side="left")
            self.state_lbl = tk.Label(stk, text="", font=F["hint"], bg=C["card"], fg=C["muted"], anchor="w")
            self.state_lbl.pack(fill="x")
            self.timer_lbl = tk.Label(stk, text="00:00.0", font=F["big"], fg=C["text"], bg=C["card"], anchor="w")
            self.timer_lbl.pack(fill="x")
            tk.Frame(r2, bg=C["card"], width=18).pack(side="left")
            self.count_lbl = tk.Label(r2, text="", font=F["mono"], fg=C["muted"], bg=C["card"], justify="left")
            self.count_lbl.pack(side="left")
            bf = tk.Frame(r2, bg=C["card"])
            bf.pack(side="right")
            # Gravar / Pausar / Continuar ocupam o MESMO lugar (só um aparece
            # por vez, conforme o estado) -- o botão principal fica sempre
            # sob o mesmo ponto da tela.
            slot = tk.Frame(bf, bg=C["card"])
            slot.pack(side="left", padx=4)
            w1 = max(F["btn"].measure(t) + F["hint"].measure(h) + 60
                     for t, h in (("●  Gravar", "R"), ("❚❚  Pausar", "Espaço"), ("▶  Continuar", "Espaço")))
            mk = lambda parent, t, cmd, kind, hint, w=None: RoundButton(
                parent, t, cmd, kind=kind, hint=hint, font=F["btn"], hint_font=F["hint"], height=44, width=w)
            self.b_rec = mk(slot, "●  Gravar", self.start, "danger", "R", w1)
            self.b_pause = mk(slot, "❚❚  Pausar", self.pause, "neutral", "Espaço", w1)
            self.b_resume = mk(slot, "▶  Continuar", self.resume, "primary", "Espaço", w1)
            self.b_save = mk(bf, "✓  Salvar registro", self.save, "success", "S")
            self.b_discard = mk(bf, "Descartar", self.discard, "neutral", "Del")
            self.b_save.pack(side="left", padx=4)
            self.b_discard.pack(side="left", padx=4)
            self._slot_btn = None

            self.saved_row = tk.Frame(card, bg=C["card"])
            self.saved_lbl = tk.Label(self.saved_row, text="", font=F["small"], fg=C["ok"], bg=C["card"], anchor="w")
            self.saved_lbl.pack(side="left", fill="x", expand=True)
            RoundButton(self.saved_row, "Abrir pasta", lambda: self.last_saved and open_folder(self.last_saved),
                        font=F["small"], height=30).pack(side="right")

            g = app.cfg["gravacao"]
            self.mode_seg.set(g["modo"])
            self.slider.set_range(1, MODES[g["modo"]]["fps"])
            self.slider.set(g["fps"])
            self.pupil_btn.set_selected(bool(g.get("pupila")))
            self._layout_previews()
            self._update_buttons()

        def _layout_previews(self):
            """OD | OE, ou OD | OE | Extra quando há câmera extra atribuída."""
            shown = self.app.rig.cams[EXTRA].index is not None
            if shown == self._extra_shown:
                return
            self._extra_shown = shown
            roles = ROLES if shown else EYES
            for c in range(3):
                self.pv.columnconfigure(c, weight=1 if c < len(roles) else 0, uniform="p" if c < len(roles) else "")
            for k, e in enumerate(roles):
                self.previews[e].grid(row=0, column=k, sticky="nsew",
                                      padx=(0 if k == 0 else 8, 0 if k == len(roles) - 1 else 8))
            if not shown:
                self.previews[EXTRA].grid_remove()

        # -- pupila ----------------------------------------------------------
        def toggle_pupil(self):
            if self.rec and self.rec.active_registro:
                return
            g = self.app.cfg["gravacao"]
            g["pupila"] = not g.get("pupila")
            self.pupil_btn.set_selected(g["pupila"])
            self.app.save_cfg()
            self._sync_monitors()

        def _sync_monitors(self, on: bool | None = None):
            """Ao vivo só para mostrar a marcação: 15 Hz poupa CPU para a
            gravação. A medida de todos os quadros é feita ao salvar."""
            if on is None:
                on = bool(self.app.cfg["gravacao"].get("pupila")) and self.app.current == "rec"
            for e in EYES:
                cam = self.app.rig.cams[e]
                m = self.monitors.get(e)
                if m is not None and (not on or m.cam is not cam):
                    m.stop()
                    self.monitors.pop(e)
                if on and e not in self.monitors:
                    self.monitors[e] = PupilMonitor(cam, max_hz=15)

        def on_hide(self):
            self._sync_monitors(False)

        # -- configuração ----------------------------------------------------
        def on_mode(self, k):
            if self.rec and self.rec.active_registro:
                return
            self.mode_seg.set(k)
            g = self.app.cfg["gravacao"]
            g["modo"] = k
            self.slider.set_range(1, MODES[k]["fps"])
            g["fps"] = self.slider.value
            self.app.save_cfg()
            self.app.rig.set_mode(k)

        def on_fps(self, v):
            self.app.cfg["gravacao"]["fps"] = v
            if self._save_job:
                self.after_cancel(self._save_job)
            self._save_job = self.after(600, self.app.save_cfg)

        def on_show(self):
            self.app.rig.set_mode(self.app.cfg["gravacao"]["modo"])
            self._sync_monitors(bool(self.app.cfg["gravacao"].get("pupila")))

        def can_leave(self):
            if self.rec and self.rec.active_registro:
                return False, "Salve ou descarte o registro antes de sair da Gravação."
            return True, ""

        # -- ações ----------------------------------------------------------
        def start(self):
            if self.rec and self.rec.active_registro:
                return
            rig = self.app.rig
            if rig.busy:
                self.app.toast.show("As câmeras ainda estão sendo configuradas…", "warn")
                return
            live = [e for e in EYES if rig.cams[e].ok]
            if not live:
                self.app.toast.show("Nenhuma câmera com imagem.", "erro")
                return
            if len(live) == 1:
                self.app.toast.show(f"Gravando só o {live[0]} — a outra câmera está sem imagem.", "warn", 5000)
            elif rig.cams[EXTRA].index is not None and not rig.cams[EXTRA].ok:
                self.app.toast.show("Câmera extra sem imagem — gravando só os olhos.", "warn", 5000)
            self.rec = Recorder(rig, Path(self.app.cfg["saida"]), self.app.patient_raw(),
                                self.slider.value, rig.mode_key, pupil=bool(self.app.cfg["gravacao"].get("pupila")))
            try:
                self.rec.start()
            except Exception as ex:
                self.app.toast.show(f"Não foi possível iniciar: {ex}", "erro", 8000)
                self.rec = None
            self._update_buttons()

        def pause(self):
            if self.rec and self.rec.state == "rec":
                self.rec.pause()
                self._update_buttons()

        def resume(self):
            if self.rec and self.rec.state == "paused":
                self.rec.resume()
                self._update_buttons()

        def save(self):
            if self.rec and self.rec.state in ("rec", "paused"):
                self.rec.pause()
                if self.rec.frames("OD") + self.rec.frames("OE") == 0:
                    self.rec.discard()
                    self.app.toast.show("Nada foi gravado — registro descartado.", "warn")
                    self._update_buttons()
                    return
                self.rec.save_async()
                self._update_buttons()

        def discard(self):
            if not (self.rec and self.rec.state in ("rec", "paused")):
                return
            was_rec = self.rec.state == "rec"
            self.rec.pause()
            if messagebox.askyesno("Descartar registro",
                                   f"Apagar o registro atual ({fmt_time(self.rec.elapsed())})?\n"
                                   "Isso não pode ser desfeito."):
                self.rec.discard()
                self.app.toast.show("Registro descartado.", "info")
            elif was_rec:
                self.rec.resume()
            self._update_buttons()

        def on_key(self, key):
            k = key.lower()
            st = self.rec.state if self.rec else "idle"
            if k == "r" and st not in ("rec", "paused", "saving"):
                self.start()
            elif k == "space" and st in ("rec", "paused"):
                self.pause() if st == "rec" else self.resume()
            elif k == "s" and st in ("rec", "paused"):
                self.save()
            elif k in ("delete",) and st in ("rec", "paused"):
                self.discard()
            else:
                return False
            return True

        # -- estado ---------------------------------------------------------
        def _update_buttons(self):
            st = self.rec.state if self.rec else "idle"
            active = st in ("rec", "paused")
            want = {"rec": self.b_pause, "paused": self.b_resume}.get(st, self.b_rec)
            if want is not self._slot_btn:
                if self._slot_btn is not None:
                    self._slot_btn.pack_forget()
                want.pack()
                self._slot_btn = want
            self.b_rec.set_enabled(st not in ("rec", "paused", "saving", "stopping"))
            self.b_pause.set_enabled(st == "rec")
            self.b_resume.set_enabled(st == "paused")
            self.b_save.set_enabled(active)
            self.b_discard.set_enabled(active)
            self.mode_seg.set_enabled(not active and st != "saving")
            self.slider.set_enabled(not active and st != "saving")
            self.pupil_btn.set_enabled(not active and st != "saving")

        def tick(self):
            rig, rec = self.app.rig, self.rec
            st = rec.state if rec else "idle"
            self._layout_previews()
            if self.monitors:
                self._sync_monitors()          # OD/OE trocados ou câmera reaberta
            for eye in (ROLES if self._extra_shown else EYES):
                fr, info, msg = self.cam_view(eye)
                p = self.previews[eye]
                m = self.monitors.get(eye)
                if m is not None and fr is not None:
                    lt = m.latest
                    if lt is not None and fr.t - lt[0].t < 0.3:      # quadro e medida do mesmo instante
                        fr, rd = lt
                        info = f"{rig.cams[eye].fps:4.0f} fps · pupila " + (f"{rd.mm:.1f} mm" if rd else "--")
                        p.show(fr, info, msg, pupil=rd)
                    else:
                        p.show(fr, info, msg)
                else:
                    p.show(fr, info, msg)
                if st == "rec":
                    p.set_highlight(C["rec"])
                    p.set_badge("●  REC", C["rec"] if int(time.monotonic() * 2) % 2 == 0 else "#a8323d")
                elif st == "paused":
                    p.set_highlight(C["warn"])
                    p.set_badge("❚❚  PAUSADO", "#a36d12")
                else:
                    p.set_highlight()
                    p.set_badge()

            target = self.slider.value
            self.fps_lbl.configure(text=str(target))
            cams = {e: rig.cams[e].fps for e in EYES}
            live = [v for v in cams.values() if v > 1]
            if live:
                eff = min(target, min(live))
                ext = f" · extra {rig.cams[EXTRA].fps:.1f}" if self._extra_shown else ""
                self.eff_lbl.configure(text=f"câmeras: OD {cams['OD']:.1f} · OE {cams['OE']:.1f}{ext} fps\n"
                                            f"grava ≈ {eff:.0f} fps")
            else:
                self.eff_lbl.configure(text="câmeras sem imagem")

            labels = {"idle": ("PRONTO", C["muted"]), "rec": ("● GRAVANDO", C["rec"]),
                      "paused": ("❚❚ PAUSADO", C["warn"]), "saving": ("SALVANDO", C["accent"]),
                      "stopping": ("SALVANDO", C["accent"]), "saved": ("SALVO", C["ok"]),
                      "discarded": ("PRONTO", C["muted"]), "error": ("ERRO", C["rec"])}
            text, col = labels.get(st, ("", C["muted"]))
            self.state_lbl.configure(text=text, fg=col)
            if rec and st in ("rec", "paused"):
                self.timer_lbl.configure(text=fmt_time(rec.elapsed()))
                el = max(rec.elapsed(), 1e-3)
                seg = f"  ·  trecho {rec.segment}" if rec.segment > 1 else ""
                drop = f"  ·  {rec.dropped()} perdidos" if rec.dropped() else ""
                lines = [f"OD {rec.frames('OD'):>6} q · {rec.frames('OD') / el:5.1f} fps{seg}",
                         f"OE {rec.frames('OE'):>6} q · {rec.frames('OE') / el:5.1f} fps{drop}"]
                if EXTRA in rec.roles:
                    lines.append(f"Ex {rec.frames(EXTRA):>6} q · {rec.frames(EXTRA) / el:5.1f} fps")
                self.count_lbl.configure(text="\n".join(lines))
            elif rec and st in ("saving", "stopping"):
                self.count_lbl.configure(text=f"salvando… {int(rec.progress * 100)}%")
            elif st in ("idle", "discarded"):
                self.timer_lbl.configure(text="00:00.0")
                self.count_lbl.configure(text="")

            if rec and st == "saved" and rec.saved_path != self.last_saved:
                self.last_saved = rec.saved_path
                s = rec.summary["olhos"]
                x = rec.summary.get("camera_extra")
                extra = f" · extra {x['quadros']} a {x['fps_efetivo']:.1f} fps" if x else ""
                pup = " · pupila medida" if rec.pupil else ""
                self.saved_lbl.configure(
                    text=f"✓ Salvo em {rec.saved_path.name}   ·   OD {s['OD']['quadros']} quadros a "
                         f"{s['OD']['fps_efetivo']:.1f} fps · OE {s['OE']['quadros']} a {s['OE']['fps_efetivo']:.1f} fps"
                         f"{extra}{pup}")
                self.saved_row.pack(fill="x", padx=16, pady=(0, 12))
                self.app.toast.show("Registro salvo.", "ok")
                self.count_lbl.configure(text="")
                self._update_buttons()
            if rec and st == "error" and not getattr(rec, "_reported", False):
                rec._reported = True
                self.app.toast.show(f"Erro ao salvar: {rec.error}. Os arquivos ficaram em {rec.tmp}", "erro", 12000)
                self._update_buttons()
            if rec and st in ("saving", "stopping"):
                self._update_buttons()

    class PupilScreen(Screen):
        """Pupilometria: mede os dois olhos ao vivo; durante a avaliação
        guarda o maior e o menor diâmetro de cada olho com a foto (trocada
        sempre que é superada) e salva a página de resultado."""

        EYE_COLORS = {"OD": "#4c8dff", "OE": "#f59e4b"}

        def __init__(self, parent, app):
            super().__init__(parent, app)
            F = app.fonts
            self.exam: PupilExam | None = None
            self.monitors: dict = {}
            self.saving = False
            self.folder: Path | None = None
            self._shown_changed = -1
            self._thumb_photos: dict = {}
            self._chart_of = None
            self._n = 0

            guide = tk.Frame(self, bg=C["card"])
            guide.pack(fill="x", padx=20, pady=(16, 12))
            txt = tk.Frame(guide, bg=C["card"])
            txt.pack(side="left", fill="both", expand=True, padx=16, pady=12)
            tk.Label(txt, text="Pupila · resposta à luz", font=F["h1"], fg=C["text"], bg=C["card"],
                     anchor="w").pack(fill="x")
            tk.Label(txt, text="Paciente olhando para longe, à frente. Inicie a avaliação e varie a luz "
                               "(apague a sala, acenda a lanterna em cada olho). O programa guarda o MAIOR e o "
                               "MENOR diâmetro de cada olho, com a foto; um novo recorde substitui o anterior.",
                     font=F["small"], fg=C["muted"], bg=C["card"], anchor="w", justify="left",
                     wraplength=720).pack(fill="x", pady=(4, 0))
            stk = tk.Frame(guide, bg=C["card"])
            stk.pack(side="left", padx=12)
            self.state_lbl = tk.Label(stk, text="", font=F["hint"], bg=C["card"], fg=C["muted"], anchor="w")
            self.state_lbl.pack(fill="x")
            self.timer_lbl = tk.Label(stk, text="00:00.0", font=F["big"], fg=C["text"], bg=C["card"], anchor="w")
            self.timer_lbl.pack(fill="x")
            bf = tk.Frame(guide, bg=C["card"])
            bf.pack(side="right", padx=16)
            w1 = max(F["btn"].measure(t) + F["hint"].measure("Espaço") + 60
                     for t in ("▶  Iniciar avaliação", "■  Encerrar e salvar"))
            self.b_start = RoundButton(bf, "▶  Iniciar avaliação", self.start, kind="primary", hint="Espaço",
                                       font=F["btn"], hint_font=F["hint"], height=44, width=w1)
            self.b_stop = RoundButton(bf, "■  Encerrar e salvar", self.stop, kind="success", hint="Espaço",
                                      font=F["btn"], hint_font=F["hint"], height=44, width=w1)
            self.b_reset = RoundButton(bf, "Zerar", self.reset, kind="ghost", font=F["btn"], height=44)
            self.b_start.pack(side="left", padx=4)
            self.b_reset.pack(side="left", padx=4)
            self._slot_btn = self.b_start

            row = tk.Frame(self, bg=C["bg"])
            row.pack(fill="both", expand=True, padx=20)
            row.columnconfigure(0, weight=1, uniform="p")
            row.columnconfigure(1, weight=1, uniform="p")
            row.rowconfigure(0, weight=1)
            self.previews, self.now_lbl, self.now_sub, self.thumbs = {}, {}, {}, {}
            for k, eye in enumerate(EYES):
                col = tk.Frame(row, bg=C["bg"])
                col.grid(row=0, column=k, sticky="nsew", padx=(0, 8) if k == 0 else (8, 0))
                p = Preview(col, eye, F, min_w=300, min_h=120)
                p.pack(fill="both", expand=True)
                self.previews[eye] = p
                st = tk.Frame(col, bg=C["card"])
                st.pack(fill="x", pady=(8, 0))
                cur = tk.Frame(st, bg=C["card"])
                cur.pack(side="left", fill="y", padx=14, pady=10)
                tk.Label(cur, text="AGORA", font=F["hint"], fg=C["muted"], bg=C["card"], anchor="w").pack(fill="x")
                self.now_lbl[eye] = tk.Label(cur, text="--", font=F["big"], fg=self.EYE_COLORS[eye], bg=C["card"],
                                             anchor="w", width=7)
                self.now_lbl[eye].pack(fill="x")
                self.now_sub[eye] = tk.Label(cur, text="", font=F["small"], fg=C["muted"], bg=C["card"], anchor="w",
                                             justify="left")
                self.now_sub[eye].pack(fill="x")
                for kind, title, sub in reversed(PUPIL_EXTREMES):
                    box = tk.Frame(st, bg=C["card"])
                    box.pack(side="right", padx=(0, 12), pady=10)
                    cv = tk.Canvas(box, width=150, height=100, bg=C["video"], highlightthickness=0)
                    cv.pack()
                    lbl = tk.Label(box, text=f"{title}: --", font=F["small"], fg=C["text"], bg=C["card"])
                    lbl.pack(pady=(4, 0))
                    self.thumbs[(eye, kind)] = (cv, lbl, title)

            self.chart = tk.Canvas(self, height=116, bg=C["card"], highlightthickness=0)
            self.chart.pack(fill="x", padx=20, pady=(10, 0))
            self.saved_row = tk.Frame(self, bg=C["bg"])
            self.saved_row.pack(fill="x", padx=20, pady=(8, 12))
            self.saved_lbl = tk.Label(self.saved_row, text="", font=F["small"], fg=C["ok"], bg=C["bg"], anchor="w")
            self.saved_lbl.pack(side="left", fill="x", expand=True)
            self.open_btn = RoundButton(self.saved_row, "Abrir pasta", lambda: self.folder and open_folder(self.folder),
                                        font=F["small"], height=30)
            self._update_buttons()

        # -- monitores --------------------------------------------------------
        def _on_reading(self, eye, fr, rd):
            ex = self.exam
            if ex is not None and ex.t_end is None:
                ex.add(eye, fr, rd)

        def _sync_monitors(self, on: bool):
            for e in EYES:
                cam = self.app.rig.cams[e]
                m = self.monitors.get(e)
                if m is not None and (not on or m.cam is not cam):
                    m.stop()
                    self.monitors.pop(e)
                if on and e not in self.monitors:
                    self.monitors[e] = PupilMonitor(cam, 30, on_reading=lambda fr, rd, e=e: self._on_reading(e, fr, rd))

        def on_show(self):
            self.app.rig.set_mode(PUPIL_MODE)
            self._sync_monitors(True)

        def on_hide(self):
            self._sync_monitors(False)

        def can_leave(self):
            if self.exam is not None and self.exam.t_end is None:
                return False, "Encerre a avaliação da pupila antes de sair."
            if self.saving:
                return False, "Aguarde terminar de salvar…"
            return True, ""

        def on_key(self, key):
            if key == "space":
                if self.exam is not None and self.exam.t_end is None:
                    self.stop()
                else:
                    self.start()
                return True
            return False

        # -- ações ------------------------------------------------------------
        def start(self):
            if self.saving or (self.exam is not None and self.exam.t_end is None):
                return
            if self.app.rig.busy:
                self.app.toast.show("As câmeras ainda estão sendo configuradas…", "warn")
                return
            if not any(self.app.rig.cams[e].ok for e in EYES):
                self.app.toast.show("Nenhuma câmera com imagem.", "erro")
                return
            self.exam = PupilExam()
            self.folder = None
            self._shown_changed = -1
            self.saved_lbl.configure(text="")
            self.open_btn.pack_forget()
            self._update_buttons()

        def reset(self):
            if self.saving:
                return
            self.exam = None
            self._shown_changed = -1
            self._update_buttons()

        def stop(self):
            ex = self.exam
            if ex is None or ex.t_end is not None or self.saving:
                return
            ex.finish()
            snap = ex.snapshot()
            if not any(snap["series"].values()):
                self.app.toast.show("Nenhuma medida de pupila — nada foi salvo. Confira foco, luz IR e recorte.",
                                    "warn", 6000)
                self._update_buttons()
                return
            app, rig, now = self.app, self.app.rig, datetime.now()
            self.folder = Path(app.cfg["saida"]).expanduser() / \
                f"{now:%Y-%m-%d_%H%M%S}_{safe_name(app.patient_raw())}_pupila"
            info = {
                "paciente": app.patient_raw(),
                "data": now.strftime("%d/%m/%Y %H:%M"),
                "tecnico": [f"{e}: {rig.label_for(rig.cams[e].index)}" + (", recortado" if rig.cams[e].roi else "")
                            for e in EYES] + [f"Modo: {MODES[PUPIL_MODE]['rotulo']}",
                                              f"Duração: {fmt_time(snap['duracao'])}"],
                "cameras": {e: {"camera": rig.label_for(rig.cams[e].index), "rotacao": rig.cams[e].rotation,
                                "espelhado": rig.cams[e].flip,
                                "recorte_fracao_xywh": rig.cams[e].roi} for e in EYES},
            }
            folder = self.folder
            self.saving = True
            self._update_buttons()
            self.app.toast.show("Salvando a avaliação da pupila…", "info", 2000)

            def work():
                try:
                    save_pupil_exam(folder, snap, info)
                    self.after(0, lambda: self._saved(folder))
                except Exception as ex_:
                    msg = f"{type(ex_).__name__}: {ex_}"
                    self.after(0, lambda: self._save_failed(msg))
            threading.Thread(target=work, daemon=True).start()

        def _saved(self, folder):
            self.saving = False
            self.saved_lbl.configure(text=f"✓ Salvo em {folder.name}   ·   resultado.png · .pdf · pupila.csv · imagens/")
            self.open_btn.pack(side="right")
            self._update_buttons()
            self.app.toast.show("Avaliação da pupila salva.", "ok")

        def _save_failed(self, msg):
            self.saving = False
            self._update_buttons()
            self.app.toast.show(f"Erro ao salvar: {msg}", "erro", 8000)

        # -- tela -------------------------------------------------------------
        def _update_buttons(self):
            running = self.exam is not None and self.exam.t_end is None
            want = self.b_stop if running else self.b_start
            if want is not self._slot_btn:
                self._slot_btn.pack_forget()
                want.pack(side="left", padx=4, before=self.b_reset)
                self._slot_btn = want
            self.b_start.set_enabled(not running and not self.saving)
            self.b_stop.set_enabled(running and not self.saving)
            self.b_reset.set_enabled(self.exam is not None and not running and not self.saving)

        def _draw_thumbs(self, snap):
            for (eye, kind), (cv, lbl, title) in self.thumbs.items():
                r = snap["extremes"][eye][kind] if snap else None
                cv.delete("all")
                if r is None:
                    cv.create_text(75, 50, text="—", fill=C["faint"], font=self.app.fonts["body"])
                    lbl.configure(text=f"{title}: --")
                    continue
                img = draw_pupil(r["frame"].img.copy(), r["reading"], label=False)
                h, w = img.shape[:2]
                s = min(150 / w, 100 / h)
                small = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
                ph = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)))
                self._thumb_photos[(eye, kind)] = ph
                cv.create_image(75, 50, image=ph)
                lbl.configure(text=f"{title}: {r['mm']:.1f} mm · {r['t']:.1f} s")

        def _draw_chart(self, snap):
            cv = self.chart
            cv.delete("all")
            W, H = max(10, cv.winfo_width()), max(10, cv.winfo_height())
            f = self.app.fonts["small"]
            series = snap["series"] if snap else {}
            vals = [v[1] for e in EYES for v in series.get(e, [])]
            if not vals:
                cv.create_text(W / 2, H / 2, fill=C["faint"], font=f,
                               text="o gráfico do diâmetro aparece durante a avaliação")
                return
            lo, hi = math.floor(min(vals) - 0.3), math.ceil(max(vals) + 0.3)
            T = max(10.0, snap["duracao"])
            x0, x1, y0, y1 = 56, W - 16, 12, H - 22
            X = lambda t: x0 + (x1 - x0) * t / T
            Y = lambda v: y1 - (y1 - y0) * (v - lo) / max(1e-6, hi - lo)
            step = 1 if hi - lo <= 6 else 2
            for v in range(int(lo), int(hi) + 1, step):
                cv.create_line(x0, Y(v), x1, Y(v), fill=C["border"])
                cv.create_text(x0 - 8, Y(v), text=f"{v} mm", anchor="e", fill=C["muted"], font=f)
            tstep = next(s for s in (1, 2, 5, 10, 15, 30, 60, 120, 300) if T / s <= 12)
            for k in range(0, int(T) + 1, tstep):
                cv.create_text(X(k), H - 10, text=f"{k} s", fill=C["muted"], font=f)
            for e in EYES:
                pts = series.get(e, [])
                if len(pts) > 400:                     # o gráfico não precisa de todos os pontos
                    pts = pts[:: len(pts) // 400 + 1]
                seg = []
                for i, p in enumerate(pts):
                    if seg and p[0] - pts[i - 1][0] > 0.6:
                        if len(seg) > 3:
                            cv.create_line(*seg, fill=self.EYE_COLORS[e], width=2)
                        seg = []
                    seg += [X(p[0]), Y(p[1])]
                if len(seg) > 3:
                    cv.create_line(*seg, fill=self.EYE_COLORS[e], width=2)
                for kind, _, _ in PUPIL_EXTREMES:
                    r = snap["extremes"][e][kind]
                    if r:
                        cx, cy = X(r["t"]), Y(r["mm"])
                        cv.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, outline=self.EYE_COLORS[e], width=2,
                                       fill=C["card"])
            for k, e in enumerate(EYES):
                lx = x1 - 110 + k * 56
                cv.create_line(lx, y0 + 6, lx + 18, y0 + 6, fill=self.EYE_COLORS[e], width=3)
                cv.create_text(lx + 24, y0 + 6, text=e, anchor="w", fill=C["text"], font=f)

        def tick(self):
            self._n += 1
            if self.monitors:
                self._sync_monitors(True)
            for eye in EYES:
                fr, info, msg = self.cam_view(eye)
                m = self.monitors.get(eye)
                lt = m.latest if m else None
                rd = None
                if fr is not None and lt is not None and fr.t - lt[0].t < 0.5:
                    fr, rd = lt[0], lt[1]
                    self.previews[eye].show(fr, info, msg, pupil=rd)
                else:
                    self.previews[eye].show(fr, info, msg)
                if rd is not None:
                    self.now_lbl[eye].configure(text=f"{rd.mm:.1f} mm")
                    self.now_sub[eye].configure(text=f"P/I {rd.ratio:.2f} · íris {2 * rd.iris[2]:.0f} px\n"
                                                     f"confiança {rd.conf:.2f}")
                else:
                    self.now_lbl[eye].configure(text="--")
                    self.now_sub[eye].configure(text="sem medida\n(piscada, foco ou luz IR)" if fr is not None
                                                else "")
            ex = self.exam
            running = ex is not None and ex.t_end is None
            if ex is None:
                if self._shown_changed != -2:
                    self._draw_thumbs(None)
                    self._draw_chart(None)
                    self._shown_changed = -2
                self.timer_lbl.configure(text="00:00.0")
            else:
                self.timer_lbl.configure(text=fmt_time(ex.duration))
                snap = None
                if ex.changed != self._shown_changed:
                    snap = ex.snapshot()
                    self._draw_thumbs(snap)
                    self._shown_changed = ex.changed
                # gráfico: ~5×/s durante a avaliação, e uma última vez ao encerrar
                if snap is not None or (running and self._n % 6 == 0) or (not running and self._chart_of is not ex):
                    self._draw_chart(snap or ex.snapshot())
                    if not running:
                        self._chart_of = ex
            if self.saving:
                text, col = "SALVANDO", C["accent"]
            elif running:
                text, col = "● AVALIANDO", C["rec"]
            elif ex is not None:
                text, col = "ENCERRADA", C["ok"]
            else:
                text, col = "PRONTO", C["muted"]
            self.state_lbl.configure(text=text, fg=col)

    def fmt_time(s: float) -> str:
        m, s = divmod(max(0.0, s), 60)
        return f"{int(m):02d}:{s:04.1f}"

    def pick_family(root, names):
        fams = set(tkfont.families(root))
        return next((f for f in names if f in fams), "TkDefaultFont")

    class App:
        def __init__(self, root, args):
            self.root, self.args = root, args
            self.cfg_path = CONFIG_PATH.with_suffix(".sim.json") if args.simular else CONFIG_PATH
            self.cfg = load_config(self.cfg_path, args.simular)
            if args.saida:
                self.cfg["saida"] = args.saida
            fam = pick_family(root, ["Inter", "Segoe UI", "SF Pro Text", "Helvetica Neue", "Cantarell",
                                     "Noto Sans", "DejaVu Sans", "Liberation Sans", "Arial"])
            mono = pick_family(root, ["JetBrains Mono", "Cascadia Mono", "Consolas", "SF Mono", "Menlo",
                                      "DejaVu Sans Mono", "Liberation Mono", "Courier New"])
            self.fonts = {
                "h1": tkfont.Font(family=fam, size=19, weight="bold"),
                "title": tkfont.Font(family=fam, size=15, weight="bold"),
                "h2": tkfont.Font(family=fam, size=13, weight="bold"),
                "body": tkfont.Font(family=fam, size=11),
                "small": tkfont.Font(family=fam, size=10),
                "btn": tkfont.Font(family=fam, size=11, weight="bold"),
                "hint": tkfont.Font(family=fam, size=8, weight="bold"),
                "mono": tkfont.Font(family=mono, size=10),
                "big": tkfont.Font(family=mono, size=22, weight="bold"),
            }
            root.title("OculoDiag")
            root.configure(bg=C["bg"])
            root.geometry(args.geometria or "1360x860")
            root.minsize(1200, 720)

            self.rig = CameraRig(self.cfg, save_cb=self.save_cfg, sim=args.simular, backend=args.cam_backend)

            # barra superior
            top = tk.Frame(root, bg=C["panel"])
            top.pack(fill="x")
            brand = tk.Frame(top, bg=C["panel"])
            brand.pack(side="left", padx=20, pady=12)
            tk.Label(brand, text="OculoDiag", font=self.fonts["h1"], fg=C["text"], bg=C["panel"]).pack(side="left")
            tk.Label(brand, text="motilidade ocular · IR", font=self.fonts["small"], fg=C["muted"],
                     bg=C["panel"]).pack(side="left", padx=(10, 0), pady=(6, 0))
            if args.simular:
                tk.Label(brand, text=" SIMULAÇÃO ", font=self.fonts["hint"], fg="#1a1204", bg=C["warn"]).pack(
                    side="left", padx=(12, 0), pady=(4, 0))
            self.tabs = Segmented(top, [("cam", "1  Câmeras"), ("mot", "2  Motilidade"), ("rec", "3  Gravação"),
                                        ("pup", "4  Pupila")], self.show, self.fonts["btn"], height=36)
            self.tabs.pack(side="left", padx=30)
            pt = tk.Frame(top, bg=C["panel"])
            pt.pack(side="right", padx=20)
            tk.Label(pt, text="Paciente", font=self.fonts["small"], fg=C["muted"], bg=C["panel"]).pack(side="left")
            self.patient = tk.Entry(pt, font=self.fonts["body"], bg=C["card2"], fg=C["text"], width=20,
                                    insertbackground=C["text"], relief="flat", highlightthickness=1,
                                    highlightbackground=C["border"], highlightcolor=C["accent"])
            self.patient.pack(side="left", padx=(8, 0), ipady=5, ipadx=6)

            # a barra de status é empacotada ANTES do conteúdo: em janela baixa
            # quem encolhe é o conteúdo, não ela
            status = tk.Frame(root, bg=C["panel"])
            status.pack(fill="x", side="bottom")
            self.status_l = tk.Label(status, text="", font=self.fonts["mono"], fg=C["muted"], bg=C["panel"])
            self.status_l.pack(side="left", padx=20, pady=6)
            self.status_r = tk.Label(status, text="F1 F2 F3 F4 trocam de tela", font=self.fonts["small"],
                                     fg=C["faint"], bg=C["panel"])
            self.status_r.pack(side="right", padx=20)

            content = tk.Frame(root, bg=C["bg"])
            content.pack(fill="both", expand=True)
            content.rowconfigure(0, weight=1)
            content.columnconfigure(0, weight=1)
            self.screens = {"cam": CamerasScreen(content, self), "mot": MotilityScreen(content, self),
                            "rec": RecordingScreen(content, self), "pup": PupilScreen(content, self)}
            for s in self.screens.values():
                s.grid(row=0, column=0, sticky="nsew")

            self.toast = Toast(root, self.fonts["body"])
            root.bind_all("<KeyPress>", self._on_key)
            root.bind_all("<Button-1>", self._on_click, add="+")
            root.protocol("WM_DELETE_WINDOW", self.quit)

            self.current = None
            self.rig.start(MOTILITY_MODE)
            self.show("cam")
            self._n_tick = 0
            self._tick()

        def save_cfg(self):
            save_config(self.cfg_path, self.cfg)

        def patient_raw(self) -> str:
            return self.patient.get().strip()

        def show(self, key):
            if self.current == key:
                return
            if self.current is not None:
                ok, msg = self.screens[self.current].can_leave()
                if not ok:
                    self.toast.show(msg, "warn")
                    self.tabs.set(self.current)
                    return
                self.screens[self.current].on_hide()
            self.current = key
            self.tabs.set(key)
            self.screens[key].tkraise()
            self.screens[key].on_show()

        def _on_click(self, e):
            if not isinstance(e.widget, tk.Entry):
                try:
                    self.root.focus_set()
                except Exception:
                    pass

        def _on_key(self, e):
            if isinstance(self.root.focus_get(), tk.Entry):
                if e.keysym in ("Return", "KP_Enter", "Escape", "Tab"):
                    self.root.focus_set()
                    return "break"
                return None
            if e.keysym in ("F1", "F2", "F3", "F4"):
                self.show({"F1": "cam", "F2": "mot", "F3": "rec", "F4": "pup"}[e.keysym])
                return "break"
            if self.current and self.screens[self.current].on_key(e.keysym):
                return "break"
            return None

        def _tick(self):
            try:
                self.screens[self.current].tick()
                self._n_tick += 1
                if self._n_tick % 8 == 0:
                    parts = []
                    for e in ROLES:
                        cam = self.rig.cams[e]
                        if e == EXTRA and cam.index is None:
                            continue
                        parts.append(f"{ROLE_TITLES[e]} {self.rig.label_for(cam.index).split(' · ')[0]} "
                                     f"{cam.fps:5.1f} fps" if cam.ok else f"{ROLE_TITLES[e]} {cam.status}")
                    mode = MODES[self.rig.mode_key]["rotulo"] if self.rig.mode_key else "—"
                    busy = "   ·   configurando câmeras…" if self.rig.busy else ""
                    self.status_l.configure(text="   ·   ".join(parts) + f"   ·   {mode}{busy}")
            except Exception as ex:
                print(f"[interface] {type(ex).__name__}: {ex}")
            self.root.after(33, self._tick)

        def quit(self):
            rec = self.screens["rec"].rec
            if rec and rec.state in ("rec", "paused"):
                if not messagebox.askyesno("Sair", "Há um registro não salvo. Sair e DESCARTAR?"):
                    return
                rec.discard()
            elif rec and rec.state == "saving":
                self.toast.show("Aguarde terminar de salvar…", "warn")
                return
            pup = self.screens["pup"]
            if pup.saving:
                self.toast.show("Aguarde terminar de salvar…", "warn")
                return
            if pup.exam is not None and pup.exam.t_end is None:
                if not messagebox.askyesno("Sair", "Há uma avaliação de pupila em andamento. Sair sem salvar?"):
                    return
            for scr in (pup, self.screens["rec"]):
                scr.on_hide()
            self.save_cfg()
            self.rig.shutdown()
            self.root.destroy()


def main(argv=None):
    ap = argparse.ArgumentParser(description="OculoDiag — motilidade ocular com câmeras IR near-eye")
    ap.add_argument("--simular", action="store_true", help="olhos sintéticos, sem câmeras")
    ap.add_argument("--saida", help="pasta onde os exames são salvos (padrão ~/OculoDiag)")
    ap.add_argument("--cam-backend", default="auto", choices=["auto", *BACKENDS.keys()])
    ap.add_argument("--geometria", help="tamanho da janela, ex. 1280x800")
    args = ap.parse_args(argv)
    if tk is None:
        sys.exit("tkinter não está instalado.  Raspberry Pi OS / Debian:  sudo apt install python3-tk "
                 "python3-pil.imagetk")
    root = tk.Tk()
    App(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()
