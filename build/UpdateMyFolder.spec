# -*- mode: python ; coding: utf-8 -*-
"""Especificacion de PyInstaller.

Se empaqueta en modo carpeta (onedir) a proposito. Con onefile cada arranque
descomprime la aplicacion entera en un temporal, lo que la vuelve lenta al
abrir y complica el esquema de versiones del actualizador. Con onedir el
contenido ya esta en disco y actualizar es extraer una carpeta nueva.

Uso:  pyinstaller build/UpdateMyFolder.spec --noconfirm
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))
from app.version import APP_NAME  # noqa: E402

# CustomTkinter carga sus temas desde archivos JSON en tiempo de ejecucion;
# sin esto la aplicacion empaquetada arranca sin estilos y falla.
datas = collect_data_files("customtkinter")

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=["app", "app.ui", "darkdetect"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "numpy", "pandas", "matplotlib", "PIL.ImageQt"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Aplicacion de ventana: sin consola detras. Con UMF_CONSOLE=1 se compila
    # una variante con consola, que es la unica forma de ver una traza cuando
    # el fallo solo se reproduce empaquetado (sin consola, stderr no existe y
    # el error se pierde antes de llegar a ningun lado).
    console=bool(os.environ.get("UMF_CONSOLE")),
    icon=str(ROOT / "build" / "icon.ico") if (ROOT / "build" / "icon.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
