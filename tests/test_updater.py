"""Pruebas del actualizador contra un servidor HTTP local.

Cubren el camino completo: consultar el manifiesto, descargar, verificar el
SHA-256, extraer a versions/<ver> y mover el puntero. Tambien se comprueba la
logica de seleccion del lanzador .cmd, que es lo que decide que version
arranca el usuario.
"""

import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import updater                                        # noqa: E402
from app.updater import (ReleaseInfo, UpdateError, activate, check_for_update,  # noqa: E402
                         download, fetch_latest, stage)
from app.version import is_newer                               # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class Server:
    """Servidor HTTP efimero que sirve una carpeta temporal."""

    def __init__(self, directory: Path):
        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):       # sin ruido en la salida de pruebas
                pass

        handler = lambda *a, **kw: Quiet(*a, directory=str(directory), **kw)  # noqa: E731
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def make_package(path: Path, version: str) -> str:
    """Crea un zip que aparenta ser la aplicacion empaquetada."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("UpdateMyFolder.exe", f"binario falso {version}")
        archive.writestr("_internal/base_library.zip", "x")
    return hashlib.sha256(path.read_bytes()).hexdigest()


class UpdaterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="umf-upd-"))
        self.published = self.tmp / "publicado"
        self.published.mkdir()
        self.install = self.tmp / "instalacion"
        (self.install / "versions" / "1.0.0").mkdir(parents=True)
        (self.install / "versions" / "1.0.0" / "UpdateMyFolder.exe").write_text("v1")
        (self.install / "versions" / "current.txt").write_text("1.0.0")
        self.server = Server(self.published)

        # El actualizador escribe en la instalacion real; se redirige a la
        # carpeta temporal y se finge estar empaquetado.
        self._orig_root = updater.install_root
        self._orig_frozen = updater.is_frozen
        self._orig_version = updater.__version__
        updater.install_root = lambda: self.install
        updater.is_frozen = lambda: True
        self.run_as("1.0.0")

    def tearDown(self):
        updater.install_root = self._orig_root
        updater.is_frozen = self._orig_frozen
        updater.__version__ = self._orig_version
        self.server.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_as(self, version: str) -> None:
        """Finge que el proceso corre desde esa version.

        La limpieza nunca borra la carpeta en uso, asi que sin fijar el numero
        estas pruebas dependerian del __version__ real y cambiarian de
        resultado en cada publicacion.
        """
        updater.__version__ = version

    def publish(self, version: str, sha_ok: bool = True) -> str:
        package = self.published / f"UpdateMyFolder-{version}.zip"
        digest = make_package(package, version)
        (self.published / "manifest.json").write_text(json.dumps({
            "version": version,
            "url": package.name,                    # relativa al manifiesto
            "sha256": digest if sha_ok else "0" * 64,
            "size": package.stat().st_size,
            "notes": f"Notas de la version {version}",
        }), encoding="utf-8")
        return f"{self.server.base}/manifest.json"


class TestManifest(UpdaterCase):
    def test_reads_manifest_and_resolves_relative_url(self):
        url = self.publish("1.1.0")
        info = fetch_latest(url)
        self.assertEqual(info.version, "1.1.0")
        self.assertTrue(info.url.endswith("UpdateMyFolder-1.1.0.zip"))
        self.assertTrue(info.url.startswith("http://"))
        self.assertTrue(info.verified)

    def test_newer_version_is_reported(self):
        url = self.publish("2.0.0")
        self.assertIsNotNone(check_for_update(url, current="1.0.0"))

    def test_same_or_older_version_is_not_reported(self):
        url = self.publish("1.0.0")
        self.assertIsNone(check_for_update(url, current="1.0.0"))
        self.assertIsNone(check_for_update(url, current="1.4.0"))

    def test_numeric_not_lexicographic_ordering(self):
        self.assertTrue(is_newer("1.10.0", "1.9.0"))
        self.assertFalse(is_newer("1.9.0", "1.10.0"))

    def test_missing_manifest_raises_readable_error(self):
        with self.assertRaises(UpdateError):
            fetch_latest(f"{self.server.base}/no-existe.json")

    def test_empty_url_raises(self):
        with self.assertRaises(UpdateError):
            fetch_latest("   ")


class TestDownloadAndStage(UpdaterCase):
    def test_full_cycle_installs_and_moves_pointer(self):
        url = self.publish("1.1.0")
        info = check_for_update(url, current="1.0.0")
        self.assertIsNotNone(info)

        seen = []
        zip_path = download(info, self.tmp / "descarga.zip",
                            on_progress=lambda d, t: seen.append(d))
        stage(zip_path, info.version)
        activate(info.version)

        installed = self.install / "versions" / "1.1.0" / "UpdateMyFolder.exe"
        self.assertTrue(installed.exists())
        self.assertIn("1.1.0", installed.read_text())
        self.assertEqual((self.install / "versions" / "current.txt").read_text(), "1.1.0")
        self.assertTrue(seen, "no se reporto progreso de descarga")

    def test_previous_version_survives_for_rollback(self):
        url = self.publish("1.1.0")
        info = fetch_latest(url)
        stage(download(info, self.tmp / "d.zip"), info.version)
        activate(info.version)
        self.assertTrue((self.install / "versions" / "1.0.0").exists())

    def test_bad_sha256_is_rejected_and_file_removed(self):
        url = self.publish("1.1.0", sha_ok=False)
        info = fetch_latest(url)
        target = self.tmp / "malo.zip"
        with self.assertRaises(UpdateError) as ctx:
            download(info, target)
        self.assertIn("SHA-256", str(ctx.exception))
        self.assertFalse(target.exists())

    def test_zip_with_single_root_folder_is_unwrapped(self):
        package = self.published / "envuelto.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("UpdateMyFolder/UpdateMyFolder.exe", "binario")
            archive.writestr("UpdateMyFolder/_internal/x", "y")
        stage(package, "1.2.0")
        self.assertTrue((self.install / "versions" / "1.2.0" / "UpdateMyFolder.exe").exists())

    def test_zip_escaping_the_target_is_refused(self):
        package = self.published / "malicioso.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("../../fuera.exe", "no deberia salir")
        with self.assertRaises(UpdateError):
            stage(package, "9.9.9")
        self.assertFalse((self.install.parent / "fuera.exe").exists())

    def test_package_without_application_is_refused(self):
        package = self.published / "vacio.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("leeme.txt", "nada util")
        with self.assertRaises(UpdateError):
            stage(package, "3.0.0")

    def test_install_refused_when_not_frozen(self):
        updater.is_frozen = lambda: False
        url = self.publish("1.1.0")
        with self.assertRaises(UpdateError) as ctx:
            updater.install(fetch_latest(url))
        self.assertIn("empaquetada", str(ctx.exception))

    def test_prune_discards_old_versions(self):
        for version in ("1.1.0", "1.2.0", "1.3.0", "1.4.0"):
            (self.install / "versions" / version).mkdir()
        activate("1.4.0")
        left = sorted(d.name for d in (self.install / "versions").iterdir() if d.is_dir())
        self.assertIn("1.4.0", left)            # la recien activada
        self.assertIn("1.3.0", left)            # se conserva para revertir
        self.assertNotIn("1.1.0", left)         # sobra: se borra

    def test_prune_never_deletes_the_running_version(self):
        """La carpeta desde la que corre el proceso esta bloqueada por Windows,
        y borrarla dejaria al usuario sin nada a lo que revertir."""
        for version in ("1.1.0", "1.2.0", "1.3.0", "1.4.0"):
            (self.install / "versions" / version).mkdir()
        self.run_as("1.1.0")                    # sobraria por antiguedad
        activate("1.4.0")
        self.assertTrue((self.install / "versions" / "1.1.0").exists())


@unittest.skipUnless(os.name == "nt", "el lanzador es un .cmd de Windows")
class TestLauncher(unittest.TestCase):
    """El lanzador decide que version arranca; conviene probar esa eleccion."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="umf-launch-"))
        source = (ROOT / "build" / "launcher.cmd").read_text(encoding="utf-8")
        # Se sustituye el arranque real por un echo para poder leer la eleccion.
        probe = source.replace('start "" "%VERS%\\!PICK!\\UpdateMyFolder.exe" %*',
                               'echo ELEGIDO=!PICK!')
        self.script = self.tmp / "probe.cmd"
        self.script.write_text(probe, encoding="utf-8")
        self.versions = self.tmp / "versions"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, *versions: str, pointer: str | None = None,
             pointer_bytes: bytes | None = None):
        for version in versions:
            (self.versions / version).mkdir(parents=True)
            (self.versions / version / "UpdateMyFolder.exe").write_text("x")
        if pointer_bytes is not None:
            (self.versions / "current.txt").write_bytes(pointer_bytes)
        elif pointer is not None:
            (self.versions / "current.txt").write_text(pointer, encoding="utf-8")

    def run_probe(self) -> str:
        # stdin cerrado: cuando no hay ninguna version el lanzador hace 'pause'
        # para que el usuario alcance a leer el mensaje, y sin esto la prueba
        # se quedaria esperando una tecla para siempre.
        out = subprocess.run(["cmd", "/c", str(self.script)], cwd=self.tmp,
                             capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=30)
        for line in out.stdout.splitlines():
            if line.startswith("ELEGIDO="):
                return line.split("=", 1)[1].strip()
        return out.stdout.strip()

    def test_follows_the_pointer(self):
        self.make("1.0.0", "1.1.0", pointer="1.1.0")
        self.assertEqual(self.run_probe(), "1.1.0")

    def test_rollback_by_editing_the_pointer(self):
        self.make("1.0.0", "1.1.0", pointer="1.0.0")
        self.assertEqual(self.run_probe(), "1.0.0")

    def test_pointer_saved_with_utf8_bom_still_works(self):
        """El Bloc de notas guarda UTF-8 con BOM. Sin sanear la lectura, esos
        bytes invisibles hacian que el puntero no coincidiera con ninguna
        carpeta y se arrancara la version equivocada sin aviso."""
        self.make("1.0.0", "1.1.0", pointer_bytes=b"\xef\xbb\xbf1.0.0")
        self.assertEqual(self.run_probe(), "1.0.0")

    def test_pointer_with_trailing_newline_and_spaces_works(self):
        self.make("1.0.0", "1.1.0", pointer_bytes=b"  1.0.0  \r\n")
        self.assertEqual(self.run_probe(), "1.0.0")

    def test_pointer_with_quotes_works(self):
        self.make("1.0.0", "1.1.0", pointer_bytes=b'"1.0.0"')
        self.assertEqual(self.run_probe(), "1.0.0")

    def test_falls_back_when_pointer_is_broken(self):
        self.make("1.0.0", pointer="9.9.9")
        self.assertEqual(self.run_probe(), "1.0.0")

    def test_falls_back_when_pointer_is_missing(self):
        self.make("1.0.0")
        self.assertEqual(self.run_probe(), "1.0.0")

    def test_reports_when_nothing_is_installed(self):
        self.versions.mkdir(parents=True)
        self.assertIn("No se encontro", self.run_probe())


if __name__ == "__main__":
    unittest.main(verbosity=2)
