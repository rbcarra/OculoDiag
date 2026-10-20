#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OculoDiag -- registro diagnóstico de motilidade ocular com duas câmeras IR
near-eye (HBVCAM-1914S V44 / sensor GC0308).

Três telas (F1 / F2 / F3):
  1. Câmeras     qual câmera é de cada olho, rotação, espelhamento e
                 recorte (ROI) de cada olho (salvo em ~/.oculodiag.json e
                 reaplicado na próxima vez)
  2. Motilidade  8 direções + longe (PPO) + perto; ESPAÇO registra os dois
                 olhos; ao final salva a página de resultado (PNG, PDF e HTML)
  3. Gravação    gravar / pausar / continuar / salvar / descartar, com FPS
                 alvo; salva um vídeo por olho, um lado a lado e os
                 tempos de cada quadro

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

VERSION = "1.1"
CONFIG_PATH = Path.home() / ".oculodiag.json"

EYES = ("OD", "OE")
EYE_NAMES = {"OD": "olho direito", "OE": "olho esquerdo"}

# Modos de captura. "fps" é o que se PEDE ao driver (ficha técnica); o que
# a câmera entrega de verdade é medido e mostrado ao vivo. Em bancada:
# 320x240 -> ~123 fps, 640x480 -> ~29 fps.
MODES = {
    "rapido":  {"w": 320, "h": 240, "fps": 128, "rotulo": "320×240 · rápido"},
    "detalhe": {"w": 640, "h": 480, "fps": 30,  "rotulo": "640×480 · detalhe"},
}
MOTILITY_MODE = "detalhe"   # fotos estáticas: resolução vale mais que FPS

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
        "cameras": {e: {"id": None, "index": None, "rotation": rot, "flip": False, "roi": None}
                    for e in EYES},
        "saida": str(Path.home() / "OculoDiag"),
        "gravacao": {"modo": "rapido", "fps": 123},
    }


def load_config(path: Path, sim: bool = False) -> dict:
    cfg = default_config(sim)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for e in EYES:
            cfg["cameras"][e].update(data.get("cameras", {}).get(e, {}))
        cfg["saida"] = data.get("saida", cfg["saida"])
        cfg["gravacao"].update(data.get("gravacao", {}))
    except FileNotFoundError:
        pass
    except Exception as ex:  # arquivo corrompido: começa do padrão
        print(f"[config] ignorando {path}: {ex}")
    for e in EYES:
        cfg["cameras"][e]["roi"] = clean_roi(cfg["cameras"][e].get("roi"))
    if cfg["gravacao"].get("modo") not in MODES:
        cfg["gravacao"]["modo"] = "rapido"
    return cfg


