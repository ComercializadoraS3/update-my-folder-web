"""Empaqueta y publica una version nueva.

Flujo completo de una entrega, en un comando:

    python build/publish.py --bump 1.1.0 --release

La version puede darse exacta (1.1.0) o como salto (major, minor, patch), o
deducirse del mensaje del commit con --bump auto, que es lo que usa el flujo
de GitHub Actions en cada push a main.

  1. escribe la version nueva en app/version.py
  2. compila con PyInstaller (onedir)
  3. arma dos zip:
       UpdateMyFolder-<ver>.zip              paquete de actualizacion
       UpdateMyFolder-instalador-<ver>.zip   instalacion completa la primera vez
  4. calcula el SHA-256 y escribe manifest.json
  5. con --release, crea el release en GitHub y sube los archivos (usa 'gh')

Los clientes ya instalados solo necesitan el primer zip: el actualizador lo
descarga, verifica el hash y lo extrae en versions/<ver>.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
APP_NAME = "UpdateMyFolder"


def current_version() -> str:
    text = (ROOT / "app" / "version.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("No se pudo leer __version__ de app/version.py")
    return match.group(1)


def bump_level_from_message(message: str) -> str:
    """Deduce cuanto subir la version a partir del mensaje del commit.

    Convenio (Conventional Commits): 'BREAKING CHANGE' o un '!' antes de los
    dos puntos suben la mayor, 'feat:' sube la menor y cualquier otra cosa
    sube el parche. Asi el salto lo decide quien escribe el commit, sin un
    paso manual aparte.
    """
    first = next(iter(message.strip().splitlines()), "")
    if "BREAKING CHANGE" in message or re.match(r"^\w+(\([^)]*\))?!:", first):
        return "major"
    if re.match(r"^feat(\([^)]*\))?:", first, re.IGNORECASE):
        return "minor"
    return "patch"


def next_version(current: str, level: str) -> str:
    numbers = [int(n) for n in re.findall(r"\d+", current)[:3]]
    numbers += [0] * (3 - len(numbers))
    major, minor, patch = numbers
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def resolve_bump(request: str, message: str = "") -> str:
    """Traduce 'auto', 'patch', 'minor', 'major' o '1.4.0' a la version final."""
    request = request.strip()
    if request == "auto":
        request = bump_level_from_message(
            message or os.environ.get("COMMIT_MESSAGE", ""))
    if request in ("major", "minor", "patch"):
        return next_version(current_version(), request)
    if not re.fullmatch(r"\d+\.\d+\.\d+", request):
        raise SystemExit(
            f"Version no valida: {request} "
            "(se espera X.Y.Z, o major | minor | patch | auto)")
    return request


def bump(new_version: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", new_version):
        raise SystemExit(f"Version no valida: {new_version} (se espera X.Y.Z)")
    path = ROOT / "app" / "version.py"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        re.sub(r'__version__\s*=\s*"[^"]+"', f'__version__ = "{new_version}"', text),
        encoding="utf-8")
    print(f"  version -> {new_version}")


def build() -> Path:
    # Se invoca como modulo del interprete actual, no por el comando suelto:
    # asi no depende de que Scripts/ este en el PATH y se empaqueta con el
    # mismo Python con el que se desarrolla.
    try:
        import PyInstaller                                     # noqa: F401
    except ImportError:
        raise SystemExit(
            "Falta PyInstaller. Instala con: pip install -r requirements-dev.txt") from None
    for stale in (DIST / APP_NAME, ROOT / "build" / APP_NAME):
        shutil.rmtree(stale, ignore_errors=True)
    subprocess.run(
        [sys.executable, "-m", "PyInstaller",
         str(ROOT / "build" / f"{APP_NAME}.spec"), "--noconfirm",
         "--distpath", str(DIST), "--workpath", str(ROOT / "build" / "_work")],
        cwd=ROOT, check=True)
    out = DIST / APP_NAME
    if not (out / f"{APP_NAME}.exe").exists():
        raise SystemExit(f"La compilacion no genero {out / (APP_NAME + '.exe')}")
    return out


def zip_tree(source: Path, target: Path, prefix: str = "") -> Path:
    target.unlink(missing_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, prefix + str(path.relative_to(source)).replace("\\", "/"))
    return target


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def make_installer(app_dir: Path, version: str) -> Path:
    """Zip con la estructura completa lista para descomprimir la primera vez."""
    staging = DIST / f"_instalador-{version}"
    shutil.rmtree(staging, ignore_errors=True)
    versions = staging / "versions" / version
    versions.parent.mkdir(parents=True)
    shutil.copytree(app_dir, versions)
    (staging / "versions" / "current.txt").write_text(version, encoding="utf-8")
    shutil.copy2(ROOT / "build" / "launcher.cmd", staging / f"{APP_NAME}.cmd")
    shutil.copy2(ROOT / "build" / "launcher-silencioso.vbs",
                 staging / f"{APP_NAME}-silencioso.vbs")
    (staging / "LEEME.txt").write_text(
        "Update My Folder\n"
        "================\n\n"
        "1. Descomprime esta carpeta donde quieras (por ejemplo C:/Apps/UpdateMyFolder).\n"
        f"2. Crea un acceso directo a {APP_NAME}.cmd y usalo siempre para abrir.\n"
        f"   (Si molesta el parpadeo de la consola, apunta el acceso directo a\n"
        f"    {APP_NAME}-silencioso.vbs en su lugar.)\n\n"
        "No abras el .exe de versions/ directamente: el lanzador es lo que\n"
        "permite que la aplicacion se actualice sola sin romper el acceso directo.\n",
        encoding="utf-8")

    # El nombre lleva 'instalador' antes del numero, no despues, y eso importa:
    # la API de GitHub devuelve los adjuntos ordenados por nombre, y las
    # versiones instaladas hasta la 1.2.1 se quedan con el primer .zip del
    # release. Con '-instalador' al final ese primero era el instalador, cuyo
    # hash no coincide con el publicado, y esas instalaciones no podian
    # actualizarse. Asi el paquete de actualizacion ordena primero.
    target = DIST / f"{APP_NAME}-instalador-{version}.zip"
    zip_tree(staging, target)
    shutil.rmtree(staging, ignore_errors=True)
    return target


def write_manifest(version: str, package: Path, digest: str, repo: str | None) -> Path:
    url = (f"https://github.com/{repo}/releases/download/v{version}/{package.name}"
           if repo else package.name)
    manifest = DIST / "manifest.json"
    manifest.write_text(json.dumps({
        "version": version,
        "url": url,
        "sha256": digest,
        "size": package.stat().st_size,
        "notes": f"Version {version}",
    }, indent=2), encoding="utf-8")
    return manifest


def release(version: str, files: list[Path], digest: str, notes: str) -> None:
    if shutil.which("gh") is None:
        raise SystemExit("Falta la CLI de GitHub ('gh'). Sube los archivos a mano "
                         "o instala https://cli.github.com")
    body = f"{notes}\n\nsha256: {digest}\n"
    subprocess.run(
        ["gh", "release", "create", f"v{version}", *[str(f) for f in files],
         "--title", f"v{version}", "--notes", body],
        cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Empaqueta y publica Update My Folder")
    parser.add_argument("--bump", metavar="X.Y.Z|major|minor|patch|auto",
                        help="version nueva antes de compilar; 'auto' la deduce "
                             "del mensaje del commit (--commit-message o $COMMIT_MESSAGE)")
    parser.add_argument("--commit-message", default="",
                        help="mensaje del que deducir el nivel cuando --bump auto")
    parser.add_argument("--skip-build", action="store_true",
                        help="reutiliza dist/UpdateMyFolder ya compilado")
    parser.add_argument("--release", action="store_true",
                        help="crea el release en GitHub con 'gh' y sube los archivos")
    parser.add_argument("--repo", help="owner/repo para la URL del manifiesto")
    parser.add_argument("--notes", default="", help="notas de version")
    args = parser.parse_args()

    if args.bump:
        bump(resolve_bump(args.bump, args.commit_message))
    version = current_version()
    print(f"Publicando {APP_NAME} v{version}")

    DIST.mkdir(exist_ok=True)
    app_dir = DIST / APP_NAME if args.skip_build else build()
    if not app_dir.exists():
        raise SystemExit(f"No existe {app_dir}. Compila sin --skip-build.")

    package = zip_tree(app_dir, DIST / f"{APP_NAME}-{version}.zip")
    digest = sha256(package)
    (DIST / f"{package.name}.sha256").write_text(f"{digest}  {package.name}\n",
                                                 encoding="utf-8")
    installer = make_installer(app_dir, version)
    manifest = write_manifest(version, package, digest, args.repo)

    print(f"  paquete     {package.name}  ({package.stat().st_size / 1e6:.1f} MB)")
    print(f"  instalador  {installer.name}")
    print(f"  sha256      {digest}")
    print(f"  manifiesto  {manifest}")

    if args.release:
        release(version, [package, installer, DIST / f"{package.name}.sha256"],
                digest, args.notes or f"Version {version}")
        print("  release publicado en GitHub")
    else:
        print("\nSube al release de GitHub estos archivos:")
        print(f"  {package}")
        print(f"  {DIST / (package.name + '.sha256')}")
        print(f"  {installer}   (solo para instalaciones nuevas)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
