"""Pruebas de integracion del motor: recorrido, comparacion y copia."""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.comparer import Status, compare                       # noqa: E402
from app.config import (DEFAULT_EXCLUDE, DEFAULT_UPDATE_URL, AppConfig,  # noqa: E402
                        Profile, auto_workers, is_network_path)
from app.copier import CopyStats, run_copy                     # noqa: E402
from app.rules import RuleSet                                  # noqa: E402
from app.scanner import scan_both, scan_tree                   # noqa: E402
from app.updater import _github_api_url                        # noqa: E402


def write(root: Path, rel: str, text: str, mtime: float | None = None) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="umf-test-"))
        self.src = self.tmp / "origen"
        self.dst = self.tmp / "destino"
        self.src.mkdir()
        self.dst.mkdir()
        self.cancel = threading.Event()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sync(self, rules=None, **kw):
        s, d = scan_both(str(self.src), str(self.dst), rules, self.cancel, 8)
        return compare(s, d, str(self.src), str(self.dst), workers=8,
                       cancel=self.cancel, **kw)


class TestScanner(EngineCase):
    def test_finds_files_at_all_depths(self):
        write(self.src, "a.txt", "1")
        write(self.src, "sub/b.txt", "2")
        write(self.src, "sub/deep/c.txt", "3")
        result = scan_tree(str(self.src), workers=4)
        self.assertEqual(set(result.entries), {"a.txt", "sub/b.txt", "sub/deep/c.txt"})
        self.assertEqual(result.errors, [])

    def test_excluded_folder_is_pruned(self):
        write(self.src, "keep/a.cs", "x")
        write(self.src, "bin/b.cs", "x")
        write(self.src, "sub/bin/c.cs", "x")
        result = scan_tree(str(self.src), RuleSet(exclude=["bin/"]), workers=4)
        self.assertEqual(set(result.entries), {"keep/a.cs"})

    def test_include_extension_filters_scan(self):
        write(self.src, "a.cs", "x")
        write(self.src, "a.md", "x")
        result = scan_tree(str(self.src), RuleSet(include=["*.cs"]), workers=4)
        self.assertEqual(set(result.entries), {"a.cs"})

    def test_folder_include_pulls_all_contents(self):
        write(self.src, "reorgs/img.png", "x")
        write(self.src, "reorgs/sub/doc.pdf", "x")
        write(self.src, "otro/img.png", "x")
        result = scan_tree(str(self.src), RuleSet(include=["/reorgs/"]), workers=4)
        self.assertEqual(set(result.entries), {"reorgs/img.png", "reorgs/sub/doc.pdf"})

    def test_missing_destination_is_not_an_error(self):
        result = scan_tree(str(self.tmp / "no-existe"), missing_ok=True)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.entries, {})


class TestComparer(EngineCase):
    def test_classifies_new_modified_same_orphan(self):
        past = time.time() - 10_000
        write(self.src, "nuevo.txt", "contenido")
        write(self.src, "igual.txt", "igual", mtime=past)
        write(self.dst, "igual.txt", "igual", mtime=past)
        write(self.src, "mod.txt", "version nueva mas larga")
        write(self.dst, "mod.txt", "vieja", mtime=past)
        write(self.dst, "sobrante.txt", "adios")

        cmp = self.sync()
        by_rel = {i.rel: i.status for i in cmp.items}
        self.assertEqual(by_rel["nuevo.txt"], Status.NEW)
        self.assertEqual(by_rel["mod.txt"], Status.MODIFIED)
        self.assertEqual(by_rel["sobrante.txt"], Status.ORPHAN)
        self.assertNotIn("igual.txt", by_rel)       # los iguales no se listan
        self.assertEqual(cmp.same_count, 1)

    def test_mtime_tolerance_absorbs_small_drift(self):
        base = time.time() - 5_000
        write(self.src, "a.txt", "abc", mtime=base)
        write(self.dst, "a.txt", "abc", mtime=base + 1.5)
        self.assertEqual(len(self.sync(tolerance=2.0).pending), 0)
        self.assertEqual(len(self.sync(tolerance=0.5).pending), 1)

    def test_verify_content_avoids_pointless_copy(self):
        base = time.time() - 5_000
        write(self.src, "a.txt", "identico", mtime=base)
        write(self.dst, "a.txt", "identico", mtime=base + 900)
        self.assertEqual(len(self.sync().pending), 1)                      # solo fechas
        self.assertEqual(len(self.sync(verify_content=True).pending), 0)   # lee bytes

    def test_verify_content_still_detects_real_change(self):
        base = time.time() - 5_000
        write(self.src, "a.txt", "AAAA", mtime=base)
        write(self.dst, "a.txt", "BBBB", mtime=base + 900)
        self.assertEqual(len(self.sync(verify_content=True).pending), 1)

    def test_copy_all_forces_everything(self):
        base = time.time() - 5_000
        write(self.src, "a.txt", "x", mtime=base)
        write(self.dst, "a.txt", "x", mtime=base)
        self.assertEqual(len(self.sync().pending), 0)
        self.assertEqual(len(self.sync(copy_all=True).pending), 1)


