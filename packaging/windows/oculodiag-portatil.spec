# -*- mode: python ; coding: utf-8 -*-
# Versão portátil do OculoDiag: um único .exe, sem instalação.
# Rodar a partir da raiz do projeto:  pyinstaller packaging\windows\oculodiag-portatil.spec
import re
from pathlib import Path

ROOT = Path(SPECPATH).parents[1]
ICON = Path(SPECPATH) / "oculodiag.ico"
VERSION = re.search(r'^VERSION = "(.+)"', (ROOT / "oculo_diag.py").read_text(encoding="utf-8"), re.M).group(1)

a = Analysis(
    [str(ROOT / "oculo_diag.py")],
    pathex=[str(ROOT)],
    hiddenimports=["PIL.ImageTk", "PIL._tkinter_finder"],
    excludes=["matplotlib", "scipy", "pandas", "IPython", "pytest"],
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=f"OculoDiag-{VERSION}-portatil",
    console=False,
    icon=str(ICON) if ICON.exists() else None,
    upx=False,
)