# ── recorte (ROI) ──────────────────────────────────────────────────────
# O recorte é guardado em FRAÇÕES do quadro já orientado (x, y, w, h em
# 0..1). Assim ele vale igual em 320x240 e 640x480 (o sensor entrega o
# mesmo campo de visão nos dois modos) e acompanha giro/espelhamento.
ROI_MIN = 0.06   # menor lado aceito, em fração do quadro


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
    O movimento é conjugado: as duas câmeras simuladas seguem o mesmo alvo."""
    target = None
    _goal = (0.0, 0.0)
    _next_sacc = 0.0

    def __init__(self, index: int, w: int, h: int, fps: float):
        self.index, self.w, self.h, self.fps = index, w, h, fps
        self.t_next = time.monotonic()
        self.t_last = self.t_next
        self.gx = self.gy = 0.0
        rng = np.random.default_rng(index)
        self.noise = [rng.integers(0, 14, (h, w, 3), dtype=np.uint8) for _ in range(5)]
        self.k = 0
        s = h / 240
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
        eye = np.full((h, w, 3), 200, np.uint8)
        ix, iy = int(w / 2 + self.gx * 60 * s), int(h / 2 + self.gy * 34 * s)
        cv2.circle(eye, (ix, iy), int(42 * s), (92, 92, 92), -1, cv2.LINE_AA)
        cv2.circle(eye, (ix, iy), int(15 * s), (28, 28, 28), -1, cv2.LINE_AA)
        cv2.circle(eye, (ix + int(9 * s), iy - int(9 * s)), max(2, int(4 * s)), (250, 250, 250), -1, cv2.LINE_AA)
        img = (eye * self.mask + self.skin * (1 - self.mask)).astype(np.uint8)
        img = cv2.add(img, self.noise[self.k % len(self.noise)])
        self.k += 1
        cv2.putText(img, f"SIM #{self.index}", (int(8 * s), int(22 * s)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5 * s, (170, 170, 170), 1, cv2.LINE_AA)
        return True, img


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
        self.roi = None          # [x, y, w, h] em frações, ou None
        self.index: int | None = None
        self.mode_key: str | None = None
        self.status = "fechada"
        self.note = ""
        self.n = 0
        self._times: deque = deque(maxlen=300)

    # -- abrir / fechar ---------------------------------------------------
    def _open_cap(self, index: int, m: dict):
        if self.sim:
            return SimCapture(index, m["w"], m["h"], m["fps"]), ""
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
            px = roi_pixels(self.roi, full.shape[1], full.shape[0])
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
    """As duas câmeras + a configuração. Abrir/fechar é lento e às vezes
    trava por segundos, então toda operação de hardware roda serializada
    numa thread própria (nunca duas aberturas simultâneas do mesmo
    dispositivo -- causa de 'Device busy' numa versão anterior)."""

    def __init__(self, cfg: dict, save_cb=None, sim: bool = False, backend: str = "auto"):
        self.cfg = cfg
        self.save_cb = save_cb or (lambda: None)
        self.sim = sim
        self.backend = backend
        self.cams = {e: Camera(e, backend, sim) for e in EYES}
        for e in EYES:
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
        for e in EYES:
            cc = self.cfg["cameras"][e]
            idx = by_id.get(cc.get("id")) if cc.get("id") else None
            if idx is None and cc.get("index") in idxs and cc.get("index") not in used:
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
            for e in EYES:
                self._assign(e, idx[e])
                self.cams[e].open(idx[e], mode_key)
            self.save_cb()
        self.mode_key = mode_key
        self._submit(job)

    def set_mode(self, mode_key: str, force: bool = False):
        if mode_key == self.mode_key and not force:
            return
        self.mode_key = mode_key

        def job():
            # fecha as duas antes de abrir: libera banda USB e evita 'busy'
            idx = {e: self.cams[e].index for e in EYES}
            for c in self.cams.values():
                c.close()
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
        outro olho, equivale a trocar OD <-> OE."""
        idxs = [c["index"] for c in self.available]
        if not idxs:
            return
        cur = self.cams[eye].index
        pos = idxs.index(cur) if cur in idxs else -1
        new = idxs[(pos + step) % len(idxs)]
        other = "OE" if eye == "OD" else "OD"
        if new == self.cams[other].index:
            self.swap()
            return
        if new == cur:
            return
        mode = self.mode_key

        def job():
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


