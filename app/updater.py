"""Auto-actualizacion desde GitHub Releases (o un manifiesto JSON propio).

El problema de fondo en Windows es que no se puede sobrescribir un ejecutable
en uso. La solucion aqui es no intentarlo: cada version vive en su propia
carpeta y un puntero indica cual esta activa.

    <raiz>/UpdateMyFolder.cmd        lanzador estable (el acceso directo)
    <raiz>/versions/current.txt      texto plano con la version activa
    <raiz>/versions/1.0.0/...        una carpeta por version instalada
    <raiz>/versions/1.1.0/...

Actualizar es descargar, verificar el SHA-256, extraer a una carpeta nueva y
mover el puntero. La version anterior sigue en disco, asi que revertir es
cambiar una linea de texto. Nunca se toca un archivo bloqueado.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import install_root, is_frozen
from .logging_setup import get as get_logger
from .version import APP_NAME, __version__, is_newer

log = get_logger("updater")

USER_AGENT = f"{APP_NAME}/{__version__}"
TIMEOUT = 15
KEEP_VERSIONS = 3
_SHA_IN_TEXT = re.compile(r"\b([a-fA-F0-9]{64})\b")
_INSTALLER_MARKS = ("instalador", "installer", "setup")


@dataclass
class ReleaseInfo:
    version: str
    url: str
    notes: str = ""
    sha256: str = ""
    size: int = 0

    @property
    def verified(self) -> bool:
        return bool(self.sha256)


class UpdateError(Exception):
    pass


# --------------------------------------------------------------------- consulta

def _get(url: str, accept: str = "application/json") -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def _github_api_url(url: str) -> str | None:
    """Normaliza cualquier forma de URL de GitHub a la API de releases."""
    url = url.strip().rstrip("/")
    if "api.github.com/repos/" in url:
        return url if "/releases" in url else url + "/releases/latest"
    match = re.match(r"https?://(?:www\.)?github\.com/([^/]+)/([^/]+)", url)
    if match:
        owner, repo = match.group(1), match.group(2).removesuffix(".git")
        return f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    return None


def _pick_package(assets: list[dict], version: str) -> dict:
    """Elige el zip de actualizacion entre los adjuntos del release.

    Un release lleva dos zip: el paquete de actualizacion y el instalador
    completo para una maquina nueva. La API de GitHub devuelve los adjuntos
    ordenados por nombre, no por orden de subida, y "-instalador.zip" queda
    antes que ".zip", asi que quedarse con el primer .zip descargaba el
    instalador mientras el hash publicado era el del paquete: la verificacion
    fallaba siempre. Se elige por nombre, no por posicion.
    """
    zips = [a for a in assets if a["name"].lower().endswith(".zip")]
    if not zips:
        raise UpdateError("El release no incluye ningun archivo .zip")
    expected = f"{APP_NAME}-{version.lstrip('vV')}.zip".lower()
    exact = next((a for a in zips if a["name"].lower() == expected), None)
    if exact is not None:
        return exact
    # Un release con otros nombres: se descartan los que se anuncian como
    # instalador antes de tomar el primero que quede.
    plain = [a for a in zips
             if not any(mark in a["name"].lower() for mark in _INSTALLER_MARKS)]
    return (plain or zips)[0]


def _sha_named(text: str, package: str, lone_ok: bool = False) -> str:
    """Busca en `text` el SHA-256 que corresponde a `package`.

    El formato de sha256sum es "<hash>  <archivo>" por linea. Un hash sin
    nombre de archivo solo se acepta con `lone_ok`, y solo si es el unico del
    texto: es el caso de las notas de un release, donde el hash va suelto.
    """
    lines = [line for line in text.splitlines() if _SHA_IN_TEXT.search(line)]
    for line in lines:
        if package.lower() in line.lower():
            return _SHA_IN_TEXT.search(line).group(1).lower()
    if lone_ok and len(lines) == 1 and ".zip" not in lines[0].lower():
        return _SHA_IN_TEXT.search(lines[0]).group(1).lower()
    return ""


def _published_sha(assets: list[dict], package: str, notes: str) -> str:
    """Hash publicado del paquete elegido, o cadena vacia si no hay ninguno.

    Un hash cualquiera del release no sirve: tiene que ser el del archivo que
    se va a descargar. Se busca primero su propio .sha256, luego una lista de
    sumas en la que alguna linea lo nombre, y al final las notas del release.
    """
    def text_of(asset: dict) -> str:
        try:
            return _get(asset["browser_download_url"],
                        "text/plain").decode("utf-8", "replace")
        except (urllib.error.URLError, OSError):
            return ""

    own = next((a for a in assets
                if a["name"].lower() == f"{package}.sha256".lower()), None)
    if own is not None:
        sha = _sha_named(text_of(own), package, lone_ok=True)
        if sha:
            return sha

    for asset in assets:
        if asset["name"].lower().endswith((".sha256", "sha256sums.txt")):
            sha = _sha_named(text_of(asset), package)
            if sha:
                return sha

    return _sha_named(notes, package, lone_ok=True)


def _from_github(api_url: str) -> ReleaseInfo:
    data = json.loads(_get(api_url))
    if isinstance(data, list):                  # .../releases devuelve una lista
        data = next((r for r in data if not r.get("draft")), None)
        if data is None:
            raise UpdateError("El repositorio no tiene releases publicados")

    assets = data.get("assets", [])
    version = str(data.get("tag_name") or data.get("name") or "0.0.0")
    notes = (data.get("body") or "").strip()
    package = _pick_package(assets, version)

    return ReleaseInfo(
        version=version,
        url=package["browser_download_url"],
        notes=notes,
        sha256=_published_sha(assets, package["name"], notes),
        size=int(package.get("size") or 0),
    )


def _from_manifest(url: str) -> ReleaseInfo:
    data = json.loads(_get(url))
    if "version" not in data or "url" not in data:
        raise UpdateError("El manifiesto no tiene los campos 'version' y 'url'")
    download = data["url"]
    if not download.lower().startswith(("http://", "https://")):
        download = url.rsplit("/", 1)[0] + "/" + download.lstrip("/")
    return ReleaseInfo(
        version=str(data["version"]),
        url=download,
        notes=str(data.get("notes", "")),
        sha256=str(data.get("sha256", "")).lower(),
        size=int(data.get("size") or 0),
    )


def fetch_latest(update_url: str) -> ReleaseInfo:
    """Consulta el canal configurado y devuelve la ultima version publicada."""
    if not update_url.strip():
        raise UpdateError("No hay URL de actualizacion configurada")
    api = _github_api_url(update_url)
    log.debug("consultando actualizaciones en %s (api=%s)", update_url, bool(api))
    try:
        return _from_github(api) if api else _from_manifest(update_url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("No se encontro ningun release publicado (404)") from exc
        raise UpdateError(f"Error HTTP {exc.code} al consultar actualizaciones") from exc
    except urllib.error.URLError as exc:
        raise UpdateError(f"Sin conexion con el servidor de actualizaciones: {exc.reason}") from exc
    except (ValueError, KeyError) as exc:
        raise UpdateError(f"Respuesta de actualizacion no valida: {exc}") from exc


def check_for_update(update_url: str, current: str = __version__) -> ReleaseInfo | None:
    """Devuelve la version publicada solo si es mas nueva que la actual."""
    info = fetch_latest(update_url)
    return info if is_newer(info.version, current) else None


# ------------------------------------------------------------------- instalacion

def versions_dir() -> Path:
    return install_root() / "versions"


def current_pointer() -> Path:
    return versions_dir() / "current.txt"


def download(info: ReleaseInfo, dest: Path, on_progress=None,
             cancel: threading.Event | None = None) -> Path:
    """Descarga el zip verificando el SHA-256 mientras escribe."""
    cancel = cancel or threading.Event()
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = info.size
    done = 0

    request = urllib.request.Request(
        info.url, headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            total = total or int(response.headers.get("Content-Length") or 0)
            with open(dest, "wb") as out:
                while True:
                    if cancel.is_set():
                        raise UpdateError("Descarga cancelada")
                    block = response.read(1 << 18)
                    if not block:
                        break
                    out.write(block)
                    digest.update(block)
                    done += len(block)
                    if on_progress:
                        on_progress(done, total)
    except urllib.error.URLError as exc:
        dest.unlink(missing_ok=True)
        raise UpdateError(f"Fallo la descarga: {exc.reason}") from exc

    log.info("descargados %d bytes de %s", done, info.url)
    if info.sha256 and digest.hexdigest() != info.sha256:
        log.error("sha256 no coincide: esperado %s, obtenido %s",
                  info.sha256, digest.hexdigest())
        dest.unlink(missing_ok=True)
        raise UpdateError("El archivo descargado no coincide con el SHA-256 publicado")
    return dest


def stage(zip_path: Path, version: str) -> Path:
    """Extrae el zip a versions/<version>, dejandolo listo para activar."""
    target = versions_dir() / version.lstrip("vV")
    staging = target.with_name(target.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.namelist():
                # Un zip no puede escribir fuera de su carpeta de destino.
                resolved = (staging / member).resolve()
                if not str(resolved).startswith(str(staging.resolve())):
                    raise UpdateError(f"Ruta insegura en el paquete: {member}")
            archive.extractall(staging)
    except (zipfile.BadZipFile, OSError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(f"El paquete descargado no se pudo extraer: {exc}") from exc

    # Muchos zip envuelven todo en una sola carpeta raiz; se desenvuelve.
    contents = list(staging.iterdir())
    if len(contents) == 1 and contents[0].is_dir():
        inner = contents[0]
        for child in inner.iterdir():
            shutil.move(str(child), str(staging / child.name))
        inner.rmdir()

    if not any(staging.glob("*.exe")) and not (staging / "app").exists():
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError("El paquete no contiene la aplicacion esperada")

    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)
    log.info("version %s extraida en %s", version, target)
    return target


def activate(version: str) -> None:
    """Mueve el puntero a la version indicada y limpia las mas antiguas."""
    version = version.lstrip("vV")
    pointer = current_pointer()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    tmp = pointer.with_suffix(".tmp")
    tmp.write_text(version, encoding="utf-8")
    os.replace(tmp, pointer)
    log.info("puntero movido a %s", version)
    _prune(keep=version)


def _prune(keep: str) -> None:
    """Conserva las versiones mas recientes para poder revertir; borra el resto."""
    from .version import parse_version
    try:
        installed = [d for d in versions_dir().iterdir() if d.is_dir()]
    except OSError:
        return
    installed.sort(key=lambda d: parse_version(d.name), reverse=True)
    for old in installed[KEEP_VERSIONS:]:
        if old.name in (keep, __version__):
            continue
        shutil.rmtree(old, ignore_errors=True)


def restart_into(version: str) -> None:
    """Arranca la version recien instalada y cierra la actual.

    Se lanza el ejecutable nuevo directamente en vez del lanzador .cmd para no
    mostrar una ventana de consola al usuario.
    """
    target = versions_dir() / version.lstrip("vV")
    exe = target / f"{APP_NAME}.exe"
    if not exe.exists():
        found = next(iter(target.glob("*.exe")), None)
        if found is None:
            raise UpdateError("No se encontro el ejecutable de la version nueva")
        exe = found
    subprocess.Popen([str(exe)], cwd=str(target), close_fds=True,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    sys.exit(0)


def install(info: ReleaseInfo, on_progress=None,
            cancel: threading.Event | None = None) -> str:
    """Descarga, verifica, extrae y activa. Devuelve la version instalada.

    No reinicia: quien llama decide cuando hacerlo.
    """
    if not is_frozen():
        raise UpdateError(
            "La instalacion automatica solo funciona en la version empaquetada. "
            "En desarrollo, actualiza el codigo fuente manualmente.")
    import tempfile
    with tempfile.TemporaryDirectory(prefix="umf-update-") as tmpdir:
        zip_path = download(info, Path(tmpdir) / "update.zip", on_progress, cancel)
        stage(zip_path, info.version)
    activate(info.version)
    return info.version.lstrip("vV")
