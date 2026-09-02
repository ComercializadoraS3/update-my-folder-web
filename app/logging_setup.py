"""Registro de diagnostico.

Una aplicacion de ventana empaquetada no tiene consola: si algo revienta, el
usuario ve un cuadro de dialogo y el detalle se pierde. Aqui todo queda
siempre en un archivo rotativo, y con el modo depuracion ademas sale por la
consola en tiempo real.

    %APPDATA%/UpdateMyFolder/logs/app.log      diagnostico (rotativo)
    %APPDATA%/UpdateMyFolder/logs/sync-*.log   resumen de cada copia

Activar el modo depuracion:

    python main.py --debug           o        set UMF_DEBUG=1
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler

from .config import log_dir

ENV_FLAG = "UMF_DEBUG"
FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)-14s] %(name)-12s %(message)s"
_configured = False


def env_debug() -> bool:
    return os.environ.get(ENV_FLAG, "").strip().lower() not in ("", "0", "false", "no")


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"umf.{name}")


def setup(debug: bool = False) -> logging.Logger:
    """Configura el registro. Idempotente: llamarlo dos veces no duplica lineas."""
    global _configured
    root = logging.getLogger("umf")
    if _configured:
        return root

    debug = debug or env_debug()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.propagate = False
    formatter = logging.Formatter(FORMAT, datefmt="%H:%M:%S")

    try:
        handler = RotatingFileHandler(
            log_dir() / "app.log", maxBytes=1 << 20, backupCount=3, encoding="utf-8")
        handler.setFormatter(formatter)
        root.addHandler(handler)
    except OSError:
        pass                    # sin registro en disco la aplicacion sigue viva

    # Empaquetado sin consola, sys.stderr es None: escribir ahi seria un error.
    if debug and sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        root.addHandler(console)

    _install_excepthooks(root)
    _configured = True
    root.info("--- inicio  debug=%s  frozen=%s ---", debug, getattr(sys, "frozen", False))
    return root


def _install_excepthooks(logger: logging.Logger) -> None:
    """Nada que se rompa deberia desaparecer sin dejar rastro en el archivo."""
    previous = sys.excepthook

    def hook(exc_type, exc, tb):
        logger.critical("Excepcion sin capturar", exc_info=(exc_type, exc, tb))
        previous(exc_type, exc, tb)

    sys.excepthook = hook

    def thread_hook(args):
        logger.critical("Excepcion sin capturar en el hilo %s", args.thread.name,
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = thread_hook
