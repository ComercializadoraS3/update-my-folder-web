"""Puentes de estilo entre CustomTkinter y los widgets ttk clasicos.

CustomTkinter no trae tabla. La lista de resultados usa ttk.Treeview, que es
el unico widget de la biblioteca estandar que aguanta miles de filas con
columnas y ordenamiento. Aqui se le aplican los colores del tema activo de
CTk para que no desentone con el resto de la ventana.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import customtkinter as ctk

# Colores por estado de fila, en pares (claro, oscuro).
STATUS_COLORS = {
    "NUEVO":      ("#1a7f37", "#57d977"),
    "MODIFICADO": ("#9a6700", "#e3b341"),
    "SOBRANTE":   ("#a40e26", "#ff7b72"),
    "OK":         ("#57606a", "#8b949e"),
    "ERROR":      ("#a40e26", "#ff7b72"),
}


def _mode_index() -> int:
    return 1 if ctk.get_appearance_mode() == "Dark" else 0


def pick(pair) -> str:
    """Elige el color del par (claro, oscuro) segun la apariencia activa."""
    if isinstance(pair, (list, tuple)):
        return pair[_mode_index()]
    return pair


def theme_color(widget: str, key: str) -> str:
    return pick(ctk.ThemeManager.theme[widget][key])


def apply_treeview_style(tree_style_name: str = "UMF.Treeview") -> ttk.Style:
    """Reviste el Treeview con la paleta de CustomTkinter."""
    style = ttk.Style()
    try:
        style.theme_use("clam")     # el unico tema ttk que permite recolorear todo
    except tk.TclError:
        pass

    surface = theme_color("CTkFrame", "fg_color")
    text = theme_color("CTkLabel", "text_color")
    accent = theme_color("CTkButton", "fg_color")
    accent_text = theme_color("CTkButton", "text_color")
    header = theme_color("CTkFrame", "top_fg_color")
    border = theme_color("CTkFrame", "border_color")

    style.configure(
        tree_style_name,
        background=surface,
        fieldbackground=surface,
        foreground=text,
        borderwidth=0,
        rowheight=24,
        font=("Segoe UI", 10),
    )
    style.map(
        tree_style_name,
        background=[("selected", accent)],
        foreground=[("selected", accent_text)],
    )
    style.configure(
        f"{tree_style_name}.Heading",
        background=header,
        foreground=text,
        relief="flat",
        borderwidth=0,
        padding=(8, 6),
        font=("Segoe UI Semibold", 10),
    )
    style.map(f"{tree_style_name}.Heading", background=[("active", border)])
    style.layout(tree_style_name, [
        (f"{tree_style_name}.treearea", {"sticky": "nswe"})
    ])
    return style


def configure_row_tags(tree: ttk.Treeview) -> None:
    for name, pair in STATUS_COLORS.items():
        tree.tag_configure(name, foreground=pick(pair))
