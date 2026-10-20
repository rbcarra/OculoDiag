# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec do OculoDiag para Windows.
# Rodar a partir da raiz do projeto:  pyinstaller packaging\windows\oculodiag.spec
from pathlib import Path

ROOT = Path(SPECPATH).parents[1]
ICON = Path(SPECPATH) / "oculodiag.ico"

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
    [],
    exclude_binaries=True,
    name="OculoDiag",
    console=False,           # app Tk: sem janela de console
    icon=str(ICON) if ICON.exists() else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="OculoDiag", upx=False)
