"""Ventana principal.

Regla de oro del hilado: los widgets solo se tocan desde el hilo principal.
El trabajo pesado corre en un hilo aparte que publica eventos en una cola;
la ventana la drena cada 100 ms con `after`. Por eso la interfaz nunca se
congela ni aparece el "no responde" de Windows durante una copia larga.
"""

from __future__ import annotations

import datetime
import os
import queue
import subprocess
import threading
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from ..comparer import (Comparison, Status, compare, format_duration,
                        format_size, format_time)
from ..config import AppConfig, Profile, log_dir
from ..copier import CopyStats, run_copy
from ..logging_setup import get as get_logger
from ..rules import RuleSet
from ..scanner import scan_both
from ..updater import ReleaseInfo, UpdateError, check_for_update, install, restart_into
from ..version import APP_TITLE, __version__
from .theme import apply_treeview_style, configure_row_tags

PUMP_MS = 100
CHUNK_ROWS = 400        # filas insertadas por tanda para no bloquear el dibujado

log = get_logger("ui")

FILTERS = ("Todos", "Nuevos", "Modificados", "Sobrantes")
FILTER_STATUS = {
    "Nuevos": Status.NEW,
    "Modificados": Status.MODIFIED,
    "Sobrantes": Status.ORPHAN,
}


