"""Grupo de hilos demonio que se puede abandonar.

`ThreadPoolExecutor` no sirve aqui por dos motivos, y ambos se notan justo
cuando la red falla, que es cuando mas importa:

1. Sus hilos no son demonios y, ademas, registra un gancho de salida que hace
   `join()` sobre todos ellos sin limite de tiempo. Un solo hilo detenido en
   una escritura SMB deja el proceso vivo para siempre: la ventana se cierra y
   el programa no termina.

2. Al cancelar hay que esperar igualmente a los hilos en vuelo. Una escritura
   sobre un recurso de red que dejo de responder no se puede interrumpir desde
   Python: se sale cuando el protocolo agota su propia espera, que son decenas
   de segundos o nunca. Mientras tanto "Cancelar" no hacia nada.

Aqui los hilos son demonios y se los puede dejar atras. Si no responden en el
plazo dado se sigue adelante sin ellos; el proceso puede terminar y el sistema
cierra sus descriptores al salir.
"""

from __future__ import annotations

import queue
import threading
import time

from .logging_setup import get as get_logger

log = get_logger("pool")

GRACE = 2.0     # segundos que se espera a un hilo antes de abandonarlo


class Pool:
    """Aplica una funcion a varios elementos sobre `workers` hilos demonio."""

    def __init__(self, workers: int, name: str,
                 cancel: threading.Event | None = None) -> None:
        """`cancel` corta la espera de resultados.

        Hace falta pasarlo: si se atascan todos los hilos a la vez no llega
        ni un resultado, y sin este evento quien consume se quedaria esperando
        para siempre justo en el caso que este modulo existe para resolver.
        """
        self.name = name
        self._cancel = cancel
        self._tasks: queue.Queue = queue.Queue()
        self._results: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._threads = [
            threading.Thread(target=self._loop, name=f"{name}_{n}", daemon=True)
            for n in range(max(1, workers))
        ]
        for thread in self._threads:
            thread.start()

    # ------------------------------------------------------------------ hilos

    def _loop(self) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                return
            if self._stop.is_set():
                continue                # grupo abandonado: ya nadie recoge esto
            fn, item = task
            try:
                self._results.put((item, fn(item), None))
            except BaseException as exc:            # noqa: BLE001
                self._results.put((item, None, exc))

    # ------------------------------------------------------------------- uso

    def map_unordered(self, fn, items):
        """Entrega `(elemento, resultado)` en cuanto cada tarea termina.

        Las excepciones se propagan a quien consume, igual que hacia
        `ThreadPoolExecutor.map`. Si el grupo se abandona, la iteracion corta
        ahi mismo en vez de esperar a los hilos que quedaron colgados, asi que
        el consumidor puede recibir menos resultados que elementos envio.
        """
        items = list(items)
        for item in items:
            self._tasks.put((fn, item))
        pending = len(items)
        while pending:
            if self._stop.is_set() or (self._cancel is not None and self._cancel.is_set()):
                return
            try:
                # Con espera acotada en vez de indefinida: es lo que permite
                # notar que el grupo se abandono mientras se esperaba.
                item, value, error = self._results.get(timeout=0.1)
            except queue.Empty:
                continue
            pending -= 1
            if error is not None:
                raise error
            yield item, value

    def abandon(self) -> None:
        """Deja de recoger resultados. Las tareas en cola no llegan a correr."""
        self._stop.set()

    def close(self, grace: float = GRACE) -> None:
        """Cierra el grupo esperando `grace` segundos como mucho."""
        self._stop.set()
        while True:                     # las tareas en cola ya no interesan
            try:
                self._tasks.get_nowait()
            except queue.Empty:
                break
        for _ in self._threads:
            self._tasks.put(None)

        deadline = time.monotonic() + grace
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        stuck = [t.name for t in self._threads if t.is_alive()]
        if stuck:
            # No es un error: es la situacion que este modulo existe para
            # sobrevivir. Se deja constancia porque casi siempre significa que
            # el recurso de red dejo de responder.
            log.warning("%d hilo(s) de '%s' sin responder tras %.1fs; se abandonan (%s)",
                        len(stuck), self.name, grace, ", ".join(stuck[:4]))

    def __enter__(self) -> "Pool":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
