"""Punto de entrada de nivel superior.

Existe por PyInstaller: al empaquetar, el script de arranque se ejecuta como
'__main__' suelto, y desde ahi las importaciones relativas de app/__main__.py
(`from .config import ...`) fallan por no tener paquete padre. Este archivo
importa el paquete de forma absoluta, que funciona igual empaquetado y en
desarrollo.

    python main.py          (desarrollo)
    UpdateMyFolder.exe      (empaquetado, mismo codigo)
"""

import sys

from app.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
