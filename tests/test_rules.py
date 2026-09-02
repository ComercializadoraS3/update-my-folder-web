"""Pruebas del motor de reglas. Ejecutar: python -m unittest discover tests"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rules import RuleSet, compile_rule  # noqa: E402


class TestPatternForms(unittest.TestCase):
    def test_extension_matches_at_any_depth(self):
        rs = RuleSet(include=["*.cs"])
        self.assertTrue(rs.accepts_file("Program.cs"))
        self.assertTrue(rs.accepts_file("src/deep/nested/Program.cs"))
        self.assertFalse(rs.accepts_file("src/readme.md"))

    def test_full_name_matches_only_that_file(self):
        rs = RuleSet(include=["ejemplo.cs"])
        self.assertTrue(rs.accepts_file("ejemplo.cs"))
        self.assertTrue(rs.accepts_file("src/ejemplo.cs"))
        self.assertFalse(rs.accepts_file("src/otro.cs"))

    def test_anchored_folder_only_at_root(self):
        rs = RuleSet(include=["/reorgs/"])
        self.assertTrue(rs.accepts_file("reorgs/a.txt"))
        self.assertTrue(rs.accepts_file("reorgs/sub/b.txt"))
        self.assertFalse(rs.accepts_file("src/reorgs/c.txt"))

    def test_unanchored_folder_at_any_depth(self):
        rs = RuleSet(include=["reorgs/"])
        self.assertTrue(rs.accepts_file("reorgs/a.txt"))
        self.assertTrue(rs.accepts_file("src/deep/reorgs/a.txt"))
        self.assertFalse(rs.accepts_file("src/otro/a.txt"))

    def test_anchored_path_with_wildcard(self):
        rs = RuleSet(include=["/src/app/*.cs"])
        self.assertTrue(rs.accepts_file("src/app/Main.cs"))
        self.assertFalse(rs.accepts_file("src/app/deep/Main.cs"))
        self.assertFalse(rs.accepts_file("other/src/app/Main.cs"))

    def test_globstar_folder(self):
        rs = RuleSet(exclude=["**/obj/"])
        self.assertTrue(rs.dir_excluded("obj"))
        self.assertTrue(rs.dir_excluded("src/app/obj"))

    def test_windows_separators_are_normalised(self):
        rs = RuleSet(include=[chr(92).join(["", "src", "app", ""])])
        self.assertTrue(rs.accepts_file("src/app/x.txt"))


class TestCombination(unittest.TestCase):
    def test_empty_include_accepts_everything(self):
        rs = RuleSet()
        self.assertTrue(rs.accepts_file("cualquier/cosa.bin"))

    def test_exclude_beats_include(self):
        rs = RuleSet(include=["*.cs"], exclude=["Generated.cs"])
        self.assertTrue(rs.accepts_file("src/Main.cs"))
        self.assertFalse(rs.accepts_file("src/Generated.cs"))

    def test_excluded_folder_hides_included_extension(self):
        rs = RuleSet(include=["*.cs"], exclude=["bin/"])
        self.assertTrue(rs.accepts_file("src/Main.cs"))
        self.assertFalse(rs.accepts_file("src/bin/Main.cs"))

    def test_folder_include_pulls_in_all_contents(self):
        rs = RuleSet(include=["/reorgs/"])
        self.assertTrue(rs.accepts_file("reorgs/imagen.png"))
        self.assertFalse(rs.accepts_file("otros/imagen.png"))

    def test_case_insensitive(self):
        rs = RuleSet(include=["*.CS"])
        self.assertTrue(rs.accepts_file("src/main.cs"))


class TestDegenerate(unittest.TestCase):
    def test_blank_and_comment_lines_ignored(self):
        self.assertIsNone(compile_rule("   "))
        self.assertIsNone(compile_rule("# comentario"))
        self.assertIsNone(compile_rule("/"))

    def test_only_comments_means_no_includes(self):
        rs = RuleSet(include=["# nada"])
        self.assertFalse(rs.has_includes)
        self.assertTrue(rs.accepts_file("x.txt"))

    def test_leading_slash_file(self):
        rs = RuleSet(include=["/README.md"])
        self.assertTrue(rs.accepts_file("README.md"))
        self.assertFalse(rs.accepts_file("docs/README.md"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
