"""Reglas de colocacion de la ventana principal.

Se prueba `startup_size`, que es la decision entera sin nada de Tk detras:
crear una ventana de verdad necesitaria un escritorio, y estas pruebas
tambien corren en el runner de la publicacion.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ui.main_window import (DEFAULT_SIZE, MIN_SIZE,        # noqa: E402
                                startup_size)

# Una pantalla de 1920x1080 con la barra de tareas abajo.
AREA = (1920, 1031)


def size(saved: str, scale: float = 1.0, area=AREA) -> tuple[int, int]:
    return startup_size(saved, scale, *area)


class TestStartupSize(unittest.TestCase):
    def test_without_anything_saved_it_uses_the_default(self):
        self.assertEqual(size(""), DEFAULT_SIZE)

    def test_a_saved_size_is_honoured(self):
        self.assertEqual(size("1300x800"), (1300, 800))

    def test_a_size_saved_with_a_position_is_discarded(self):
        """Las versiones anteriores guardaban la geometria completa, y en ese
        formato entraba tambien el tamano de una ventana maximizada: era lo
        que hacia que el programa abriera siempre a pantalla casi completa.
        No se puede distinguir una de otra, asi que se descartan todas."""
        self.assertEqual(size("1920x1009+18+-13"), DEFAULT_SIZE)
        self.assertEqual(size("1300x800+100+50"), DEFAULT_SIZE)

    def test_garbage_falls_back_to_the_default(self):
        for saved in ("basura", "1120", "x740", "-100x-200", "1120x"):
            self.assertEqual(size(saved), DEFAULT_SIZE, saved)

    def test_a_size_that_no_longer_fits_is_discarded(self):
        """El monitor de la ultima vez puede no ser el de ahora."""
        self.assertEqual(size("3000x1400"), DEFAULT_SIZE)
        self.assertEqual(size("1300x800", area=(1024, 768)), DEFAULT_SIZE)

    def test_a_size_below_the_minimum_grows_to_the_minimum(self):
        self.assertEqual(size("300x200"), MIN_SIZE)

    def test_the_dpi_factor_counts_against_the_screen(self):
        """Al 150%, 1300 de ancho ocupan 1950 pixeles reales y ya no caben en
        una pantalla de 1920, aunque el numero guardado parezca menor."""
        self.assertEqual(size("1300x800", scale=1.0), (1300, 800))
        self.assertEqual(size("1300x800", scale=1.5), DEFAULT_SIZE)

    def test_the_default_fits_a_small_screen_check(self):
        """El de fabrica se devuelve tal cual aunque la pantalla sea menor:
        Tk lo ajusta al mapear, y no hay un tamano mejor que ofrecer."""
        self.assertEqual(size("", area=(800, 600)), DEFAULT_SIZE)


if __name__ == "__main__":
    unittest.main()
