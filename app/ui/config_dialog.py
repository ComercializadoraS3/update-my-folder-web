"""Dialogo de configuracion: perfiles, reglas y ajustes avanzados."""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from ..comparer import format_size
from ..config import AppConfig, Profile, config_path, data_dir, log_dir
from ..rules import RuleSet, compile_rule
from ..scanner import scan_tree
from ..version import APP_TITLE, __version__
from .theme import apply_treeview_style

SYNTAX_HELP = (
    "*.cs            todos los archivos con esa extension, a cualquier profundidad\n"
    "ejemplo.cs      solo los archivos con ese nombre exacto\n"
    "/reorgs/        la carpeta 'reorgs' en la raiz del origen\n"
    "reorgs/         cualquier carpeta llamada 'reorgs', a cualquier profundidad\n"
    "/src/app/*.cs   ruta anclada a la raiz, con comodin\n"
    "**/obj/         igual que 'obj/'\n\n"
    "Un patron por linea. Las lineas que empiezan con # son comentarios.\n"
    "Si la lista de inclusion esta vacia entra todo. La omision siempre gana."
)


class ConfigDialog(ctk.CTkToplevel):
    def __init__(self, parent, config: AppConfig):
        super().__init__(parent)
        self.cfg = config
        self.parent = parent
        self.editing = config.active().name

        self.title("Configuracion")
        self.geometry("900x680")
        self.minsize(820, 620)
        self.transient(parent)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(self)
        self.tabs.grid(row=0, column=0, sticky="nsew", padx=14, pady=(14, 0))
        for name in ("Perfiles y rutas", "Reglas", "Avanzado"):
            self.tabs.add(name)

        self._build_profiles(self.tabs.tab("Perfiles y rutas"))
        self._build_rules(self.tabs.tab("Reglas"))
        self._build_advanced(self.tabs.tab("Avanzado"))

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=1, column=0, sticky="ew", padx=14, pady=12)
        footer.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(footer, text=f"Configuracion: {config_path()}",
                     anchor="w", text_color=("gray45", "gray60")).grid(
            row=0, column=0, sticky="w")
        ctk.CTkButton(footer, text="Cerrar", width=120, command=self.close).grid(
            row=0, column=1)

        self.load_profile(self.editing)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(120, self.grab_set)      # tras dibujarse, para no parpadear

    # ------------------------------------------------------------------ perfiles

    def _build_profiles(self, tab) -> None:
        tab.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(tab, text="Perfil", font=("Segoe UI Semibold", 13)).grid(
            row=0, column=0, sticky="w", padx=8, pady=(12, 6))
        self.profile_menu = ctk.CTkOptionMenu(
            tab, values=self.cfg.profile_names(), width=260, command=self.on_switch)
        self.profile_menu.grid(row=0, column=1, sticky="w", pady=(12, 6))

        buttons = ctk.CTkFrame(tab, fg_color="transparent")
        buttons.grid(row=1, column=1, sticky="w", pady=(0, 14))
        for col, (text, command) in enumerate((
                ("Nuevo", self.new_profile),
                ("Duplicar", self.duplicate_profile),
                ("Renombrar", self.rename_profile),
                ("Eliminar", self.delete_profile))):
            ctk.CTkButton(buttons, text=text, width=104, fg_color="transparent",
                          border_width=1, command=command).grid(
                row=0, column=col, padx=(0, 8))

        self.source_var = tk.StringVar()
        self.dest_var = tk.StringVar()
        for row, (label, var, hint) in enumerate((
                ("Ruta de origen", self.source_var, "De donde se leen los archivos."),
                ("Ruta de destino", self.dest_var, "A donde se copian. Se crea si no existe."))):
            ctk.CTkLabel(tab, text=label, anchor="w").grid(
                row=2 + row * 2, column=0, sticky="w", padx=8, pady=(10, 0))
            row_frame = ctk.CTkFrame(tab, fg_color="transparent")
            row_frame.grid(row=2 + row * 2, column=1, sticky="ew", pady=(10, 0))
            row_frame.grid_columnconfigure(0, weight=1)
            ctk.CTkEntry(row_frame, textvariable=var, height=34).grid(
                row=0, column=0, sticky="ew")
            ctk.CTkButton(row_frame, text="Examinar", width=100,
                          command=lambda v=var: self.browse(v)).grid(
                row=0, column=1, padx=(8, 0))
            ctk.CTkLabel(tab, text=hint, anchor="w",
                         text_color=("gray45", "gray60")).grid(
                row=3 + row * 2, column=1, sticky="w", pady=(2, 0))

        ctk.CTkLabel(
            tab, justify="left", anchor="w", text_color=("gray45", "gray60"),
            text=("Cada perfil guarda sus propias rutas, reglas y opciones.\n"
                  "Se aplican en cuanto cierras esta ventana y quedan guardados "
                  "para la proxima vez que abras el programa.")).grid(
            row=8, column=1, sticky="w", pady=(24, 0))

    def load_profile(self, name: str) -> None:
        self.editing = name
        p = self._profile()
        self.profile_menu.configure(values=self.cfg.profile_names())
        self.profile_menu.set(name)
        self.source_var.set(p.source)
        self.dest_var.set(p.dest)
        self.include_box.delete("1.0", "end")
        self.include_box.insert("1.0", "\n".join(p.include))
        self.exclude_box.delete("1.0", "end")
        self.exclude_box.insert("1.0", "\n".join(p.exclude))
        self.threads_var.set(str(p.threads or 0))
        self.tolerance_var.set(str(p.mtime_tolerance))
        self.validate_rules()

    def _profile(self) -> Profile:
        for p in self.cfg.profiles:
            if p.name == self.editing:
                return p
        return self.cfg.profiles[0]

    def flush(self) -> None:
        """Vuelca el contenido de los widgets al perfil que se esta editando."""
        p = self._profile()
        p.source = self.source_var.get().strip()
        p.dest = self.dest_var.get().strip()
        p.include = _lines(self.include_box.get("1.0", "end"))
        p.exclude = _lines(self.exclude_box.get("1.0", "end"))
        p.threads = _as_int(self.threads_var.get(), 0, low=0, high=256)
        p.mtime_tolerance = _as_float(self.tolerance_var.get(), 2.0, low=0.0, high=3600.0)
        self.cfg.max_rows_display = _as_int(self.rows_var.get(), 5000, low=500, high=200_000)
        self.cfg.update_url = self.update_url_var.get().strip()
        self.cfg.auto_check_updates = bool(self.auto_update_var.get())

    def on_switch(self, name: str) -> None:
        self.flush()
        self.cfg.active_profile = name
        self.load_profile(name)

    def new_profile(self) -> None:
        name = _ask_text(self, "Nuevo perfil", "Nombre del perfil:")
        if not name:
            return
        self.flush()
        profile = Profile(name=self.cfg.unique_name(name))
        self.cfg.profiles.append(profile)
        self.cfg.active_profile = profile.name
        self.load_profile(profile.name)

    def duplicate_profile(self) -> None:
        self.flush()
        source = self._profile()
        clone = Profile(**{**source.__dict__,
                           "name": self.cfg.unique_name(f"{source.name} copia")})
        self.cfg.profiles.append(clone)
        self.cfg.active_profile = clone.name
        self.load_profile(clone.name)

    def rename_profile(self) -> None:
        current = self._profile()
        name = _ask_text(self, "Renombrar perfil", "Nuevo nombre:", current.name)
        if not name or name == current.name:
            return
        self.flush()
        current.name = self.cfg.unique_name(name)
        self.cfg.active_profile = current.name
        self.load_profile(current.name)

    def delete_profile(self) -> None:
        if len(self.cfg.profiles) == 1:
            messagebox.showinfo(APP_TITLE, "Debe existir al menos un perfil.", parent=self)
            return
        current = self._profile()
        if not messagebox.askokcancel(
                APP_TITLE, f"Eliminar el perfil '{current.name}'?", parent=self):
            return
        self.cfg.profiles.remove(current)
        self.cfg.active_profile = self.cfg.profiles[0].name
        self.load_profile(self.cfg.active_profile)

    def browse(self, var: tk.StringVar) -> None:
        chosen = filedialog.askdirectory(initialdir=var.get() or None, parent=self)
        if chosen:
            var.set(os.path.normpath(chosen))

    # -------------------------------------------------------------------- reglas

    def _build_rules(self, tab) -> None:
        tab.grid_columnconfigure((0, 1), weight=1)
        tab.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(tab, text="Incluir  (vacio = todo)",
                     font=("Segoe UI Semibold", 13), anchor="w").grid(
            row=0, column=0, sticky="w", padx=8, pady=(12, 4))
        ctk.CTkLabel(tab, text="Omitir  (gana sobre incluir)",
                     font=("Segoe UI Semibold", 13), anchor="w").grid(
            row=0, column=1, sticky="w", padx=8, pady=(12, 4))

        self.include_box = ctk.CTkTextbox(tab, font=("Consolas", 12), undo=True)
        self.include_box.grid(row=1, column=0, sticky="nsew", padx=(8, 6))
        self.exclude_box = ctk.CTkTextbox(tab, font=("Consolas", 12), undo=True)
        self.exclude_box.grid(row=1, column=1, sticky="nsew", padx=(6, 8))
        for box in (self.include_box, self.exclude_box):
            box.bind("<KeyRelease>", lambda _e: self.validate_rules())

        self.rules_status = ctk.CTkLabel(tab, text="", anchor="w")
        self.rules_status.grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(8, 0))

        actions = ctk.CTkFrame(tab, fg_color="transparent")
        actions.grid(row=3, column=0, columnspan=2, sticky="ew", padx=8, pady=(6, 0))
        ctk.CTkButton(actions, text="Probar reglas contra el origen", width=250,
                      command=self.test_rules).grid(row=0, column=0)
        ctk.CTkButton(actions, text="Preajuste .NET", width=140, fg_color="transparent",
                      border_width=1,
                      command=lambda: self.apply_preset(_PRESET_DOTNET)).grid(
            row=0, column=1, padx=8)
        ctk.CTkButton(actions, text="Preajuste web", width=140, fg_color="transparent",
                      border_width=1,
                      command=lambda: self.apply_preset(_PRESET_WEB)).grid(row=0, column=2)

        help_box = ctk.CTkTextbox(tab, height=170, font=("Consolas", 11),
                                  fg_color=("gray92", "gray14"))
        help_box.grid(row=4, column=0, columnspan=2, sticky="ew", padx=8, pady=(12, 8))
        help_box.insert("1.0", SYNTAX_HELP)
        help_box.configure(state="disabled")

    def apply_preset(self, patterns: list[str]) -> None:
        existing = _lines(self.exclude_box.get("1.0", "end"))
        merged = existing + [p for p in patterns if p not in existing]
        self.exclude_box.delete("1.0", "end")
        self.exclude_box.insert("1.0", "\n".join(merged))
        self.validate_rules()

    def validate_rules(self) -> None:
        problems: list[str] = []
        counts = []
        for label, box in (("incluir", self.include_box), ("omitir", self.exclude_box)):
            patterns = _lines(box.get("1.0", "end"))
            valid = 0
            for pattern in patterns:
                try:
                    if compile_rule(pattern):
                        valid += 1
                except Exception:                           # noqa: BLE001
                    problems.append(f"{label}: {pattern}")
            counts.append(valid)
        if problems:
            self.rules_status.configure(
                text="Patrones no validos -> " + ", ".join(problems[:4]),
                text_color=("#a40e26", "#ff7b72"))
        else:
            self.rules_status.configure(
                text=f"{counts[0]} regla(s) de inclusion, {counts[1]} de omision.",
                text_color=("gray40", "gray65"))

    def test_rules(self) -> None:
        self.flush()
        p = self._profile()
        if not os.path.isdir(p.source):
            messagebox.showwarning(
                APP_TITLE, "Configura primero una ruta de origen valida.", parent=self)
            return
        RulesTester(self, p)

    # ------------------------------------------------------------------ avanzado

    def _build_advanced(self, tab) -> None:
        tab.grid_columnconfigure(1, weight=1)
        row = 0

        def field(label: str, hint: str, variable: tk.StringVar, width: int = 120):
            nonlocal row
            ctk.CTkLabel(tab, text=label, anchor="w").grid(
                row=row, column=0, sticky="w", padx=8, pady=(12, 0))
            ctk.CTkEntry(tab, textvariable=variable, width=width, height=32).grid(
                row=row, column=1, sticky="w", pady=(12, 0))
            ctk.CTkLabel(tab, text=hint, anchor="w",
                         text_color=("gray45", "gray60")).grid(
                row=row + 1, column=1, sticky="w")
            row += 2

        self.threads_var = tk.StringVar(value="0")
        self.tolerance_var = tk.StringVar(value="2.0")
        self.rows_var = tk.StringVar(value=str(self.cfg.max_rows_display))
        self.update_url_var = tk.StringVar(value=self.cfg.update_url)

        field("Hilos", "0 = automatico: pocos hilos en disco local (donde no aportan) "
                       "y muchos en rutas de red (donde manda la latencia).",
              self.threads_var)
        field("Tolerancia de fecha (s)",
              "Diferencia de fecha que se considera igual. 2 s cubre FAT32 y desfases de red.",
              self.tolerance_var)
        field("Filas maximas en pantalla",
              "Limite visual. La copia siempre usa la lista completa.", self.rows_var)
        field("URL de actualizacion",
              "Repositorio de GitHub (https://github.com/org/repo) o manifest.json propio.",
              self.update_url_var, width=520)

        self.auto_update_var = tk.BooleanVar(value=self.cfg.auto_check_updates)
        ctk.CTkCheckBox(tab, text="Comprobar actualizaciones al iniciar",
                        variable=self.auto_update_var).grid(
            row=row, column=1, sticky="w", pady=(14, 0))
        row += 1

        ctk.CTkLabel(tab, text="Apariencia", anchor="w").grid(
            row=row, column=0, sticky="w", padx=8, pady=(14, 0))
        self.appearance_menu = ctk.CTkOptionMenu(
            tab, values=["Sistema", "Claro", "Oscuro"], width=160,
            command=self.on_appearance)
        self.appearance_menu.set({"system": "Sistema", "light": "Claro",
                                  "dark": "Oscuro"}.get(self.cfg.appearance, "Sistema"))
        self.appearance_menu.grid(row=row, column=1, sticky="w", pady=(14, 0))
        row += 1

        folders = ctk.CTkFrame(tab, fg_color="transparent")
        folders.grid(row=row, column=1, sticky="w", pady=(24, 0))
        ctk.CTkButton(folders, text="Abrir carpeta de configuracion", width=230,
                      fg_color="transparent", border_width=1,
                      command=lambda: os.startfile(data_dir())).grid(row=0, column=0)
        ctk.CTkButton(folders, text="Abrir registros", width=150,
                      fg_color="transparent", border_width=1,
                      command=lambda: os.startfile(log_dir())).grid(
            row=0, column=1, padx=8)
        row += 1

        ctk.CTkLabel(tab, text=f"{APP_TITLE} v{__version__}",
                     text_color=("gray45", "gray60")).grid(
            row=row, column=1, sticky="w", pady=(20, 0))

    def on_appearance(self, choice: str) -> None:
        mapping = {"Sistema": "system", "Claro": "light", "Oscuro": "dark"}
        self.cfg.appearance = mapping[choice]
        ctk.set_appearance_mode(self.cfg.appearance)
        apply_treeview_style()

    # -------------------------------------------------------------------- cierre

    def close(self) -> None:
        self.flush()
        self.cfg.save()
        self.grab_release()
        self.destroy()


