"""Motor de copia multihilo con escritura atomica.

Cada archivo se escribe primero en un temporal junto al destino y solo al
terminar se mueve con `os.replace`, que en NTFS es atomico. Si se cancela la
operacion o se cae la red, el destino conserva la version anterior completa:
nunca queda un archivo a medio escribir.
"""

from __future__ import annotations

import os
import shutil
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .comparer import Item, Status
from .logging_setup import get as get_logger
from .scanner import to_os_path

log = get_logger("copier")

CHUNK = 1 << 20             # 1 MiB
TMP_SUFFIX = ".umf-tmp"
RETRIES = 3


@dataclass
class CopyStats:
    """Contadores compartidos entre los hilos de copia y la interfaz.

    La interfaz los lee en su propio temporizador en vez de recibir un evento
    por bloque; asi el progreso es fluido sin inundar la cola de mensajes.
    """
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    deleted: int = 0
    failed: int = 0
    started: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_bytes(self, n: int) -> None:
        with self._lock:
            self.bytes_done += n

    def finish_file(self, ok: bool) -> None:
        with self._lock:
            self.files_done += 1
            if not ok:
                self.failed += 1

    def add_deleted(self) -> None:
        with self._lock:
            self.deleted += 1

    @property
    def elapsed(self) -> float:
        return max(1e-6, time.monotonic() - self.started)

    @property
    def rate(self) -> float:
        return self.bytes_done / self.elapsed

    @property
    def eta(self) -> float:
        remaining = max(0, self.bytes_total - self.bytes_done)
        rate = self.rate
        return remaining / rate if rate > 1 else 0.0


@dataclass
class CopyReport:
    copied: int = 0
    deleted: int = 0
    bytes_copied: int = 0
    cancelled: bool = False
    errors: list[tuple[str, str]] = field(default_factory=list)


class _DirCache:
    """Evita repetir makedirs para la misma carpeta desde varios hilos."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def ensure(self, path: str) -> None:
        with self._lock:
            if path in self._seen:
                return
        os.makedirs(path, exist_ok=True)
        with self._lock:
            self._seen.add(path)


def _clear_readonly(path: str) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass


def _copy_one(item: Item, src_root: str, dst_root: str, dirs: _DirCache,
              stats: CopyStats, cancel: threading.Event) -> tuple[bool, str]:
    src = to_os_path(src_root, item.rel)
    dst = to_os_path(dst_root, item.rel)
    tmp = dst + TMP_SUFFIX
    last_error = ""

    for attempt in range(RETRIES):
        if cancel.is_set():
            return False, "cancelado"
        written = 0
        try:
            dirs.ensure(os.path.dirname(dst))
            with open(src, "rb", buffering=0) as fin, open(tmp, "wb", buffering=0) as fout:
                while True:
                    if cancel.is_set():
                        raise InterruptedError
                    block = fin.read(CHUNK)
                    if not block:
                        break
                    fout.write(block)
                    written += len(block)
                    stats.add_bytes(len(block))
            shutil.copystat(src, tmp)       # conservar la fecha: la proxima
                                            # sincronizacion depende de ella
            if os.path.exists(dst):
                _clear_readonly(dst)
            os.replace(tmp, dst)
            return True, ""
        except InterruptedError:
            stats.add_bytes(-written)
            _discard(tmp)
            return False, "cancelado"
        except OSError as exc:
            last_error = str(exc)
            stats.add_bytes(-written)       # no contar dos veces al reintentar
            _discard(tmp)
            if attempt < RETRIES - 1 and not cancel.is_set():
                # Espera creciente: los fallos de SMB suelen ser transitorios.
                time.sleep(0.5 * (2 ** attempt))
                continue
            return False, last_error
    return False, last_error


def _discard(path: str) -> None:
    try:
        if os.path.exists(path):
            _clear_readonly(path)
            os.remove(path)
    except OSError:
        pass


def run_copy(
    items: list[Item],
    src_root: str,
    dst_root: str,
    *,
    workers: int = 16,
    dry_run: bool = False,
    mirror_delete: bool = False,
    cancel: threading.Event | None = None,
    stats: CopyStats | None = None,
    on_item=None,
) -> CopyReport:
    """Copia los elementos seleccionados y, opcionalmente, borra los sobrantes.

    `on_item(item, ok, mensaje)` se llama una vez por archivo terminado.
    """
    cancel = cancel or threading.Event()
    stats = stats or CopyStats()
    report = CopyReport()

    to_copy = [i for i in items
               if i.selected and i.status in (Status.NEW, Status.MODIFIED)]
    to_delete = [i for i in items if i.selected and i.status is Status.ORPHAN] \
        if mirror_delete else []

    log.info("copia: %d archivo(s), %s, %d hilos, dry_run=%s, espejo=%s (%d a eliminar)",
             len(to_copy), f"{sum(i.size for i in to_copy) / 1e6:.1f} MB",
             workers, dry_run, mirror_delete, len(to_delete))
    stats.files_total = len(to_copy) + len(to_delete)
    stats.bytes_total = sum(i.size for i in to_copy)
    stats.started = time.monotonic()

    if dry_run:
        for item in to_copy:
            stats.add_bytes(item.size)
            stats.finish_file(True)
            if on_item:
                on_item(item, True, "simulado")
        report.copied = len(to_copy)
        report.bytes_copied = stats.bytes_total
        report.deleted = len(to_delete)
        return report

    dirs = _DirCache()
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="copy") as pool:
        futures = {pool.submit(_copy_one, i, src_root, dst_root, dirs, stats, cancel): i
                   for i in to_copy}
        # as_completed reporta en cuanto cada archivo termina, no en el orden
        # en que se enviaron: el progreso avanza de forma pareja.
        for future in as_completed(futures):
            item = futures[future]
            ok, message = future.result()
            stats.finish_file(ok)
            if ok:
                report.copied += 1
                report.bytes_copied += item.size
            elif message == "cancelado":
                report.cancelled = True
            else:
                report.errors.append((item.rel, message))
                log.warning("fallo al copiar %s: %s", item.rel, message)
            if on_item:
                on_item(item, ok, message)

    if cancel.is_set():
        report.cancelled = True
        return report

    for item in to_delete:
        path = to_os_path(dst_root, item.rel)
        try:
            _clear_readonly(path)
            os.remove(path)
            report.deleted += 1
            stats.add_deleted()
        except OSError as exc:
            report.errors.append((item.rel, f"no se pudo eliminar: {exc}"))
        stats.finish_file(True)
        if on_item:
            on_item(item, True, "eliminado")

    if to_delete:
        prune_empty_dirs(dst_root)

    log.info("copia terminada: %d copiados, %d eliminados, %d errores, "
             "%.1f MB en %.1f s", report.copied, report.deleted,
             len(report.errors), report.bytes_copied / 1e6, stats.elapsed)
    return report


def prune_empty_dirs(root: str) -> int:
    """Elimina las carpetas que quedaron vacias tras un borrado en espejo."""
    removed = 0
    for current, dirnames, filenames in os.walk(root, topdown=False):
        if current == root:
            continue
        if not dirnames and not filenames:
            try:
                os.rmdir(current)
                removed += 1
            except OSError:
                pass
    return removed
