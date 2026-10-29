# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Правило polkit времени (ADR-0017): sheregesh-ui может только «start» sheregesh-time-peer@<IPv4|IPv6|none>.
Движок JavaScript (node) и наборы адресов — как у правила видео (test_video_polkit)."""
import shutil
import unittest
from pathlib import Path

from test_video_polkit import BAD, GOOD, UNITS, evaluate

RULE = Path(__file__).resolve().parents[2] / "os/layer/sheregesh-lab.d/53-sheregesh-time-peer.rules"


def peer(inst):
    return f"sheregesh-time-peer@{inst}.service"


@unittest.skipUnless(shutil.which("node"), "нет node")
class TimePeerPolkitTest(unittest.TestCase):
    def test_rule(self):
        cases = [("sheregesh-ui", UNITS, peer(i), "start", "YES") for i in GOOD]
        cases += [("sheregesh-ui", UNITS, peer(i), "start", "NOT_HANDLED") for i in BAD]
        for verb in ("stop", "restart", "kill", "set-property", "enable", None):
            cases.append(("sheregesh-ui", UNITS, peer("192.168.1.92"), verb, "NOT_HANDLED"))
        for unit in ("sheregesh-time-peer@.service", "sheregesh-time-role.service", "chrony.service",
                     "sheregesh-video-allow@none.service", "xsheregesh-time-peer@none.service",
                     "sheregesh-time-peer@none.timer", None):
            cases.append(("sheregesh-ui", UNITS, unit, "start", "NOT_HANDLED"))
        for user in ("someone", "root", "sheregesh-uix"):
            cases.append((user, UNITS, peer("none"), "start", "NOT_HANDLED"))
        cases.append(("sheregesh-ui", "org.freedesktop.login1.reboot", peer("none"), "start", "NOT_HANDLED"))
        got = evaluate(RULE, cases)
        self.assertEqual(got, [c[-1] for c in cases])


if __name__ == "__main__":
    unittest.main()
