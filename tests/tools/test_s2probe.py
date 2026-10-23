# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Скрипт проб S2 (tools/probe/s2probe.py): ограничение стендом ADR-0007, вердикты и формат отчётов."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/probe"))
import s2probe  # noqa: E402


def run(*argv):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return s2probe.main(list(argv))


class S2ProbeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, name):
        return json.loads((self.out / f"{name}.json").read_text())

    def test_link_refuses_addresses_outside_lab_stand(self):
        for peer in ("8.8.8.8", "10.0.0.2", "192.168.1.2", "127.0.0.1"):
            self.assertEqual(run("link", "--out", str(self.out), "--peer", peer), 2, peer)
        self.assertFalse((self.out / "link.json").exists())

    def test_boot_verdict_against_15_s(self):
        mark = self.out / "mark.json"
        for seconds, verdict in ((14.9, "PASS"), (15.1, "FAIL")):
            mark.write_text(json.dumps({"clock": "boottime", "seconds": seconds}))
            run("boot", "--out", str(self.out), "--mark", str(mark))
            self.assertEqual(self.report("boot")["verdict"], verdict)
        mark.write_text(json.dumps({"clock": "monotonic", "seconds": 3.0}))     # не от старта ядра — не доказательство
        self.assertEqual(run("boot", "--out", str(self.out), "--mark", str(mark)), 1)
        self.assertEqual(self.report("boot")["verdict"], "FAIL")

    def test_env_report_is_informational(self):
        self.assertEqual(run("env", "--out", str(self.out)), 0)
        r = self.report("env")
        self.assertEqual(r["verdict"], "INFO")
        for key in ("commit", "machine", "gst_elements", "codec_choice", "kernel"):
            self.assertIn(key, r)

    def test_cpu_percent(self):
        self.assertEqual(s2probe.cpu_percent((100, 1000), (400, 2000)), 30.0)
        self.assertIsNone(s2probe.cpu_percent(None, (1, 2)))


if __name__ == "__main__":
    unittest.main()