def build_side_by_side(od_path: Path, oe_path: Path, od_ts: list, oe_ts: list,
                       out_path: Path, fps: float, t0: float, progress=None) -> int:
    """Vídeo OD | OE pareando cada quadro do OD com o quadro do OE mais
    próximo no tempo (as câmeras não são sincronizadas por hardware)."""
    ca = cv2.VideoCapture(str(od_path), cv2.CAP_OPENCV_MJPEG)
    cb = cv2.VideoCapture(str(oe_path), cv2.CAP_OPENCV_MJPEG)
    if not ca.isOpened():
        ca = cv2.VideoCapture(str(od_path))
    if not cb.isOpened():
        cb = cv2.VideoCapture(str(oe_path))
    ok, fb = cb.read()
    if not ok:
        return 0
    j, n, writer = 0, 0, None
    total = max(1, len(od_ts))
    for i, t in enumerate(od_ts):
        ok, fa = ca.read()
        if not ok:
            break
        while j + 1 < len(oe_ts) and abs(oe_ts[j + 1] - t) <= abs(oe_ts[j] - t):
            ok2, img = cb.read()
            if not ok2:
                break
            j += 1
            fb = img
        H = max(fa.shape[0], fb.shape[0])
        a = fa if fa.shape[0] == H else cv2.resize(fa, (int(fa.shape[1] * H / fa.shape[0]), H))
        b = fb if fb.shape[0] == H else cv2.resize(fb, (int(fb.shape[1] * H / fb.shape[0]), H))
        gap = np.full((H, 4, 3), 40, np.uint8)
        frame = np.hstack([a, gap, b])
        s = max(0.4, H / 480)
        for txt, x in (("OD", 6), ("OE", a.shape[1] + 10)):
            cv2.putText(frame, txt, (x, int(20 * s + 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * s,
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
    ca.release()
    cb.release()
    return n


class Recorder:
    """Um registro: pode ser pausado e continuado várias vezes (cada trecho
    é um 'segmento'), e no fim é salvo ou descartado.

    FPS: guarda o maior FPS possível até o alvo. Se a câmera entrega mais
    que o alvo, os quadros são dizimados por relógio (próximo quadro devido
    a cada 1/alvo s), o que dá o alvo em média; se entrega menos, guarda
    todos. O FPS gravado no arquivo é o EFETIVO medido."""

    def __init__(self, rig: CameraRig, out_root: Path, patient: str, target_fps: float, mode_key: str):
        self.rig = rig
        self.out_root = Path(out_root).expanduser()
        self.patient = patient
        self.target = float(target_fps)
        self.mode_key = mode_key
        self.state = "idle"     # idle rec paused saving saved discarded error
        self.progress = 0.0
        self.error = None
        self.saved_path: Path | None = None
        self.summary: dict = {}

    # -- controle ---------------------------------------------------------
    def start(self):
        self.started_at = datetime.now()
        self.out_root.mkdir(parents=True, exist_ok=True)
        self.tmp = self.out_root / f".gravando_{self.started_at:%Y%m%d_%H%M%S}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.cam_fps = {e: self.rig.cams[e].fps for e in EYES}
        self.cam_info = {e: {"camera": self.rig.label_for(self.rig.cams[e].index),
                             "rotacao": self.rig.cams[e].rotation,
                             "espelhado": self.rig.cams[e].flip,
                             "recorte_fracao_xywh": self.rig.cams[e].roi} for e in EYES}
        hdr = {e: (min(self.target, self.cam_fps[e]) if self.cam_fps[e] > 1 else self.target) for e in EYES}
        self.writers = {e: EyeWriter(self.tmp / f"{e}.avi", hdr[e]) for e in EYES}
        self.cam_period = {e: 1.0 / self.cam_fps[e] if self.cam_fps[e] > 1 else 1.0 / 120 for e in EYES}
        self.next_due = {e: None for e in EYES}
        self.segment = 1
        self.t0 = time.monotonic()
        self.seg_start = self.t0
        self.active = 0.0
        self._hooks = []
        for e in EYES:
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
            self.next_due = {e: None for e in EYES}
            self.seg_start = time.monotonic()
            self.state = "rec"

    def elapsed(self) -> float:
        if self.state == "rec":
            return self.active + time.monotonic() - self.seg_start
        return self.active

    def frames(self, eye: str) -> int:
        return len(self.writers[eye].rows) if hasattr(self, "writers") else 0

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

            combined = None
            wa, wb = self.writers["OD"], self.writers["OE"]
            if wa.rows and wb.rows:
                combined = "OD_OE_lado_a_lado.avi"
                n = build_side_by_side(wa.path, wb.path, [r[1] for r in wa.rows], [r[1] for r in wb.rows],
                                       self.tmp / combined, eff["OD"], self.t0,
                                       progress=lambda p: setattr(self, "progress", 0.12 + 0.83 * p))
                patch_avi_fps(self.tmp / combined, n / active)
            self.progress = 0.96

            self.summary = {
                "programa": f"OculoDiag {VERSION}",
                "paciente": self.patient,
                "inicio": self.started_at.isoformat(timespec="seconds"),
                "duracao_gravada_s": round(self.active, 3),
                "segmentos": self.segment,
                "fps_alvo": self.target,
                "modo": MODES[self.mode_key]["rotulo"],
                "video_lado_a_lado": combined,
                "olhos": {e: {
                    "arquivo": f"{e}.avi" if self.writers[e].rows else None,
                    "quadros": len(self.writers[e].rows),
                    "fps_efetivo": round(eff[e], 2),
                    "fps_camera_no_inicio": round(self.cam_fps[e], 2),
                    "resolucao": list(self.writers[e].size) if self.writers[e].size else None,
                    "perdidos_fila_disco": self.writers[e].dropped,
                    "erro": self.writers[e].error,
                    **self.cam_info[e]} for e in EYES},
                "observacao": ("Vídeos na vista do examinador (OD à esquerda). Pausas não aparecem nos "
                               "vídeos: veja t_s e 'segmento' em quadros.csv. t_s é o relógio do "
                               "computador na chegada do quadro; t_driver_s, quando existe, é o "
                               "timestamp do driver V4L2."),
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
            tk.Label(head, text=eye, font=fonts["h2"], fg=C["text"], bg=C["card"]).pack(side="left")
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

        def show(self, fr: Frame | None, info: str = "", msg: str = "", edit=None):
            """`edit` = None (normal) ou {"rect": [x,y,w,h] | None}: mostra o
            quadro INTEIRO com o recorte desenhado por cima."""
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
            key = (fr.n, (cw, ch), edit is not None, rect)
            if self.last == key and self.img_id is not None:
                return
            src = fr.full if (edit is not None and fr.full is not None) else fr.img
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
                msg = "nenhuma câmera atribuída\n(use ◀ ▶ na tela Câmeras)"
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
                      "motilidade e para a gravação."),
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
            row.columnconfigure(0, weight=1)
            row.columnconfigure(2, weight=1)
            row.rowconfigure(0, weight=1)
            self.previews, self.dev_labels, self.flip_btns = {}, {}, {}
            self.roi_btn, self.roi_clear_btn, self.roi_rows = {}, {}, {}
            self.editing = None        # olho cujo recorte está sendo editado
            self.edit_rect = None
            self._drag = None
            for eye, col in (("OD", 0), ("OE", 2)):
                box = tk.Frame(row, bg=C["bg"])
                box.grid(row=0, column=col, sticky="nsew")
                p = Preview(box, eye, F)
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
                RoundButton(ctl, "▶", lambda e=eye: self.cycle(e, 1), font=F["btn"], width=38).pack(side="right")
                lbl = tk.Label(ctl, text="", font=F["small"], fg=C["muted"], bg=C["bg"], width=24)
                lbl.pack(side="right", padx=6)
                self.dev_labels[eye] = lbl
                RoundButton(ctl, "◀", lambda e=eye: self.cycle(e, -1), font=F["btn"], width=38).pack(side="right")

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
            self.edit_rect = list(self.app.rig.cams[eye].roi) if self.app.rig.cams[eye].roi else None
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
            eye, rect = self.editing, clean_roi(self.edit_rect)
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
            if len(self.app.rig.available) < 2 and self.app.rig.cams[eye].index is not None:
                self.app.toast.show("Só uma câmera encontrada.", "warn")
            self.app.rig.cycle(eye, step)

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
            for eye in EYES:
                fr, info, msg = self.cam_view(eye)
                cam = rig.cams[eye]
                extra = f"  ·  {cam.rotation}°" + ("  ·  espelhado" if cam.flip else "")
                edit = {"rect": self.edit_rect} if self.editing == eye else None
                if edit is not None and fr is not None and fr.full is not None:
                    info = f"{cam.fps:5.1f} fps · editando recorte"
                elif fr is not None and cam.roi:
                    extra += "  ·  ✂"
                self.previews[eye].show(fr, info + extra if fr is not None else info, msg, edit=edit)
                self.roi_clear_btn[eye].set_enabled(cam.roi is not None)
                self.dev_labels[eye].configure(text=rig.label_for(cam.index))
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

            pv = tk.Frame(self, bg=C["bg"])
            pv.pack(fill="both", expand=True, padx=20, pady=(16, 12))
            pv.columnconfigure(0, weight=1, uniform="p")
            pv.columnconfigure(1, weight=1, uniform="p")
            pv.rowconfigure(0, weight=1)
            self.previews = {}
            for k, eye in enumerate(EYES):
                p = Preview(pv, eye, F)
                p.grid(row=0, column=k, sticky="nsew", padx=(0, 8) if k == 0 else (8, 0))
                self.previews[eye] = p

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
            self._update_buttons()

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
            self.rec = Recorder(rig, Path(self.app.cfg["saida"]), self.app.patient_raw(),
                                self.slider.value, rig.mode_key)
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

        def tick(self):
            rig, rec = self.app.rig, self.rec
            st = rec.state if rec else "idle"
            for eye in EYES:
                fr, info, msg = self.cam_view(eye)
                p = self.previews[eye]
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
                self.eff_lbl.configure(text=f"câmeras: OD {cams['OD']:.1f} · OE {cams['OE']:.1f} fps\n"
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
                self.count_lbl.configure(
                    text=f"OD {rec.frames('OD'):>6} q · {rec.frames('OD') / el:5.1f} fps{seg}\n"
                         f"OE {rec.frames('OE'):>6} q · {rec.frames('OE') / el:5.1f} fps{drop}")
            elif rec and st in ("saving", "stopping"):
                self.count_lbl.configure(text=f"salvando… {int(rec.progress * 100)}%")
            elif st in ("idle", "discarded"):
                self.timer_lbl.configure(text="00:00.0")
                self.count_lbl.configure(text="")

            if rec and st == "saved" and rec.saved_path != self.last_saved:
                self.last_saved = rec.saved_path
                s = rec.summary["olhos"]
                self.saved_lbl.configure(
                    text=f"✓ Salvo em {rec.saved_path.name}   ·   OD {s['OD']['quadros']} quadros a "
                         f"{s['OD']['fps_efetivo']:.1f} fps · OE {s['OE']['quadros']} a {s['OE']['fps_efetivo']:.1f} fps")
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
            self.tabs = Segmented(top, [("cam", "1  Câmeras"), ("mot", "2  Motilidade"), ("rec", "3  Gravação")],
                                  self.show, self.fonts["btn"], height=36)
            self.tabs.pack(side="left", padx=30)
            pt = tk.Frame(top, bg=C["panel"])
            pt.pack(side="right", padx=20)
            tk.Label(pt, text="Paciente", font=self.fonts["small"], fg=C["muted"], bg=C["panel"]).pack(side="left")
            self.patient = tk.Entry(pt, font=self.fonts["body"], bg=C["card2"], fg=C["text"], width=20,
                                    insertbackground=C["text"], relief="flat", highlightthickness=1,
                                    highlightbackground=C["border"], highlightcolor=C["accent"])
            self.patient.pack(side="left", padx=(8, 0), ipady=5, ipadx=6)

            content = tk.Frame(root, bg=C["bg"])
            content.pack(fill="both", expand=True)
            content.rowconfigure(0, weight=1)
            content.columnconfigure(0, weight=1)
            self.screens = {"cam": CamerasScreen(content, self), "mot": MotilityScreen(content, self),
                            "rec": RecordingScreen(content, self)}
            for s in self.screens.values():
                s.grid(row=0, column=0, sticky="nsew")

            status = tk.Frame(root, bg=C["panel"])
            status.pack(fill="x", side="bottom")
            self.status_l = tk.Label(status, text="", font=self.fonts["mono"], fg=C["muted"], bg=C["panel"])
            self.status_l.pack(side="left", padx=20, pady=6)
            self.status_r = tk.Label(status, text="F1 F2 F3 trocam de tela", font=self.fonts["small"],
                                     fg=C["faint"], bg=C["panel"])
            self.status_r.pack(side="right", padx=20)

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
            if e.keysym in ("F1", "F2", "F3"):
                self.show({"F1": "cam", "F2": "mot", "F3": "rec"}[e.keysym])
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
                    for e in EYES:
                        cam = self.rig.cams[e]
                        parts.append(f"{e} {self.rig.label_for(cam.index).split(' · ')[0]} "
                                     f"{cam.fps:5.1f} fps" if cam.ok else f"{e} {cam.status}")
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
