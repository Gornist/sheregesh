# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Выключение блока с пульта (протокол 1.6, REQ-UI-006): правило polkit 54 — sheregesh-ui только «start»
sheregesh-remote-poweroff.service (движок JavaScript — node, как test_video_polkit), юнит и проверки verify_image."""
import contextlib
import io
import shutil
import unittest
from pathlib import Path

from test_video_polkit import UNITS, evaluate

ROOT = Path(__file__).resolve().parents[2]
RULE = ROOT / "os/layer/sheregesh-lab.d/54-sheregesh-remote-power.rules"
UNIT = ROOT / "tools/image/units/sheregesh-remote-poweroff.service"
OFF = "sheregesh-remote-poweroff.service"

import sys  # noqa: E402
sys.path.insert(0, str(ROOT / "tools/image"))
import verify_image as vi  # noqa: E402


@unittest.skipUnless(shutil.which("node"), "нет node")
class RemotePowerPolkitTest(unittest.TestCase):
    def test_rule(self):
        cases = [("sheregesh-ui", UNITS, OFF, "start", "YES")]
        for verb in ("stop", "restart", "kill", "set-property", "enable", None):
            cases.append(("sheregesh-ui", UNITS, OFF, verb, "NOT_HANDLED"))
        for unit in ("poweroff.target", "reboot.target", "sheregesh-remote-poweroff.timer", "xsheregesh-remote-poweroff.service",
                     "sheregesh-remote-net.service", None):
            cases.append(("sheregesh-ui", UNITS, unit, "start", "NOT_HANDLED"))
        for user in ("someone", "root", "sheregesh-uix"):
            cases.append((user, UNITS, OFF, "start", "NOT_HANDLED"))
        for action in ("org.freedesktop.login1.power-off", "org.freedesktop.login1.reboot"):
            cases.append(("sheregesh-ui", action, OFF, "start", "NOT_HANDLED"))
        self.assertEqual(evaluate(RULE, cases), [c[-1] for c in cases])


class RemotePowerImageTest(unittest.TestCase):
    def run_checks(self, files):
        class Root:
            cat = staticmethod(lambda p: files.get(p, ""))
            exists = staticmethod(lambda p: p in files)
        rep = vi.Report()
        with contextlib.redirect_stdout(io.StringIO()):
            vi.remote_power_checks(rep, Root)
        return {c["id"]: c["status"] for c in rep.checks}

    def test_layer_installs_rule(self):
        self.assertIn("54-sheregesh-remote-power.rules", (ROOT / "os/layer/sheregesh-lab.yaml").read_text())

    def test_verify(self):
        files = {f"/usr/lib/systemd/system/{OFF}": UNIT.read_text(), vi.REMOTE_POWER_POLKIT: RULE.read_text()}
        self.assertEqual(self.run_checks(files), {"remote_power.unit": "PASS", "remote_power.polkit": "PASS"})
        loose = {**files, vi.REMOTE_POWER_POLKIT: RULE.read_text() + '\n// action.id == "org.freedesktop.login1.power-off"'}
        self.assertEqual(self.run_checks(loose)["remote_power.polkit"], "FAIL")
        self.assertEqual(self.run_checks({vi.REMOTE_POWER_POLKIT: RULE.read_text()})["remote_power.unit"], "FAIL")


if __name__ == "__main__":
    unittest.main()