class MainWindow(ctk.CTk):
    def __init__(self, config: AppConfig, debug: bool = False):
        super().__init__()
        self.cfg = config
        self.debug = debug
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.stats: CopyStats | None = None
        self.comparison: Comparison | None = None
        self.row_of: dict[str, str] = {}
        self.item_of: dict[str, object] = {}
        self.pending_release: ReleaseInfo | None = None
        self._insert_job: str | None = None

        self.title(f"{APP_TITLE}  ·  v{__version__}")
        self.minsize(940, 600)
        if self.cfg.window_geometry:
            try:
                self.geometry(self.cfg.window_geometry)
            except tk.TclError:
                self.geometry("1120x740")
        else:
            self.geometry("1120x740")

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)

        self._build_header()
        self._build_paths()
        self._build_options()
        self._build_actions()
        self._build_update_banner()
        self._build_table()
        self._build_footer()

        self.load_profile()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(PUMP_MS, self._pump)
        if self.cfg.auto_check_updates and self.cfg.update_url.strip():
            self.after(1200, lambda: self.check_updates(silent=True))

    # ------------------------------------------------------------------ layout

    def _build_header(self) -> None:
        bar = ctk.CTkFrame(self, corner_radius=0)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(bar, text="Perfil", font=("Segoe UI Semibold", 13)).grid(
            row=0, column=0, padx=(14, 8), pady=12)
        self.profile_menu = ctk.CTkOptionMenu(
            bar, values=self.cfg.profile_names(), width=220,
            command=self.on_profile_change)
        self.profile_menu.grid(row=0, column=1, pady=12)

        self.header_status = ctk.CTkLabel(bar, text="", text_color=("gray40", "gray65"))
        self.header_status.grid(row=0, column=2, sticky="e", padx=10)

        ctk.CTkButton(bar, text="Configuracion", width=130,
                      command=self.open_config).grid(row=0, column=3, padx=(0, 8), pady=12)
        self.update_button = ctk.CTkButton(
            bar, text="Buscar actualizacion", width=170, fg_color="transparent",
            border_width=1, command=lambda: self.check_updates(silent=False))
        self.update_button.grid(row=0, column=4, padx=(0, 14), pady=12)

    def _build_paths(self) -> None:
        frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        frame.grid(row=1, column=0, sticky="ew", padx=14, pady=(12, 0))
        frame.grid_columnconfigure(1, weight=1)

        self.source_var = tk.StringVar()
        self.dest_var = tk.StringVar()
        for row, (label, var) in enumerate((("Origen", self.source_var),
                                            ("Destino", self.dest_var))):
            ctk.CTkLabel(frame, text=label, width=70, anchor="w").grid(
                row=row, column=0, sticky="w", pady=4)
            entry = ctk.CTkEntry(frame, textvariable=var, height=34)
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            ctk.CTkButton(frame, text="Examinar", width=100,
                          command=lambda v=var: self.browse(v)).grid(
                row=row, column=2, padx=(8, 0), pady=4)
        for var in (self.source_var, self.dest_var):
            var.trace_add("write", lambda *_: self.save_paths())

    def _build_options(self) -> None:
        frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        frame.grid(row=2, column=0, sticky="ew", padx=14, pady=(10, 0))

        self.copy_all_var = tk.BooleanVar()
        self.verify_var = tk.BooleanVar()
        self.dry_run_var = tk.BooleanVar()
        self.mirror_var = tk.BooleanVar()

        boxes = [
            ("Copiar todo (ignorar comparacion)", self.copy_all_var,
             "Copia todos los archivos que pasen las reglas, aunque no hayan cambiado."),
            ("Verificar contenido", self.verify_var,
             "Cuando el tamano coincide pero la fecha no, compara los bytes antes de copiar."),
            ("Simulacion (no escribe)", self.dry_run_var,
             "Analiza y muestra que se haria, sin tocar el destino."),
            ("Eliminar sobrantes en destino", self.mirror_var,
             "Modo espejo: borra del destino lo que ya no existe en el origen."),
        ]
        for col, (text, var, tip) in enumerate(boxes):
            box = ctk.CTkCheckBox(frame, text=text, variable=var,
                                  command=self.save_options)
            box.grid(row=0, column=col, padx=(0, 22), sticky="w")
            _Tooltip(box, tip)
        self.mirror_var.trace_add("write", lambda *_: self.refresh_table())

    def _build_actions(self) -> None:
        frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        frame.grid(row=3, column=0, sticky="ew", padx=14, pady=(12, 0))
        frame.grid_columnconfigure(3, weight=1)

        self.analyze_button = ctk.CTkButton(frame, text="Analizar", width=130, height=36,
                                            command=self.start_analysis)
        self.analyze_button.grid(row=0, column=0)
        self.copy_button = ctk.CTkButton(frame, text="Copiar seleccionados", width=190,
                                         height=36, state="disabled",
                                         command=self.start_copy)
        self.copy_button.grid(row=0, column=1, padx=8)
        self.cancel_button = ctk.CTkButton(frame, text="Cancelar", width=110, height=36,
                                           state="disabled", fg_color="transparent",
                                           border_width=1, command=self.request_cancel)
        self.cancel_button.grid(row=0, column=2)

        self.filter_buttons = ctk.CTkSegmentedButton(
            frame, values=list(FILTERS), command=lambda _v: self.refresh_table())
        self.filter_buttons.set("Todos")
        self.filter_buttons.grid(row=0, column=3, sticky="e", padx=(12, 8))

        ctk.CTkButton(frame, text="Todos", width=80, fg_color="transparent",
                      border_width=1,
                      command=lambda: self.set_all_selected(True)).grid(row=0, column=4)
        ctk.CTkButton(frame, text="Ninguno", width=80, fg_color="transparent",
                      border_width=1,
                      command=lambda: self.set_all_selected(False)).grid(
            row=0, column=5, padx=(8, 0))

    def _build_update_banner(self) -> None:
        self.banner = ctk.CTkFrame(self, corner_radius=6,
                                   fg_color=("#dbeafe", "#14304d"))
        self.banner.grid_columnconfigure(0, weight=1)
        self.banner_label = ctk.CTkLabel(self.banner, text="", anchor="w", justify="left")
        self.banner_label.grid(row=0, column=0, sticky="ew", padx=14, pady=10)
        self.banner_button = ctk.CTkButton(self.banner, text="Instalar y reiniciar",
                                           width=170, command=self.install_update)
        self.banner_button.grid(row=0, column=1, padx=(0, 8), pady=10)
        ctk.CTkButton(self.banner, text="Ahora no", width=90, fg_color="transparent",
                      border_width=1, command=self.hide_banner).grid(
            row=0, column=2, padx=(0, 14), pady=10)
        self.banner.grid_remove()

    def _build_table(self) -> None:
        container = ctk.CTkFrame(self, corner_radius=6)
        container.grid(row=5, column=0, sticky="nsew", padx=14, pady=(12, 0))
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(0, weight=1)

        apply_treeview_style()
        columns = ("sel", "estado", "ruta", "tam", "forigen", "fdestino")
        self.tree = ttk.Treeview(container, columns=columns, show="headings",
                                 style="UMF.Treeview", selectmode="extended")
        headings = (("sel", "", 40), ("estado", "Estado", 120),
                    ("ruta", "Ruta relativa", 520), ("tam", "Tamano", 100),
                    ("forigen", "Modificado origen", 155),
                    ("fdestino", "Modificado destino", 155))
        for key, text, width in headings:
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="w",
                             stretch=(key == "ruta"),
                             minwidth=40 if key == "sel" else 70)
        self.tree.column("sel", anchor="center")
        self.tree.column("tam", anchor="e")
        configure_row_tags(self.tree)

        scroll = ctk.CTkScrollbar(container, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        scroll.grid(row=0, column=1, sticky="ns", padx=(2, 8), pady=8)

        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<Double-1>", lambda e: self.toggle_selected_rows())
        self.tree.bind("<space>", lambda e: self.toggle_selected_rows())
        self.tree.bind("<Button-3>", self.on_tree_context)

        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="Abrir carpeta en el origen",
                                      command=lambda: self.reveal("source"))
        self.context_menu.add_command(label="Abrir carpeta en el destino",
                                      command=lambda: self.reveal("dest"))
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Copiar ruta relativa",
                                      command=self.copy_relpath)

    def _build_footer(self) -> None:
        frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        frame.grid(row=6, column=0, sticky="ew", padx=14, pady=(8, 14))
        frame.grid_columnconfigure(0, weight=1)

        self.progress = ctk.CTkProgressBar(frame, height=12)
        self.progress.set(0)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.progress_label = ctk.CTkLabel(frame, text="", width=330, anchor="e")
        self.progress_label.grid(row=0, column=1, padx=(12, 0))

        self.status = ctk.CTkLabel(frame, text="Listo.", anchor="w",
                                   text_color=("gray30", "gray70"))
        self.status.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    # ----------------------------------------------------------------- perfiles

    @property
    def profile(self) -> Profile:
        return self.cfg.active()

    def load_profile(self) -> None:
        p = self.profile
        self._loading = True
        self.profile_menu.configure(values=self.cfg.profile_names())
        self.profile_menu.set(p.name)
        self.source_var.set(p.source)
        self.dest_var.set(p.dest)
        self.copy_all_var.set(p.copy_all)
        self.verify_var.set(p.verify_content)
        self.dry_run_var.set(p.dry_run)
        self.mirror_var.set(p.mirror_delete)
        self._loading = False
        rules = len(p.include) + len(p.exclude)
        self.header_status.configure(
            text=f"{rules} regla(s) activas" if rules else "sin reglas: entra todo")
        self.clear_results()

    def on_profile_change(self, name: str) -> None:
        self.cfg.active_profile = name
        self.cfg.save()
        self.load_profile()

    def save_paths(self) -> None:
        if getattr(self, "_loading", False):
            return
        self.profile.source = self.source_var.get()
        self.profile.dest = self.dest_var.get()
        self.cfg.save()

    def save_options(self) -> None:
        if getattr(self, "_loading", False):
            return
        p = self.profile
        p.copy_all = self.copy_all_var.get()
        p.verify_content = self.verify_var.get()
        p.dry_run = self.dry_run_var.get()
        p.mirror_delete = self.mirror_var.get()
        self.cfg.save()

    def browse(self, var: tk.StringVar) -> None:
        chosen = filedialog.askdirectory(initialdir=var.get() or None)
        if chosen:
            var.set(os.path.normpath(chosen))

    def open_config(self) -> None:
        from .config_dialog import ConfigDialog
        dialog = ConfigDialog(self, self.cfg)
        self.wait_window(dialog)
        self.cfg.save()
        self.load_profile()

    # ------------------------------------------------------------------ analisis

    def busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def start_analysis(self) -> None:
        if self.busy():
            return
        src, dst = self.source_var.get().strip(), self.dest_var.get().strip()
        if not src or not dst:
            messagebox.showwarning(APP_TITLE, "Configura la ruta de origen y la de destino.")
            return
        if not os.path.isdir(src):
            messagebox.showerror(APP_TITLE, f"La carpeta de origen no existe:\n{src}")
            return
        if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
            messagebox.showerror(APP_TITLE, "El origen y el destino son la misma carpeta.")
            return

        p = self.profile
        snapshot = dict(
            src=src, dst=dst,
            rules=RuleSet(p.include, p.exclude),
            copy_all=self.copy_all_var.get(),
            verify=self.verify_var.get(),
            tolerance=p.mtime_tolerance,
            scan_workers=p.workers_for("scan"),
            workers=p.workers_for("copy"),
        )
        self.clear_results()
        self.set_running(True, "Analizando...")
        self.launch(self._analyze_worker, snapshot)

    def _analyze_worker(self, s: dict) -> None:
        post = self.events.put
        post(("status", "Recorriendo carpetas..."))
        src_scan, dst_scan = scan_both(
            s["src"], s["dst"], s["rules"], self.cancel, s["scan_workers"],
            on_progress=lambda f, d: post(("scan", f, d)))
        if self.cancel.is_set():
            post(("cancelled", "Analisis cancelado."))
            return
        if src_scan.errors:
            post(("warnings", src_scan.errors[:20]))

        post(("status", "Comparando..."))
        result = compare(
            src_scan, dst_scan, s["src"], s["dst"],
            copy_all=s["copy_all"], tolerance=s["tolerance"],
            verify_content=s["verify"], workers=s["workers"], cancel=self.cancel,
            on_progress=lambda done, total: post(("verify", done, total)))
        post(("analysis", result, len(src_scan.entries), src_scan.dir_count))

    # --------------------------------------------------------------------- copia

    def start_copy(self) -> None:
        if self.busy() or not self.comparison:
            return
        pending = [i for i in self.comparison.items
                   if i.selected and i.status in (Status.NEW, Status.MODIFIED)]
        orphans = [i for i in self.comparison.items
                   if i.selected and i.status is Status.ORPHAN] if self.mirror_var.get() else []
        if not pending and not orphans:
            messagebox.showinfo(APP_TITLE, "No hay nada seleccionado para copiar.")
            return

        total = format_size(sum(i.size for i in pending))
        question = f"Se copiaran {len(pending)} archivo(s) ({total})."
        if orphans:
            question += (f"\n\nSe ELIMINARAN {len(orphans)} archivo(s) del destino "
                         "que ya no existen en el origen.\nEsta accion no se puede deshacer.")
        if self.dry_run_var.get():
            question = f"Simulacion: no se escribira nada.\n\n{question}"
        if not messagebox.askokcancel(APP_TITLE, question + "\n\nContinuar?"):
            return

        p = self.profile
        self.stats = CopyStats()
        self.set_running(True, "Copiando...")
        self.launch(self._copy_worker, dict(
            items=list(self.comparison.items),
            src=self.source_var.get().strip(), dst=self.dest_var.get().strip(),
            workers=p.workers_for("copy"), dry_run=self.dry_run_var.get(),
            mirror=self.mirror_var.get()))

    def _copy_worker(self, s: dict) -> None:
        post = self.events.put
        report = run_copy(
            s["items"], s["src"], s["dst"], workers=s["workers"],
            dry_run=s["dry_run"], mirror_delete=s["mirror"],
            cancel=self.cancel, stats=self.stats,
            on_item=lambda item, ok, msg: post(("item", item.rel, ok, msg)))
        post(("copy_done", report, s))

    # ------------------------------------------------------------------- hilado

    def launch(self, target, payload) -> None:
        self.cancel.clear()

        def runner():
            try:
                target(payload)
            except Exception as exc:                    # noqa: BLE001
                # La traza completa siempre al archivo; en la interfaz solo se
                # muestra entera en modo depuracion, para no asustar al usuario.
                log.exception("Fallo en el hilo trabajador")
                detail = traceback.format_exc() if self.debug else ""
                self.events.put(("error", f"{type(exc).__name__}: {exc}", detail))

        self.worker = threading.Thread(target=runner, daemon=True,
                                       name="umf-worker")
        self.worker.start()

    def request_cancel(self) -> None:
        self.cancel.set()
        self.status.configure(text="Cancelando...")

    def _pump(self) -> None:
        """Unico punto donde los resultados del hilo tocan la interfaz."""
        try:
            drained = 0
            while drained < 500:
                kind, *payload = self.events.get_nowait()
                drained += 1
                self._handle(kind, payload)
        except queue.Empty:
            pass
        if self.stats and self.busy():
            self._refresh_progress()
        self.after(PUMP_MS, self._pump)

    def _handle(self, kind: str, payload: list) -> None:
        if kind == "status":
            self.status.configure(text=payload[0])
        elif kind == "scan":
            self.status.configure(
                text=f"Recorriendo... {payload[0]:,} archivos en {payload[1]:,} carpetas")
        elif kind == "verify":
            done, total = payload
            self.status.configure(text=f"Verificando contenido {done:,}/{total:,}")
            self.progress.set(done / total if total else 0)
        elif kind == "analysis":
            self._on_analysis(*payload)
        elif kind == "item":
            self._on_item(*payload)
        elif kind == "copy_done":
            self._on_copy_done(*payload)
        elif kind == "cancelled":
            self.set_running(False, payload[0])
        elif kind == "warnings":
            self._show_warnings(payload[0])
        elif kind == "error":
            self.set_running(False, "Error.")
            detail = payload[1] if len(payload) > 1 and payload[1] else ""
            # En depuracion se muestra la traza completa; para el usuario final
            # solo el mensaje, con la pista de donde esta el detalle.
            tail = detail or "El detalle quedo en el registro (app.log)."
            messagebox.showerror(APP_TITLE, payload[0] + os.linesep * 2 + tail)
        elif kind == "update_found":
            self._on_update_found(*payload)
        elif kind == "update_none":
            self.update_button.configure(text="Buscar actualizacion", state="normal")
            if payload[0]:
                messagebox.showinfo(APP_TITLE, f"Ya tienes la version mas reciente (v{__version__}).")
        elif kind == "update_error":
            self.update_button.configure(text="Buscar actualizacion", state="normal")
            if payload[1]:
                messagebox.showerror(APP_TITLE, payload[0])
            else:
                self.header_status.configure(text="No se pudo comprobar actualizaciones")
        elif kind == "update_progress":
            done, total = payload
            self.progress.set(done / total if total else 0)
            self.banner_label.configure(
                text=f"Descargando actualizacion... {format_size(done)} de {format_size(total)}")
        elif kind == "update_installed":
            self._on_update_installed(payload[0])

    # ---------------------------------------------------------------- resultados

    def _on_analysis(self, result: Comparison, scanned: int, dirs: int) -> None:
        self.comparison = result
        pending = result.pending
        self.set_running(False,
                         f"{scanned:,} archivos revisados en {dirs:,} carpetas  ·  "
                         f"{len(pending):,} por copiar  ·  {result.same_count:,} sin cambios  ·  "
                         f"{len(result.orphans):,} sobrantes")
        self.copy_button.configure(state="normal" if result.items else "disabled")
        self.progress.set(0)
        self.refresh_table()

    def refresh_table(self) -> None:
        if self._insert_job is not None:
            self.after_cancel(self._insert_job)
            self._insert_job = None
        self.tree.delete(*self.tree.get_children())
        self.row_of.clear()
        self.item_of.clear()
        if not self.comparison:
            self.progress_label.configure(text="")
            return

        wanted = FILTER_STATUS.get(self.filter_buttons.get())
        rows = [i for i in self.comparison.items
                if (wanted is None or i.status is wanted)
                and (i.status is not Status.ORPHAN or self.mirror_var.get()
                     or self.filter_buttons.get() == "Sobrantes")]
        self._visible = rows
        self._insert_from(rows, 0)
        self._refresh_selection_label()

    def _insert_from(self, rows: list, start: int) -> None:
        """Inserta por tandas: 40.000 filas de golpe congelarian la ventana."""
        limit = max(500, self.cfg.max_rows_display)
        end = min(len(rows), start + CHUNK_ROWS, limit)
        for item in rows[start:end]:
            iid = self.tree.insert("", "end", values=(
                "X" if item.selected else "-",
                item.status.value,
                item.rel.replace("/", os.sep),
                format_size(item.size),
                format_time(item.mtime),
                format_time(item.dest_mtime),
            ), tags=(item.status.value,))
            self.row_of[item.rel] = iid
            self.item_of[iid] = item
        if end < min(len(rows), limit):
            self._insert_job = self.after(1, lambda: self._insert_from(rows, end))
        else:
            self._insert_job = None
            if len(rows) > limit:
                self.status.configure(
                    text=f"{self.status.cget('text')}   (mostrando {limit:,} de "
                         f"{len(rows):,} filas; la copia usa la lista completa)")

    def _refresh_selection_label(self) -> None:
        if not self.comparison:
            return
        chosen = [i for i in self.comparison.items
                  if i.selected and i.status in (Status.NEW, Status.MODIFIED)]
        total = len(self.comparison.pending)
        self.progress_label.configure(
            text=f"{len(chosen):,} de {total:,} seleccionados  ·  "
                 f"{format_size(sum(i.size for i in chosen))}")

    def on_tree_click(self, event) -> None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self._toggle(iid)
            return "break"

    def toggle_selected_rows(self) -> str:
        for iid in self.tree.selection():
            self._toggle(iid)
        return "break"

    def _toggle(self, iid: str) -> None:
        item = self.item_of.get(iid)
        if item is None:
            return
        item.selected = not item.selected
        self.tree.set(iid, "sel", "X" if item.selected else "-")
        self._refresh_selection_label()

    def set_all_selected(self, value: bool) -> None:
        if not self.comparison:
            return
        for item in getattr(self, "_visible", self.comparison.items):
            item.selected = value
        for iid, item in self.item_of.items():
            self.tree.set(iid, "sel", "X" if item.selected else "-")
        self._refresh_selection_label()

    def clear_results(self) -> None:
        self.comparison = None
        self._visible = []
        self.tree.delete(*self.tree.get_children())
        self.row_of.clear()
        self.item_of.clear()
        self.copy_button.configure(state="disabled")
        self.progress.set(0)
        self.progress_label.configure(text="")

    # ------------------------------------------------------------ copia en curso

    def _on_item(self, rel: str, ok: bool, message: str) -> None:
        iid = self.row_of.get(rel)
        if iid is None:
            return                              # fila fuera del limite mostrado
        self.tree.set(iid, "sel", "ok" if ok else "!")
        if not ok and message != "cancelado":
            self.tree.set(iid, "estado", f"ERROR: {message[:60]}")
            self.tree.item(iid, tags=("ERROR",))
        elif ok:
            self.tree.item(iid, tags=("OK",))

    def _refresh_progress(self) -> None:
        s = self.stats
        if not s or not s.files_total:
            return
        self.progress.set(min(1.0, s.bytes_done / s.bytes_total) if s.bytes_total
                          else s.files_done / s.files_total)
        self.progress_label.configure(
            text=f"{s.files_done:,}/{s.files_total:,}  ·  "
                 f"{format_size(s.rate)}/s  ·  faltan {format_duration(s.eta)}")

    def _on_copy_done(self, report, snapshot: dict) -> None:
        self.stats = None
        verb = "Simulacion" if snapshot["dry_run"] else "Copia"
        if report.cancelled:
            summary = (f"{verb} cancelada. {report.copied:,} archivo(s) copiados "
                       f"({format_size(report.bytes_copied)}).")
        else:
            summary = (f"{verb} terminada: {report.copied:,} archivo(s), "
                       f"{format_size(report.bytes_copied)}")
            if report.deleted:
                summary += f", {report.deleted:,} eliminado(s)"
            if report.errors:
                summary += f", {len(report.errors):,} con error"
            summary += "."
        self.set_running(False, summary)
        self.progress.set(1.0 if not report.cancelled else self.progress.get())

        if not snapshot["dry_run"]:
            self._write_log(report, snapshot, summary)
        if report.errors:
            self._show_errors(report.errors)

    def _write_log(self, report, snapshot: dict, summary: str) -> None:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = log_dir() / f"sync-{stamp}.log"
        lines = [
            f"{APP_TITLE} v{__version__}",
            f"Fecha:   {datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
            f"Perfil:  {self.profile.name}",
            f"Origen:  {snapshot['src']}",
            f"Destino: {snapshot['dst']}",
            f"Resumen: {summary}",
            "",
        ]
        if report.errors:
            lines.append(f"Errores ({len(report.errors)}):")
            lines += [f"  {rel}  ->  {msg}" for rel, msg in report.errors]
        try:
            path.write_text("\n".join(lines), encoding="utf-8")
        except OSError:
            pass

    def _show_errors(self, errors: list[tuple[str, str]]) -> None:
        head = "\n".join(f"- {rel}: {msg}" for rel, msg in errors[:15])
        extra = f"\n\n... y {len(errors) - 15} mas. Revisa el registro." if len(errors) > 15 else ""
        messagebox.showwarning(
            APP_TITLE,
            f"{len(errors)} archivo(s) no se pudieron copiar:\n\n{head}{extra}")

    def _show_warnings(self, warnings: list[tuple[str, str]]) -> None:
        head = "\n".join(f"- {path}: {msg}" for path, msg in warnings[:10])
        messagebox.showwarning(
            APP_TITLE,
            f"No se pudieron leer algunas carpetas del origen:\n\n{head}")

    def set_running(self, running: bool, message: str) -> None:
        state = "disabled" if running else "normal"
        self.analyze_button.configure(state=state)
        self.copy_button.configure(
            state="disabled" if running or not self.comparison else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")
        self.status.configure(text=message)

    # ------------------------------------------------------------------ contexto

    def on_tree_context(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.context_menu.tk_popup(event.x_root, event.y_root)

    def _current_item(self):
        selection = self.tree.selection()
        return self.item_of.get(selection[0]) if selection else None

    def reveal(self, side: str) -> None:
        item = self._current_item()
        if item is None:
            return
        root = self.source_var.get() if side == "source" else self.dest_var.get()
        target = os.path.join(root, item.rel.replace("/", os.sep))
        try:
            if os.path.exists(target):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(target)])
            elif os.path.isdir(os.path.dirname(target)):
                os.startfile(os.path.dirname(target))       # noqa: S606
            else:
                messagebox.showinfo(APP_TITLE, "Esa ruta todavia no existe en ese lado.")
        except OSError as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def copy_relpath(self) -> None:
        item = self._current_item()
        if item is None:
            return
        self.clipboard_clear()
        self.clipboard_append(item.rel.replace("/", os.sep))

    # ---------------------------------------------------------- actualizaciones

    def check_updates(self, silent: bool) -> None:
        url = self.cfg.update_url.strip()
        if not url:
            if not silent:
                messagebox.showinfo(
                    APP_TITLE,
                    "Configura la URL de actualizacion en Configuracion > Avanzado.\n\n"
                    "Ejemplo: https://github.com/tu-organizacion/tu-repo")
            return
        self.update_button.configure(text="Comprobando...", state="disabled")

        def work():
            try:
                info = check_for_update(url)
            except UpdateError as exc:
                self.events.put(("update_error", str(exc), not silent))
                return
            if info is None:
                self.events.put(("update_none", not silent))
            else:
                self.events.put(("update_found", info))

        threading.Thread(target=work, daemon=True, name="umf-update-check").start()

    def _on_update_found(self, info: ReleaseInfo) -> None:
        self.pending_release = info
        self.update_button.configure(text=f"Actualizar a v{info.version.lstrip('vV')}",
                                     state="normal")
        notes = (info.notes or "").strip().splitlines()
        preview = " ".join(notes[:2])[:180] if notes else "Sin notas de version."
        warning = "" if info.verified else "  (sin SHA-256 publicado)"
        self.banner_label.configure(
            text=f"Version {info.version.lstrip('vV')} disponible  ·  tienes v{__version__}"
                 f"{warning}\n{preview}")
        self.banner.grid(row=4, column=0, sticky="ew", padx=14, pady=(12, 0))

    def hide_banner(self) -> None:
        self.banner.grid_remove()

    def install_update(self) -> None:
        info = self.pending_release
        if info is None or self.busy():
            return
        if not info.verified and not messagebox.askokcancel(
                APP_TITLE,
                "Este release no publica un SHA-256, asi que no se puede verificar "
                "la integridad del paquete.\n\nInstalar de todas formas?"):
            return
        self.banner_button.configure(state="disabled")
        self.cancel.clear()

        def work():
            try:
                installed = install(
                    info,
                    on_progress=lambda d, t: self.events.put(("update_progress", d, t)),
                    cancel=self.cancel)
            except UpdateError as exc:
                self.events.put(("update_error", str(exc), True))
                return
            self.events.put(("update_installed", installed))

        threading.Thread(target=work, daemon=True, name="umf-update-install").start()

    def _on_update_installed(self, version: str) -> None:
        self.cfg.save()
        if messagebox.askokcancel(
                APP_TITLE,
                f"La version {version} quedo instalada.\n\n"
                "Se reiniciara la aplicacion para usarla. Continuar?"):
            try:
                restart_into(version)
            except UpdateError as exc:
                messagebox.showerror(APP_TITLE, str(exc))
        else:
            self.banner_label.configure(
                text=f"Version {version} instalada. Se usara al reiniciar la aplicacion.")
            self.banner_button.configure(text="Reiniciar ahora", state="normal",
                                         command=lambda: restart_into(version))

    # -------------------------------------------------------------------- cierre

    def on_close(self) -> None:
        if self.busy():
            if not messagebox.askokcancel(APP_TITLE, "Hay una operacion en curso. Cancelar y salir?"):
                return
            self.cancel.set()
        try:
            self.cfg.window_geometry = self.geometry()
            self.cfg.save()
        except Exception:                                   # noqa: BLE001
            pass
        self.destroy()


class _Tooltip:
    """Globo de ayuda minimo: CustomTkinter no trae uno."""

    def __init__(self, widget, text: str, delay: int = 550):
        self.widget, self.text, self.delay = widget, text, delay
        self.window: tk.Toplevel | None = None
        self.job: str | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self.job = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None

    def _show(self):
        if self.window or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(f"+{x}+{y}")
        frame = ctk.CTkFrame(self.window, corner_radius=4)
        frame.pack()
        ctk.CTkLabel(frame, text=self.text, justify="left",
                     wraplength=380).pack(padx=10, pady=6)

    def _hide(self, _event=None):
        self._cancel()
        if self.window:
            self.window.destroy()
            self.window = None
