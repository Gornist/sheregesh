# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""shdeploy ota-update: шаги по порядку; сбой до подтверждения — ota-abort и ota-disable; плата не вернулась —
остановка с указанием выключить и включить питание (без abort: плата недоступна)."""
import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/deploy"))
import shdeploy  # noqa: E402


class FakeRemote:
    def __init__(self, up_after=0):
        self.calls, self.up_after = 0, up_after

    def run(self, cmd, timeout=900, **kw):
        self.calls += 1
        return (0 if self.calls > self.up_after else 255), "", ""


def args(**kw):
    return argparse.Namespace(board="pi1", host="10.0.0.1", key=None, payload="p.shota", sha256="ab" * 32,
                              wait=60, settle=5, command="ota-update", **kw)


class OtaUpdateTest(unittest.TestCase):
    def run_update(self, results, remote):
        steps = []

        def step(a):
            action = a.command[len("ota-"):]
            steps.append(action)
            return results.get(action, 0)
        t = [0.0]

        def sleep(sec):
            t[0] += sec
        with mock.patch.object(shdeploy, "cmd_ota", side_effect=step):
            rc = shdeploy.cmd_ota_update(args(), sleep=sleep, clock=lambda: t[0], remote=remote)
        return rc, steps

    def test_happy_path_in_order(self):
        rc, steps = self.run_update({}, FakeRemote(up_after=2))
        self.assertEqual(rc, 0)
        self.assertEqual(steps, ["install", "enable", "tryboot", "confirm", "disable"])

    def test_install_failure_changes_nothing(self):
        rc, steps = self.run_update({"install": 1}, FakeRemote())
        self.assertEqual((rc, steps), (1, ["install"]))

    def test_confirm_failure_returns_to_working_slot(self):
        rc, steps = self.run_update({"confirm": 1}, FakeRemote())
        self.assertEqual(rc, 1)
        self.assertEqual(steps, ["install", "enable", "tryboot", "confirm", "abort", "disable"])

    def test_board_not_back_asks_for_power_cycle(self):
        rc, steps = self.run_update({}, FakeRemote(up_after=10 ** 6))
        self.assertEqual(rc, 2)
        self.assertEqual(steps, ["install", "enable", "tryboot"])


if __name__ == "__main__":
    unittest.main()