class TestCopier(EngineCase):
    def test_copies_new_and_modified_preserving_mtime(self):
        past = time.time() - 20_000
        write(self.src, "sub/nuevo.txt", "hola", mtime=past)
        cmp = self.sync()
        report = run_copy(cmp.items, str(self.src), str(self.dst), workers=4)

        dest = self.dst / "sub" / "nuevo.txt"
        self.assertEqual(report.copied, 1)
        self.assertEqual(report.errors, [])
        self.assertEqual(dest.read_text(encoding="utf-8"), "hola")
        self.assertAlmostEqual(dest.stat().st_mtime, past, delta=2)

    def test_second_run_copies_nothing(self):
        for n in range(20):
            write(self.src, f"dir{n % 4}/f{n}.txt", f"contenido {n}")
        run_copy(self.sync().items, str(self.src), str(self.dst), workers=8)
        self.assertEqual(len(self.sync().pending), 0)

    def test_no_temporary_files_left_behind(self):
        write(self.src, "a.bin", "x" * 5000)
        run_copy(self.sync().items, str(self.src), str(self.dst), workers=2)
        leftovers = [p.name for p in self.dst.rglob("*.umf-tmp")]
        self.assertEqual(leftovers, [])

    def test_dry_run_writes_nothing(self):
        write(self.src, "a.txt", "x")
        run_copy(self.sync().items, str(self.src), str(self.dst),
                 workers=2, dry_run=True)
        self.assertFalse((self.dst / "a.txt").exists())

    def test_overwrites_readonly_destination(self):
        write(self.src, "a.txt", "nuevo contenido largo")
        dest = write(self.dst, "a.txt", "viejo", mtime=time.time() - 9000)
        os.chmod(dest, 0o444)
        report = run_copy(self.sync().items, str(self.src), str(self.dst), workers=2)
        self.assertEqual(report.errors, [])
        self.assertEqual(dest.read_text(encoding="utf-8"), "nuevo contenido largo")

    def test_mirror_delete_removes_orphans_and_empty_dirs(self):
        write(self.src, "keep.txt", "x")
        write(self.dst, "viejo/borrar.txt", "x")
        cmp = self.sync()
        for item in cmp.items:
            item.selected = True
        report = run_copy(cmp.items, str(self.src), str(self.dst),
                          workers=2, mirror_delete=True)
        self.assertEqual(report.deleted, 1)
        self.assertFalse((self.dst / "viejo").exists())
        self.assertTrue((self.dst / "keep.txt").exists())

    def test_orphans_survive_without_mirror(self):
        write(self.src, "keep.txt", "x")
        write(self.dst, "sobrante.txt", "x")
        cmp = self.sync()
        run_copy(cmp.items, str(self.src), str(self.dst), workers=2)
        self.assertTrue((self.dst / "sobrante.txt").exists())

    def test_unselected_items_are_skipped(self):
        write(self.src, "si.txt", "x")
        write(self.src, "no.txt", "x")
        cmp = self.sync()
        for item in cmp.items:
            item.selected = item.rel == "si.txt"
        run_copy(cmp.items, str(self.src), str(self.dst), workers=2)
        self.assertTrue((self.dst / "si.txt").exists())
        self.assertFalse((self.dst / "no.txt").exists())

    def test_stats_track_bytes_and_files(self):
        payload = "y" * 4096
        for n in range(5):
            write(self.src, f"f{n}.txt", payload)
        stats = CopyStats()
        run_copy(self.sync().items, str(self.src), str(self.dst),
                 workers=4, stats=stats)
        self.assertEqual(stats.files_done, 5)
        self.assertEqual(stats.bytes_done, 5 * len(payload))
        self.assertEqual(stats.failed, 0)


