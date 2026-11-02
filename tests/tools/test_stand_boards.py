# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Адрес платы по серийному номеру (tools/stand/boards.py): явный адрес только проверяется, auto — переменная,
кэш, поиск по MAC; чужая плата не принимается; найденный адрес кэшируется. Ни один скрипт не держит адрес по умолчанию."""
import argparse
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from stand import boards  # noqa: E402

PI1, PI2 = boards.BOARDS["pi1"]["serial"], boards.BOARDS["pi2"]["serial"]


class BoardsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "boards.json"
        self.serials = {"10.0.0.1": PI1, "10.0.0.2": PI2}
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for k in ("SHEREGESH_PI1_HOST", "SHEREGESH_PI2_HOST"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_(self, host, cmd):
        return (0, self.serials[host]) if host in self.serials else (255, "")

    def resolve(self, board, host=None, finder=None):
        return boards.resolve(board, host, run=self.run_, finder=finder or (lambda mac, net: None), cache=self.cache)

    def test_explicit_host_is_verified_not_replaced(self):
        self.assertEqual(self.resolve("pi1", "10.0.0.1"), "10.0.0.1")
        with self.assertRaisesRegex(boards.BoardNotFound, "10.0.0.2"):
            self.resolve("pi1", "10.0.0.2")                # это pi2 — молча искать другую нельзя

    def test_auto_uses_env_then_cache_then_mac(self):
        os.environ["SHEREGESH_PI2_HOST"] = "10.0.0.2"
        self.assertEqual(self.resolve("pi2", "auto"), "10.0.0.2")
        os.environ.pop("SHEREGESH_PI2_HOST")
        self.assertEqual(self.resolve("pi2"), "10.0.0.2")          # из кэша
        self.cache.unlink()
        mac = boards.BOARDS["pi2"]["mac"]
        self.assertEqual(self.resolve("pi2", finder=lambda m, net: "10.0.0.2" if m == mac else None), "10.0.0.2")

    def test_stale_cache_skipped_and_not_found_reported(self):
        self.cache.write_text('{"pi1": "10.0.0.9"}')                # адрес сменился (DHCP)
        self.assertEqual(self.resolve("pi1", finder=lambda m, n: "10.0.0.1"), "10.0.0.1")
        self.cache.write_text('{"pi1": "10.0.0.2"}')                # по старому адресу теперь pi2
        with self.assertRaises(boards.BoardNotFound):
            self.resolve("pi1")

    def test_resolve_args_maps_options(self):
        a = argparse.Namespace(console="10.0.0.1", remote="auto", other="x")
        with mock.patch.object(boards, "resolve", side_effect=lambda b, h: {"pi1": "10.0.0.1", "pi2": "10.0.0.2"}[b]):
            boards.resolve_args(a)
        self.assertEqual((a.console, a.remote, a.other), ("10.0.0.1", "10.0.0.2", "x"))

    def test_registry_replaces_reference_stand(self):
        """Свои платы — из реестра: серийный номер и MAC читаются с платы; эталонный стенд больше не действует."""
        reg = Path(self.tmp.name) / "reg.json"
        self.assertEqual(boards.load(reg)["pi1"]["source"], "reference")       # файла нет — эталонный стенд
        seen = {"10.0.0.7": ("abc123", "AA:BB:CC:00:00:01"), "10.0.0.8": ("def456", "aa:bb:cc:00:00:02")}

        def run(host, cmd):
            serial, mac = seen[host]
            return 0, mac if cmd == boards.MAC_CMD else serial
        saved = dict(boards.BOARDS)
        try:
            with mock.patch.object(boards, "_remember"):
                boards.register("bench-console", "10.0.0.7", "console", path=reg, run=run)
                boards.register("bench-unit", "10.0.0.8", "remote", path=reg, run=run)
                with self.assertRaisesRegex(boards.BoardNotFound, "уже записана как bench-console"):
                    boards.register("dup", "10.0.0.7", "remote", path=reg, run=run)
            got = boards.load(reg)
            self.assertEqual(set(got), {"bench-console", "bench-unit"})
            self.assertEqual(got["bench-console"]["mac"], "aa:bb:cc:00:00:01")
            self.assertEqual(boards.arg_board(got), {"console": "bench-console", "remote": "bench-unit"})
            self.assertEqual(boards.ARG_BOARD, {"console": "bench-console", "remote": "bench-unit"})
        finally:
            boards.BOARDS.clear()
            boards.BOARDS.update(saved)
            boards.ARG_BOARD.clear()
            boards.ARG_BOARD.update(boards.arg_board())

    def test_reference_board_not_found_suggests_add(self):
        with self.assertRaisesRegex(boards.BoardNotFound, "boards.py add pi1 --host АДРЕС --role console"):
            self.resolve("pi1")

    def test_no_script_keeps_a_board_address_by_default(self):
        hits = subprocess.run(["git", "grep", "-n", "-E", r'"--(console|remote|host)", default="192\.168\.',
                               "--", "tools"], cwd=ROOT, capture_output=True, text=True).stdout
        self.assertEqual(hits, "")


if __name__ == "__main__":
    unittest.main()
