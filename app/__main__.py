"""Punto de entrada: python -m app  (o el ejecutable empaquetado)."""

from __future__ import annotations

import argparse
import sys
import traceback

import customtkinter as ctk

from .config import AppConfig
from .logging_setup import setup
from .version import APP_TITLE, __version__


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="UpdateMyFolder", add_help=True)
    parser.add_argument("--debug", action="store_true",
                        help="registro detallado por consola y trazas completas en la interfaz")
    parser.add_argument("--version", action="version", version=f"{APP_TITLE} {__version__}")
    # Windows puede pasar argumentos propios al abrir desde un acceso directo;
    # se ignoran en vez de abortar el arranque.
    known, _ = parser.parse_known_args(argv)
    return known


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log = setup(debug=args.debug)

    config = AppConfig.load()
    ctk.set_appearance_mode(config.appearance)
    ctk.set_default_color_theme("blue")

    try:
        from .ui.main_window import MainWindow
        window = MainWindow(config, debug=args.debug)
    except Exception:                                       # noqa: BLE001
        # Sin ventana todavia no hay donde mostrar el error: va al registro,
        # a la consola y a un cuadro nativo para el usuario empaquetado.
        log.critical("Fallo al construir la ventana", exc_info=True)
        traceback.print_exc()
        try:
            from tkinter import messagebox
            messagebox.showerror(APP_TITLE, traceback.format_exc())
        except Exception:                                   # noqa: BLE001
            pass
        return 1

    window.mainloop()
    log.info("--- fin ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
