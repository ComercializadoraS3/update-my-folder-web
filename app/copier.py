"""Motor de copia multihilo con escritura atomica.

Cada archivo se escribe primero en un temporal junto al destino y solo al
terminar se mueve con `os.replace`, que en NTFS es atomico. Si se cancela la
operacion o se cae la red, el destino conserva la version anterior completa:
nunca queda un archivo a medio escribir.

Los hilos vienen de `app.pool`, no de ThreadPoolExecutor: una escritura contra
un recurso de red que dejo de responder no se puede interrumpir desde Python,
asi que la unica salida es dejar de esperarla. El temporal lleva un
identificador propio de cada corrida para que un hilo abandonado no se cruce
con la copia siguiente.
"""

from __future__ import annotations

import os
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field

from .comparer import Item, Status
from .logging_setup import get as get_logger
from .pool import Pool
from .scanner import to_os_path

log = get_logger("copier")

CHUNK = 1 << 20             # 1 MiB
TMP_PREFIX = ".umf-tmp"
RETRIES = 3


def _tmp_suffix() -> str:
    """Sufijo distinto en cada corrida.

    Un hilo abandonado puede seguir escribiendo su temporal despues de que la
    copia se dio por cancelada. Con un nombre fijo, la corrida siguiente
    escribiria el mismo archivo desde otro hilo y las dos se pisarian. Con uno
    propio por corrida, lo peor que queda es un temporal huerfano, y el
    recorrido ya no los mira.
    """
    return f"{TMP_PREFIX}-{secrets.token_hex(4)}"


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
    # Cuando avanzo por ultima vez. La interfaz lo mira para distinguir "va
    # lento" de "el destino dejo de responder": sin esto la barra se queda
    # quieta y el programa parece colgado.
    last_progress: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_bytes(self, n: int) -> None:
        with self._lock:
            self.bytes_done += n
            if n > 0:               # los negativos son reintentos, no avance
                self.last_progress = time.monotonic()

    def finish_file(self, ok: bool) -> None:
        with self._lock:
            self.files_done += 1
            self.last_progress = time.monotonic()
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
    def stalled_for(self) -> float:
        """Segundos sin avanzar un solo byte."""
        return time.monotonic() - self.last_progress

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


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _SetFileTime = ctypes.WinDLL("kernel32", use_last_error=True).SetFileTime
    _SetFileTime.argtypes = [wintypes.HANDLE,
                             ctypes.POINTER(wintypes.FILETIME),
                             ctypes.POINTER(wintypes.FILETIME),
                             ctypes.POINTER(wintypes.FILETIME)]
    _SetFileTime.restype = wintypes.BOOL
    _EPOCH_DELTA = 116444736000000000   # 1601-01-01 -> 1970-01-01, en unidades de 100 ns

    def _filetime(ns: int) -> "wintypes.FILETIME":
        value = ns // 100 + _EPOCH_DELTA
        return wintypes.FILETIME(value & 0xFFFFFFFF, value >> 32)

    def _set_times(fileno: int, st: os.stat_result) -> None:
        atime, mtime = _filetime(st.st_atime_ns), _filetime(st.st_mtime_ns)
        if not _SetFileTime(msvcrt.get_osfhandle(fileno), None,
                            ctypes.byref(atime), ctypes.byref(mtime)):
            # Se propaga igual que lo hacia shutil.copystat: un destino sin la
            # fecha del origen se volveria a copiar en cada sincronizacion.
            raise ctypes.WinError(ctypes.get_last_error())
else:
    def _set_times(fileno: int, st: os.stat_result) -> None:
        os.utime(fileno, ns=(st.st_atime_ns, st.st_mtime_ns))


def _write_all(fout, block: bytes) -> None:
    """Escribe el bloque entero.

    `buffering=0` entrega un archivo en crudo, y ahi `write` puede escribir
    menos bytes de los pedidos sin que sea un error. Ignorar el valor devuelto
    dejaba archivos truncados en el destino con la fecha correcta: la siguiente
    sincronizacion los daba por buenos y nunca los reparaba.
    """
    view = memoryview(block)
    while view:
        written = fout.write(view)
        if not written:
            raise OSError("la escritura no avanzo")
        view = view[written:]


def _copy_one(item: Item, src_root: str, dst_root: str, dirs: _DirCache,
              stats: CopyStats, cancel: threading.Event,
              suffix: str) -> tuple[bool, str]:
    src = to_os_path(src_root, item.rel)
    dst = to_os_path(dst_root, item.rel)
    tmp = dst + suffix
    last_error = ""

    for attempt in range(RETRIES):
        if cancel.is_set():
            return False, "cancelado"
        written = 0
        try:
            dirs.ensure(os.path.dirname(dst))
            src_stat = os.stat(src)
            with open(src, "rb", buffering=0) as fin, open(tmp, "wb", buffering=0) as fout:
                while True:
                    if cancel.is_set():
                        raise InterruptedError
                    block = fin.read(CHUNK)
                    if not block:
                        break
                    _write_all(fout, block)
                    written += len(block)
                    stats.add_bytes(len(block))
                # Conservar la fecha: la proxima sincronizacion depende de ella.
                # Se fija sobre el descriptor todavia abierto en vez de con
                # shutil.copystat, que reabre el archivo: medido contra un
                # recurso SMB, esa reapertura costaba unos 12 ms por archivo y
                # no mejoraba con mas hilos, mas que la escritura en si.
                _set_times(fout.fileno(), src_stat)
            if cancel.is_set():
                # Ultima parada antes de publicar. Un hilo que se abandono y
                # termino tarde su escritura no debe dejar el archivo puesto
                # cuando la copia ya se reporto como cancelada.
                raise InterruptedError
            try:
                os.replace(tmp, dst)
            except PermissionError:
                # Solo aqui se paga el sondeo del destino: comprobar de
                # antemano si existe era otra ida y vuelta por cada archivo.
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
    suffix = _tmp_suffix()

    def copy_one(item: Item) -> tuple[bool, str]:
        return _copy_one(item, src_root, dst_root, dirs, stats, cancel, suffix)

    # El grupo entrega cada archivo en cuanto termina, no en el orden en que
    # se envio: el progreso avanza de forma pareja. Y al cancelar no se queda
    # esperando a un hilo detenido en el destino: lo abandona.
    with Pool(max(1, workers), "copy", cancel) as pool:
        for item, (ok, message) in pool.map_unordered(copy_one, to_copy):
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
                # Sin esto habria que recorrer las miles de tareas que quedan
                # en cola solo para verlas devolver "cancelado", y cada una
                # manda su aviso a la interfaz: cancelar tardaba mas que copiar.
                pool.abandon()
                break

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
