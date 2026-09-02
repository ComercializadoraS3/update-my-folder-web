"""Motor de reglas de inclusion y omision.

Sintaxis soportada (las barras invertidas de Windows se normalizan a '/'):

    *.cs            cualquier archivo con esa extension, a cualquier profundidad
    ejemplo.cs      archivos con ese nombre exacto, a cualquier profundidad
    /reorgs/        la carpeta 'reorgs' en la raiz del origen
    reorgs/         cualquier carpeta llamada 'reorgs', a cualquier profundidad
    /src/app/*.cs   ruta anclada a la raiz con comodin
    **/obj/         equivalente explicito a 'obj/'

Reglas de combinacion:
  * lista de inclusion vacia -> entra todo lo que no este excluido;
  * con inclusiones -> solo entra lo que coincida con alguna;
  * la omision siempre gana sobre la inclusion;
  * una regla de carpeta arrastra todo su contenido;
  * comparacion insensible a mayusculas (comportamiento de Windows).

Cada patron se compila una sola vez a expresion regular. Las reglas de
carpeta excluida se consultan durante el recorrido para podar el subarbol
completo: nunca se entra a lo que se va a descartar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_BACKSLASH = chr(92)


def _translate(pattern: str) -> str:
    """Traduce un patron glob a fuente de expresion regular.

    Difiere de fnmatch.translate en que '*' no cruza separadores de ruta y
    '**/' si lo hace, que es lo que hace utiles los patrones de carpeta.
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        i += 1
        if c == "*":
            if i < n and pattern[i] == "*":
                i += 1
                if i < n and pattern[i] == "/":
                    i += 1
                    out.append("(?:.*/)?")     # '**/' puede ser cero segmentos
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = i
            if j < n and pattern[j] == "!":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape("["))
            else:
                body = pattern[i:j].replace(_BACKSLASH, _BACKSLASH * 2)
                i = j + 1
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body + "]")
        else:
            out.append(re.escape(c))
    return "".join(out)


@dataclass(frozen=True)
class Rule:
    raw: str
    is_dir: bool
    anchored: bool
    name_only: bool
    regex: re.Pattern

    def matches(self, relpath: str, name: str) -> bool:
        return self.regex.match(name if self.name_only else relpath) is not None


def compile_rule(raw: str) -> Rule | None:
    """Compila un patron. Devuelve None para lineas vacias o comentarios."""
    text = raw.strip().replace(_BACKSLASH, "/")
    if not text or text.startswith("#"):
        return None

    is_dir = text.endswith("/")
    if is_dir:
        text = text[:-1]
    anchored = text.startswith("/")
    if anchored:
        text = text.lstrip("/")
    if not text:
        return None

    # Un patron sin separadores se compara contra el nombre suelto, de modo
    # que '*.cs' o 'ejemplo.cs' apliquen a cualquier profundidad.
    name_only = "/" not in text and not anchored
    body = _translate(text)
    if name_only:
        source = "^" + body + "$"
    elif anchored:
        source = "^" + body + "$"
    else:
        source = "^(?:.*/)?" + body + "$"
    return Rule(raw.strip(), is_dir, anchored, name_only, re.compile(source, re.IGNORECASE))


class RuleSet:
    """Conjunto compilado de reglas listo para consultar durante el recorrido."""

    def __init__(self, include: list[str] | None = None, exclude: list[str] | None = None):
        inc = [r for r in (compile_rule(p) for p in (include or [])) if r]
        exc = [r for r in (compile_rule(p) for p in (exclude or [])) if r]
        self.include_files = [r for r in inc if not r.is_dir]
        self.include_dirs = [r for r in inc if r.is_dir]
        self.exclude_files = [r for r in exc if not r.is_dir]
        self.exclude_dirs = [r for r in exc if r.is_dir]
        self.has_includes = bool(inc)
        self.invalid: list[str] = []

    # Se consulta por cada subdirectorio antes de descender.
    def dir_excluded(self, reldir: str) -> bool:
        name = reldir.rpartition("/")[2]
        return any(r.matches(reldir, name) for r in self.exclude_dirs)

    # Marca el subarbol como incluido; se hereda hacia abajo en el recorrido.
    def dir_included(self, reldir: str) -> bool:
        name = reldir.rpartition("/")[2]
        return any(r.matches(reldir, name) for r in self.include_dirs)

    def file_excluded(self, relpath: str, name: str) -> bool:
        return any(r.matches(relpath, name) for r in self.exclude_files)

    def file_include_match(self, relpath: str, name: str) -> bool:
        return any(r.matches(relpath, name) for r in self.include_files)

    def accepts_file(self, relpath: str, inherited_include: bool = False) -> bool:
        """Decision completa para una ruta suelta, sin contexto de recorrido.

        La usa el probador de reglas y las pruebas; el escaner usa las
        consultas granulares de arriba porque puede podar carpetas enteras.
        """
        rel = relpath.replace(_BACKSLASH, "/").lstrip("/")
        name = rel.rpartition("/")[2]
        parts = rel.split("/")[:-1]

        # Cualquier carpeta ancestro excluida descarta el archivo.
        acc = ""
        included_by_dir = inherited_include
        for part in parts:
            acc = f"{acc}/{part}" if acc else part
            if self.dir_excluded(acc):
                return False
            if self.dir_included(acc):
                included_by_dir = True

        if self.file_excluded(rel, name):
            return False
        if not self.has_includes:
            return True
        return included_by_dir or self.file_include_match(rel, name)


def validate(patterns: list[str]) -> list[str]:
    """Devuelve los patrones que no se pudieron compilar."""
    bad: list[str] = []
    for p in patterns:
        try:
            compile_rule(p)
        except re.error:
            bad.append(p)
    return bad
