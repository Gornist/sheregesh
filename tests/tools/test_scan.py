# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Тесты базовых сканеров (части TEST-UPD-009, TEST-SYS-003, TEST-REL-011)."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/security"))
import scan  # noqa: E402


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._root = scan.ROOT
        scan.ROOT = Path(self.tmp.name)
        self.addCleanup(setattr, scan, "ROOT", self._root)

    def write(self, rel, text):
        p = scan.ROOT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return rel

    def test_detects_private_key_and_preset_password(self):
        # Фикстуры собираются из частей, чтобы сам тест не срабатывал на сканере.
        files = [self.write("a/key.txt", "-----BEGIN OPENSSH " + "PRIVATE KEY-----\n"),
                 self.write("a/firstboot.conf", "PRESET_ROOT_PASS" + 'WORD="x"\n')]
        kinds = {f.rsplit(": ", 1)[1] for f in scan.scan_secrets(files)}
        self.assertEqual(kinds, {"private-key", "preset-password"})

    def test_canary_allowed_only_in_tests(self):
        canary = "SHRG-CANARY-LOG-abcdef123456\n"
        self.assertEqual(scan.scan_secrets([self.write("tests/data/c.txt", canary)]), [])
        self.assertEqual(len(scan.scan_secrets([self.write("software/x.log", canary)])), 1)

    def test_scope_denylist_in_code_but_not_docs(self):
        files = [self.write("software/sheregesh/link.py", "import crsf\n"),
                 self.write("docs/security/out-of-scope-components.md", "CRSF запрещён\n")]
        found = scan.scan_scope(files)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("software/"))

    def test_clean_tree_passes(self):
        files = [self.write("software/sheregesh/ok.py", "print('синтетические данные')\n")]
        self.assertEqual(scan.scan_secrets(files), [])
        self.assertEqual(scan.scan_scope(files), [])


if __name__ == "__main__":
    unittest.main()