class RulesTester(ctk.CTkToplevel):
    """Muestra que archivos entran y cuales quedan fuera con las reglas actuales.

    Depurar patrones a ciegas es la forma mas rapida de copiar de menos sin
    darse cuenta, asi que conviene poder verlos aplicados sobre el arbol real.
    """

    def __init__(self, parent, profile: Profile):
        super().__init__(parent)
        self.title("Probar reglas")
        self.geometry("880x600")
        self.transient(parent)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self.cancel = threading.Event()

        ctk.CTkLabel(self, text=f"Origen: {profile.source}", anchor="w").grid(
            row=0, column=0, sticky="ew", padx=14, pady=(14, 4))
        self.summary = ctk.CTkLabel(self, text="Recorriendo...", anchor="w",
                                    font=("Segoe UI Semibold", 13))
        self.summary.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 8))

        frame = ctk.CTkFrame(self)
        frame.grid(row=2, column=0, sticky="nsew", padx=14)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)
        apply_treeview_style()
        self.tree = ttk.Treeview(frame, columns=("estado", "ruta", "tam"),
                                 show="headings", style="UMF.Treeview")
        for key, text, width in (("estado", "Resultado", 110),
                                 ("ruta", "Ruta relativa", 560),
                                 ("tam", "Tamano", 100)):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, stretch=(key == "ruta"))
        self.tree.column("tam", anchor="e")
        self.tree.tag_configure("ENTRA", foreground="#1a7f37")
        self.tree.tag_configure("FUERA", foreground="#8b949e")
        scroll = ctk.CTkScrollbar(frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        scroll.grid(row=0, column=1, sticky="ns", padx=(2, 8), pady=8)

        ctk.CTkButton(self, text="Cerrar", width=120, command=self.close).grid(
            row=3, column=0, sticky="e", padx=14, pady=12)
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.result: dict | None = None
        threading.Thread(target=self._work, args=(profile,), daemon=True).start()
        self.after(150, self._poll)

    def _work(self, profile: Profile) -> None:
        rules = RuleSet(profile.include, profile.exclude)
        # Dos recorridos: uno con reglas y otro sin ellas. La diferencia es
        # exactamente lo que las reglas estan descartando.
        filtered = scan_tree(profile.source, rules, self.cancel, profile.workers_for("scan"))
        everything = scan_tree(profile.source, None, self.cancel, profile.workers_for("scan"))
        self.result = {"filtered": filtered, "all": everything}

    def _poll(self) -> None:
        if self.result is None:
            self.after(150, self._poll)
            return
        kept = self.result["filtered"].entries
        total = self.result["all"].entries
        dropped = [e for rel, e in total.items() if rel not in kept]

        self.summary.configure(
            text=f"Entran {len(kept):,} de {len(total):,} archivos  ·  "
                 f"{format_size(sum(e.size for e in kept.values()))}  ·  "
                 f"quedan fuera {len(dropped):,}")

        for entry in list(kept.values())[:1500]:
            self.tree.insert("", "end", tags=("ENTRA",),
                             values=("ENTRA", entry.rel.replace("/", os.sep),
                                     format_size(entry.size)))
        for entry in dropped[:1500]:
            self.tree.insert("", "end", tags=("FUERA",),
                             values=("fuera", entry.rel.replace("/", os.sep),
                                     format_size(entry.size)))

    def close(self) -> None:
        self.cancel.set()
        self.destroy()


# ------------------------------------------------------------------- utilidades

_PRESET_DOTNET = ["bin/", "obj/", ".vs/", "packages/", "*.user", "*.suo",
                  "TestResults/", "*.pdb"]
_PRESET_WEB = ["node_modules/", "dist/", "build/", ".git/", ".cache/",
               "*.log", "coverage/"]


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _as_int(text: str, fallback: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(float(text))))
    except (TypeError, ValueError):
        return fallback


def _as_float(text: str, fallback: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(text)))
    except (TypeError, ValueError):
        return fallback


def _ask_text(parent, title: str, prompt: str, initial: str = "") -> str:
    dialog = ctk.CTkInputDialog(title=title, text=prompt)
    if initial:
        try:
            dialog._entry.insert(0, initial)
        except Exception:                                   # noqa: BLE001
            pass
    value = dialog.get_input()
    return (value or "").strip()