class TestWorkerHeuristics(unittest.TestCase):
    """Los hilos por defecto se eligen segun donde vivan las rutas.

    Medido en este proyecto: en disco local el recorrido no gana nada con
    hilos y la copia escala hasta ~2.4x; en red ambos ganan mucho porque el
    coste dominante es la latencia por operacion.
    """

    def test_local_scan_uses_few_threads(self):
        self.assertLessEqual(auto_workers("scan", "C:/local", "D:/otro"), 4)

    def test_local_copy_uses_more_than_scan(self):
        self.assertGreater(auto_workers("copy", "C:/local", "D:/otro"),
                           auto_workers("scan", "C:/local", "D:/otro"))

    def test_network_path_raises_the_count(self):
        unc = chr(92) * 2 + "servidor" + chr(92) + "recurso"
        self.assertGreater(auto_workers("scan", "C:/local", unc),
                           auto_workers("scan", "C:/local", "D:/otro"))
        self.assertGreaterEqual(auto_workers("copy", unc, "D:/otro"), 32)

    def test_unc_path_is_detected_as_network(self):
        self.assertTrue(is_network_path(chr(92) * 2 + "servidor" + chr(92) + "recurso"))
        self.assertFalse(is_network_path("C:/Windows"))
        self.assertFalse(is_network_path(""))

    def test_manual_thread_count_overrides_the_heuristic(self):
        profile = Profile(name="x", source="C:/a", dest="C:/b", threads=7)
        self.assertEqual(profile.workers_for("scan"), 7)
        self.assertEqual(profile.workers_for("copy"), 7)

    def test_zero_threads_means_automatic(self):
        profile = Profile(name="x", source="C:/a", dest="C:/b", threads=0)
        self.assertEqual(profile.workers_for("copy"), auto_workers("copy", "C:/a", "C:/b"))


class TestDefaultExclusions(unittest.TestCase):
    """Un perfil nuevo nace con las omisiones del despliegue ya puestas."""

    def test_new_profile_carries_the_defaults(self):
        self.assertEqual(Profile().exclude, DEFAULT_EXCLUDE)

    def test_each_profile_gets_its_own_list(self):
        first, second = Profile(), Profile()
        first.exclude.append("*.bak")
        self.assertNotIn("*.bak", second.exclude)
        self.assertNotIn("*.bak", DEFAULT_EXCLUDE)

    def test_an_empty_list_saved_by_the_user_is_respected(self):
        # Quien vacia la lista a proposito no debe encontrarsela repuesta
        # en el siguiente arranque.
        self.assertEqual(Profile.from_dict({"name": "x", "exclude": []}).exclude, [])

    def test_the_defaults_decide_over_real_paths(self):
        rules = RuleSet([], DEFAULT_EXCLUDE)
        for omitted in ("Form1.cs", "src/Form1.cs", "salida.tmp", "web.config",
                        "client.log", "bin/app.pdb", "lastcalltree.info"):
            self.assertFalse(rules.accepts_file(omitted), omitted)
        for kept in ("Form1.dll", "datos.xml", "docs/readme.txt", "app.config"):
            self.assertTrue(rules.accepts_file(kept), kept)
        # Las tres carpetas van ancladas a la raiz: se podan ahi y no mas abajo.
        self.assertTrue(rules.dir_excluded("reorgs"))
        self.assertTrue(rules.dir_excluded("PrivateTempStorage"))
        self.assertFalse(rules.dir_excluded("datos/reorgs"))


class TestDefaultUpdateChannel(unittest.TestCase):
    """El canal viene puesto: una instalacion nueva se actualiza sin configurar."""

    def _load(self, raw: dict) -> AppConfig:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            return AppConfig.load(path)

    def test_a_fresh_config_points_at_the_repository(self):
        self.assertEqual(AppConfig().update_url, DEFAULT_UPDATE_URL)

    def test_an_old_config_without_the_key_gets_the_channel(self):
        self.assertEqual(self._load({"profiles": []}).update_url, DEFAULT_UPDATE_URL)

    def test_an_empty_url_falls_back_to_the_channel(self):
        # Las instalaciones anteriores al canal guardan la URL en blanco. Si se
        # respetara ese vacio nunca recibirian una actualizacion, que es justo
        # lo que hay que arreglar. Desactivar la busqueda es cosa de la casilla.
        self.assertEqual(self._load({"update_url": ""}).update_url, DEFAULT_UPDATE_URL)

    def test_a_custom_url_is_never_overwritten(self):
        propia = "https://intranet.local/umf/manifest.json"
        self.assertEqual(self._load({"update_url": propia}).update_url, propia)

    def test_the_checkbox_is_what_disables_the_search(self):
        self.assertFalse(self._load({"auto_check_updates": False}).auto_check_updates)

    def test_the_channel_resolves_to_the_releases_api(self):
        self.assertEqual(
            _github_api_url(DEFAULT_UPDATE_URL),
            "https://api.github.com/repos/ComercializadoraS3/"
            "update-my-folder-web/releases/latest")


if __name__ == "__main__":
    unittest.main(verbosity=2)
