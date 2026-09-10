"""Pruebas del grupo de hilos abandonable.

Todas giran alrededor del mismo escenario: un hilo que entro en una operacion
que no vuelve, como una escritura contra un recurso de red que dejo de
responder. Es lo que dejaba el programa sin respuesta y sin poder cerrarse.
"""

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pool import Pool                                      # noqa: E402


class TestPool(unittest.TestCase):
    def setUp(self):
        self.release = threading.Event()

    def tearDown(self):
        self.release.set()      # que ningun hilo atascado sobreviva a la prueba

    def stuck(self, _item):
        self.release.wait(30)
        return "tarde"

    # ------------------------------------------------------------ uso normal

    def test_applies_the_function_to_every_item(self):
        with Pool(4, "test") as pool:
            got = dict(pool.map_unordered(lambda n: n * n, range(20)))
        self.assertEqual(got, {n: n * n for n in range(20)})

    def test_exceptions_reach_the_consumer(self):
        def boom(n):
            if n == 3:
                raise ValueError("fallo en el 3")
            return n

        with self.assertRaises(ValueError):
            with Pool(2, "test") as pool:
                list(pool.map_unordered(boom, range(10)))

    def test_closing_twice_is_harmless(self):
        pool = Pool(2, "test")
        list(pool.map_unordered(lambda n: n, range(5)))
        pool.close()
        pool.close()

    # ------------------------------------------------- el hilo que no vuelve

    def test_threads_are_daemons(self):
        """El motivo de no usar ThreadPoolExecutor: sus hilos no son demonios
        y ademas registra un gancho de salida que les hace join() sin limite,
        asi que un hilo atascado impedia que el proceso terminara."""
        pool = Pool(3, "test")
        try:
            self.assertTrue(all(t.daemon for t in pool._threads))
        finally:
            pool.close(grace=0.1)

    def test_close_gives_up_on_a_stuck_thread(self):
        pool = Pool(2, "test")
        consumer = threading.Thread(
            target=lambda: list(pool.map_unordered(self.stuck, [1, 2])), daemon=True)
        consumer.start()
        time.sleep(0.3)                 # que los dos hilos entren en la tarea

        started = time.monotonic()
        pool.close(grace=0.5)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 3.0, "close() se quedo esperando al hilo atascado")
        self.assertTrue(any(t.is_alive() for t in pool._threads),
                        "la prueba no llego a atascar ningun hilo")

    def test_cancel_frees_the_consumer_when_every_thread_is_stuck(self):
        """Con todos los hilos atascados no llega ni un resultado. Sin el
        evento de cancelacion, quien consume esperaria para siempre."""
        cancel = threading.Event()
        pool = Pool(2, "test", cancel)
        threading.Timer(0.3, cancel.set).start()

        started = time.monotonic()
        got = list(pool.map_unordered(self.stuck, [1, 2, 3, 4]))
        elapsed = time.monotonic() - started

        self.assertEqual(got, [])
        self.assertLess(elapsed, 3.0)
        pool.close(grace=0.1)

    def test_abandoned_pool_never_starts_the_queued_tasks(self):
        started: list[int] = []
        lock = threading.Lock()

        def track(n):
            with lock:
                started.append(n)
            time.sleep(0.05)
            return n

        pool = Pool(1, "test")
        consumer = threading.Thread(
            target=lambda: list(pool.map_unordered(track, range(200))), daemon=True)
        consumer.start()
        time.sleep(0.2)
        pool.abandon()
        pool.close(grace=1.0)

        with lock:
            count = len(started)
        self.assertLess(count, 200, "se ejecuto toda la cola pese a abandonarla")


if __name__ == "__main__":
    unittest.main()
