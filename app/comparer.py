"""Clasificacion de diferencias entre el arbol de origen y el de destino."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum

from .logging_setup import get as get_logger
from .scanner import Entry, ScanResult, to_os_path

log = get_logger("comparer")

CHUNK = 1 << 20     # 1 MiB


class Status(str, Enum):
    NEW = "NUEVO"
    MODIFIED = "MODIFICADO"
    SAME = "IGUAL"
    ORPHAN = "SOBRANTE"


@dataclass(slots=True)
class Item:
    rel: str
    status: Status
    size: int = 0
    mtime: float = 0.0
    dest_size: int | None = None
    dest_mtime: float | None = None
    selected: bool = True

    @property
    def dest_is_newer(self) -> bool:
        return self.dest_mtime is not None and self.dest_mtime > self.mtime


@dataclass
class Comparison:
    items: list[Item]
    same_count: int = 0
    checked: int = 0

    @property
    def pending(self) -> list[Item]:
        return [i for i in self.items if i.status in (Status.NEW, Status.MODIFIED)]

    @property
    def orphans(self) -> list[Item]:
        return [i for i in self.items if i.status is Status.ORPHAN]

    @property
    def selected_bytes(self) -> int:
        return sum(i.size for i in self.items
                   if i.selected and i.status in (Status.NEW, Status.MODIFIED))


def content_equal(path_a: str, path_b: str, chunk: int = CHUNK) -> bool:
    """Compara dos archivos byte a byte, abandonando en la primera diferencia.

    Se prefiere a un hash porque un hash obliga a leer ambos archivos
    completos; esto suele terminar en el primer bloque cuando difieren.
    """
    try:
        with open(path_a, "rb", buffering=0) as fa, open(path_b, "rb", buffering=0) as fb:
            while True:
                ba = fa.read(chunk)
                bb = fb.read(chunk)
                if ba != bb:
                    return False
                if not ba:
                    return True
    except OSError:
        return False        # ante la duda, se considera distinto y se copia


def compare(
    src: ScanResult,
    dst: ScanResult,
    src_root: str,
    dst_root: str,
    *,
    copy_all: bool = False,
    tolerance: float = 2.0,
    verify_content: bool = False,
    workers: int = 16,
    cancel: threading.Event | None = None,
    on_progress=None,
) -> Comparison:
    """Clasifica cada archivo del origen frente al destino.

    Criterio por defecto (rapido, sin leer contenido): distinto tamano, o
    fechas separadas por mas de `tolerance` segundos. La tolerancia existe
    porque FAT32 redondea a 2 s y algunos protocolos de red desplazan la
    marca de tiempo un poco.
    """
    cancel = cancel or threading.Event()
    items: list[Item] = []
    same = 0
    to_verify: list[tuple[Item, Entry, Entry]] = []

    for rel, s in src.entries.items():
        if cancel.is_set():
            break
        d = dst.entries.get(rel)
        if d is None:
            items.append(Item(rel, Status.NEW, s.size, s.mtime))
            continue
        if copy_all:
            items.append(Item(rel, Status.MODIFIED, s.size, s.mtime, d.size, d.mtime))
            continue
        if s.size != d.size:
            items.append(Item(rel, Status.MODIFIED, s.size, s.mtime, d.size, d.mtime))
            continue
        if abs(s.mtime - d.mtime) <= tolerance:
            same += 1
            continue
        item = Item(rel, Status.MODIFIED, s.size, s.mtime, d.size, d.mtime)
        if verify_content:
            to_verify.append((item, s, d))      # mismo tamano, fecha distinta
        else:
            items.append(item)

    # Verificacion de contenido en paralelo: solo para los candidatos dudosos,
    # nunca para el arbol completo.
    if to_verify and not cancel.is_set():
        done = 0
        total = len(to_verify)

        def check(bundle):
            item, _s, _d = bundle
            if cancel.is_set():
                return item, False
            equal = content_equal(to_os_path(src_root, item.rel),
                                  to_os_path(dst_root, item.rel))
            return item, equal

        with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="verify") as pool:
            for item, equal in pool.map(check, to_verify):
                done += 1
                if equal:
                    same += 1
                else:
                    items.append(item)
                if on_progress and done % 25 == 0:
                    on_progress(done, total)
        if on_progress:
            on_progress(total, total)

    for rel, d in dst.entries.items():
        if rel not in src.entries:
            items.append(Item(rel, Status.ORPHAN, d.size, d.mtime, d.size, d.mtime,
                              selected=False))

    items.sort(key=lambda i: (i.status is Status.ORPHAN, i.rel.lower()))
    result = Comparison(items, same_count=same, checked=len(src.entries))
    log.debug("comparacion: %d nuevos, %d modificados, %d iguales, %d sobrantes "
              "(copy_all=%s verify=%s tol=%.1fs)",
              sum(1 for i in items if i.status is Status.NEW),
              sum(1 for i in items if i.status is Status.MODIFIED),
              same, len(result.orphans), copy_all, verify_content, tolerance)
    return result


def format_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def format_time(ts: float | None) -> str:
    if not ts:
        return ""
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"
