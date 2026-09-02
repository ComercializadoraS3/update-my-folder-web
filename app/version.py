"""Identidad y version de la aplicacion.

`__version__` es la unica fuente de verdad: la usan el actualizador para
comparar contra el manifiesto publicado, el script de empaquetado para
nombrar el zip, y la barra de titulo.
"""

APP_NAME = "UpdateMyFolder"
APP_TITLE = "Update My Folder"
__version__ = "1.2.0"


def parse_version(text: str) -> tuple[int, ...]:
    """Convierte '1.4.0' o 'v1.4.0-beta' en una tupla comparable.

    Los sufijos no numericos se descartan; lo que importa es el orden
    numerico entre versiones publicadas.
    """
    cleaned = text.strip().lstrip("vV").split("+")[0].split("-")[0]
    parts: list[int] = []
    for chunk in cleaned.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)
