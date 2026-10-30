# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""В docs/acceptance в git — только отчёты *.md; сырые данные приёмок — в архиве на devbox (docs/acceptance/ARCHIVE.md)."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class AcceptanceMdOnlyTest(unittest.TestCase):
    def test_only_markdown_tracked(self):
        files = subprocess.run(["git", "ls-files", "docs/acceptance"], cwd=ROOT, capture_output=True, text=True,
                               check=True).stdout.split()
        self.assertTrue(files)
        self.assertEqual([f for f in files if not f.endswith(".md")], [])

    def test_gitignore_keeps_raw_out(self):
        r = subprocess.run(["git", "check-ignore", "-q", "docs/acceptance/some-run/summary.json"], cwd=ROOT)
        self.assertEqual(r.returncode, 0)
        r = subprocess.run(["git", "check-ignore", "-q", "docs/acceptance/some-run/REPORT.md"], cwd=ROOT)
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
