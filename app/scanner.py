"""Recorrido paralelo de arboles de directorios.

Dos decisiones sostienen el rendimiento de todo el programa:

1. `os.scandir` en lugar de `os.walk` + `os.stat`. En Windows el `stat` de un
   DirEntry viene precargado desde FindFirstFile, asi que tamano y fecha
   salen sin una sola llamada al sistema adicional. En arboles grandes esa
   sola diferencia vale segundos frente a decenas de segundos.

2. Recorrido por niveles sobre un ThreadPoolExecutor. El trabajo es de E/S y
   libera el GIL en cada llamada al sistema, asi que los hilos si escalan,
   sobre todo contra rutas de red donde manda la latencia.

Las carpetas excluidas se podan antes de descender: nunca se listan.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .logging_setup import get as get_logger
from .rules import RuleSet

log = get_logger("scanner")


@dataclass(slots=True)
class Entry:
    rel: str
    size: int
    mtime: float


@dataclass
class ScanResult:
    entries: dict[str, Entry] = field(default_factory=dict)
    dir_count: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries.values())


def to_os_path(root: str, rel: str) -> str:
    return os.path.join(root, rel.replace("/", os.sep)) if rel else root


def _list_dir(root: str, reldir: str, rules: RuleSet | None, dir_included: bool):
    """Lista un directorio. Devuelve (archivos, subdirectorios, error)."""
    files: list[Entry] = []
    subdirs: list[tuple[str, bool]] = []
    try:
        with os.scandir(to_os_path(root, reldir)) as it:
            for de in it:
                try:
                    # follow_symlinks=False deja fuera enlaces y uniones
                    # (junctions), que en Windows son la via habitual a los
                    # ciclos infinitos de recorrido.
                    if de.is_dir(follow_symlinks=False):
                        rel = f"{reldir}/{de.name}" if reldir else de.name
                        if rules is not None:
                            if rules.dir_excluded(rel):
                                continue
                            subdirs.append((rel, dir_included or rules.dir_included(rel)))
                        else:
                            subdirs.append((rel, True))
                    elif de.is_file(follow_symlinks=False):
                        rel = f"{reldir}/{de.name}" if reldir else de.name
                        if rules is not None:
                            if rules.file_excluded(rel, de.name):
                                continue
                            if rules.has_includes and not dir_included:
                                if not rules.file_include_match(rel, de.name):
                                    continue
                        st = de.stat()      # gratis en Windows: ya venia en el listado
                        files.append(Entry(rel, st.st_size, st.st_mtime))
                except OSError:
                    continue                # una entrada ilegible no aborta el listado
    except OSError as exc:
        return files, subdirs, (reldir or ".", str(exc))
    return files, subdirs, None


def scan_tree(
    root: str,
    rules: RuleSet | None = None,
    cancel: threading.Event | None = None,
    workers: int = 16,
    on_progress=None,
    missing_ok: bool = False,
) -> ScanResult:
    """Recorre `root` aplicando `rules` y devuelve el mapa rel -> Entry.

    `on_progress(archivos, carpetas)` se llama cada nivel para alimentar la
    interfaz sin inundarla de eventos. Con `missing_ok` una raiz inexistente
    devuelve un resultado vacio en vez de un error: es lo normal para el
    destino en la primera sincronizacion.
    """
    started = time.monotonic()
    result = ScanResult()
    if not os.path.isdir(root):
        if not missing_ok:
            result.errors.append((root, "La ruta no existe o no es una carpeta"))
        return result

    cancel = cancel or threading.Event()
    level: list[tuple[str, bool]] = [("", not (rules and rules.has_includes))]

    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="scan") as pool:
        while level and not cancel.is_set():
            next_level: list[tuple[str, bool]] = []
            for files, subdirs, error in pool.map(
                lambda item: _list_dir(root, item[0], rules, item[1]), level
            ):
                if error:
                    result.errors.append(error)
                for entry in files:
                    result.entries[entry.rel] = entry
                next_level.extend(subdirs)
            result.dir_count += len(level)
            level = next_level
            if on_progress:
                on_progress(len(result.entries), result.dir_count)

    log.debug("recorrido %s: %d archivos, %d carpetas, %d hilos, %.0f ms",
              root, len(result.entries), result.dir_count, workers,
              (time.monotonic() - started) * 1000)
    if result.errors:
        log.warning("recorrido %s: %d carpeta(s) ilegibles", root, len(result.errors))
    return result


def scan_both(
    src_root: str,
    dst_root: str,
    rules: RuleSet | None,
    cancel: threading.Event,
    workers: int,
    on_progress=None,
) -> tuple[ScanResult, ScanResult]:
    """Recorre origen y destino a la vez.

    Solaparlos importa mucho cuando uno de los dos esta en red: el tiempo
    total pasa a ser el del lado mas lento en vez de la suma de ambos.
    """
    counts = {"src": (0, 0), "dst": (0, 0)}
    lock = threading.Lock()

    def report(which):
        def _cb(files, dirs):
            if on_progress is None:
                return
            with lock:
                counts[which] = (files, dirs)
                total_files = counts["src"][0] + counts["dst"][0]
                total_dirs = counts["src"][1] + counts["dst"][1]
            on_progress(total_files, total_dirs)
        return _cb

    half = max(2, workers // 2)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="scan-root") as pool:
        f_src = pool.submit(scan_tree, src_root, rules, cancel, half, report("src"), False)
        f_dst = pool.submit(scan_tree, dst_root, rules, cancel, half, report("dst"), True)
        return f_src.result(), f_dst.result()
