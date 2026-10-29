# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Помощник tools/image/bin/sheregesh-time (ADR-0017): команды chronyc по роли платы и адресу сеанса.

chronyc подменяется заглушкой (runner); каталог конфигурации и файл состояния — временные. Проверяется: пульт —
пул, local stratum 10 и allow блоку; блок — только пульт, без пула; сеансовый адрес на блоке заменяет прежний (источники
из peer_hosts не удаляются); недопустимые аргументы — код 2 до chronyc; «источник уже есть» — не ошибка; без роли —
ничего не делается; в исходнике нет shell."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/image/bin/sheregesh-time"
_loader = importlib.machinery.SourceFileLoader("sheregesh_time", str(SCRIPT))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
mod = importlib.util.module_from_spec(_spec)
_loader.exec_module(mod)


class Runner:
    def __init__(self, code=0, out=""):
        self.calls, self.code, self.out = [], code, out

    def __call__(self, args):
        self.calls.append(list(args))
        return types.SimpleNamespace(returncode=self.code, stdout=self.out, stderr="")


class TimeHelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "cfg"
        self.cfg.mkdir()
        self.state = Path(self.tmp.name) / "peer"

    def tearDown(self):
        self.tmp.cleanup()

    def board(self, role, hosts=("192.168.1.171",)):
        (self.cfg / f"{role}.role").write_text("urn\n")
        (self.cfg / "link.json").write_text(json.dumps({"role": role, "peer_hosts": list(hosts)}))

    def run_main(self, *argv, runner=None):
        runner = runner or Runner()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = mod.main(list(argv), runner=runner, config=self.cfg, state=self.state)
        return rc, getattr(runner, "calls", None)

    def test_console_role_serves_remote_and_runs_without_internet(self):
        self.board("console", ["192.168.1.171", "remote.local", "2001:db8::1"])
        rc, calls = self.run_main("role")
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [["add", "pool", "2.debian.pool.ntp.org", "iburst"], ["local", "stratum", "10"],
                                 ["allow", "192.168.1.171"], ["allow", "2001:db8::1"]])     # имя пропущено

    def test_remote_role_uses_only_console(self):
        self.board("remote", ["192.168.1.92"])
        rc, calls = self.run_main("role")
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [["add", "server", "192.168.1.92", "iburst", "minpoll", "4", "maxpoll", "6"]])
        self.assertFalse(any("pool" in c for c in calls))               # часы блока — по пульту, не по интернету

    def test_peer_on_console_allows_session_address(self):
        self.board("console")
        self.assertEqual(self.run_main("peer", "192.168.1.172"), (0, [["allow", "192.168.1.172"]]))
        self.assertEqual(self.run_main("peer", "none"), (0, []))

    def test_peer_on_remote_replaces_previous_session_source(self):
        self.board("remote", ["192.168.1.92"])
        rc, calls = self.run_main("peer", "192.168.1.95")
        self.assertEqual((rc, calls), (0, [["add", "server", "192.168.1.95", "iburst", "minpoll", "4", "maxpoll", "6"]]))
        self.assertEqual(self.state.read_text(), "192.168.1.95\n")
        rc, calls = self.run_main("peer", "192.168.1.96")
        self.assertEqual(calls, [["delete", "192.168.1.95"],
                                 ["add", "server", "192.168.1.96", "iburst", "minpoll", "4", "maxpoll", "6"]])
        self.assertEqual(self.run_main("peer", "192.168.1.96"), (0, []))            # тот же сеансовый адрес
        rc, calls = self.run_main("peer", "192.168.1.92")                           # адрес из peer_hosts
        self.assertEqual(calls[0], ["delete", "192.168.1.96"])
        self.assertEqual(self.run_main("peer", "none"), (0, []))                    # 92 добавлен role — не удаляется

    def test_existing_source_is_not_an_error(self):
        self.board("remote", ["192.168.1.92"])
        rc, _ = self.run_main("role", runner=Runner(1, "511 Source already present"))
        self.assertEqual(rc, 0)
        rc, _ = self.run_main("role", runner=Runner(1, "501 Not authorised"))
        self.assertEqual(rc, 1)

    def test_invalid_arguments_rejected_before_chronyc(self):
        self.board("console")
        for argv in ([], ["peer"], ["peer", "1.2.3.4", "x"], ["role", "x"], ["serve"], ["peer", "1.2.3.4/24"],
                     ["peer", "1.2.3.4;rm"], ["peer", "fe80::1%eth0"], ["peer", "0.0.0.0"], ["peer", "224.0.0.1"],
                     ["peer", ""], ["peer", "a" * 60], ["peer", "None"]):
            self.assertEqual(self.run_main(*argv), (2, []), argv)

    def test_no_role_does_nothing(self):
        self.assertEqual(self.run_main("role"), (0, []))
        (self.cfg / "console.role").write_text("x")
        (self.cfg / "remote.role").write_text("x")                                  # обе роли — не угадываем
        self.assertEqual(self.run_main("peer", "192.168.1.92"), (0, []))

    def test_chronyc_missing_is_code_1(self):
        self.board("console")

        def boom(args):
            raise FileNotFoundError("chronyc")
        self.assertEqual(self.run_main("role", runner=boom)[0], 1)

    def test_save_moves_mark_forward_only(self):
        import os
        drift = Path(self.tmp.name) / "chrony" / "chrony.drift"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mod.save(drift, now=1793300000.0), 1)          # нет каталога /persistent/chrony
            drift.parent.mkdir()
            self.assertEqual(mod.save(drift, now=1793300000.0), 0)          # создан: плата ещё не синхронизирована
            self.assertEqual(drift.read_text(), mod.DRIFT_NEW)
            self.assertEqual(drift.stat().st_mtime, 1793300000.0)
            drift.write_text("12.345678 0.123456\n")                        # chrony записал частоту
            os.utime(drift, (1793400000.0, 1793400000.0))
            self.assertEqual(mod.save(drift, now=1793300000.0), 0)          # часы позади отметки — не трогать
            self.assertEqual(drift.stat().st_mtime, 1793400000.0)
            self.assertEqual(mod.save(drift, now=1793500000.0), 0)
            self.assertEqual(drift.stat().st_mtime, 1793500000.0)
            self.assertEqual(drift.read_text(), "12.345678 0.123456\n")      # содержимое chrony не меняется
        rc, calls = self.run_main("save", runner=Runner())
        self.assertEqual(calls, [])                                         # chronyc не нужен
        self.assertEqual(self.run_main("save", "x"), (2, []))

    def test_no_shell_and_executable(self):
        src = SCRIPT.read_text()
        self.assertNotIn("shell=True", src)
        self.assertNotIn("os.system", src)
        self.assertTrue(SCRIPT.stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
