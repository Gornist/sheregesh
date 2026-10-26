# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Правила polkit видео (ADR-0016 §3.3): логика исполняется настоящим движком JavaScript (node) с заглушкой polkit,
как в test_polkit_rule.py. Обычный образ: sheregesh-ui может только «start» sheregesh-video-allow@<IPv4|IPv6|none>.
Dev-образ: службы sheregesh-video-tx/rx управляются так же, как соседи. Интеграция с polkitd — на плате."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGULAR = ROOT / "os/layer/sheregesh-lab.d/52-sheregesh-video-allow.rules"
DEV = ROOT / "os/layer/sheregesh-dev-access.d/50-sheregesh-dev.rules"
HARNESS = r"""
const fs = require('fs');
let rule = null;
const polkit = {Result: {YES: 'YES', NO: 'NO', NOT_HANDLED: 'NOT_HANDLED', AUTH_ADMIN: 'AUTH_ADMIN'},
                addRule: f => { rule = f; }};
eval(fs.readFileSync(process.argv[process.argv.length - 1], 'utf8'));
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
console.log(JSON.stringify(cases.map(c => rule({id: c.id, lookup: k => c.details[k]}, {user: c.user}))));
"""
UNITS = "org.freedesktop.systemd1.manage-units"


def evaluate(rules, cases):
    payload = [{"user": u, "id": a, "details": {"unit": unit, "verb": verb}} for u, a, unit, verb, _ in cases]
    r = subprocess.run(["node", "-e", HARNESS, str(rules)], input=json.dumps(payload), capture_output=True,
                       text=True, check=True)
    return json.loads(r.stdout)


def allow(inst):
    return f"sheregesh-video-allow@{inst}.service"


GOOD = ["none", "192.168.1.92", "10.0.0.1", "169.254.1.2", "255.255.255.255", "fe80::1", "2001:db8::1",
        "fd00:1:2:3:4:5:6:7", "::1", "::ffff:1.2.3.4", "FE80::ABCD"]
BAD = ["", "None", "1.2.3.4/24", "1.2.3.4;", "1.2.3", "256.1.1.1", "01.2.3.4", "1.2.3.4.5", "fe80::1%eth0", "a b",
       "$(id)", "../x", "..", ":::", "1::2::3", "fe80::1:", ":1:2", "x" * 60, "g::1", "1.2.3.4\n", "-h", "fe80::/10",
       "sheregesh", "ф::1"]


@unittest.skipUnless(shutil.which("node"), "нет node")
class VideoAllowPolkitTest(unittest.TestCase):
    def test_regular_image_rule(self):
        cases = [("sheregesh-ui", UNITS, allow(i), "start", "YES") for i in GOOD]
        cases += [("sheregesh-ui", UNITS, allow(i), "start", "NOT_HANDLED") for i in BAD]
        for verb in ("stop", "restart", "try-restart", "reload-or-restart", "kill", "reset-failed", "set-property",
                     "clean", "enable", None):
            cases.append(("sheregesh-ui", UNITS, allow("none"), verb, "NOT_HANDLED"))
            cases.append(("sheregesh-ui", UNITS, allow("192.168.1.92"), verb, "NOT_HANDLED"))
        for unit in ("sheregesh-video-allow@.service", "sheregesh-video-allow.service", "sheregesh-video-tx.service",
                     "sheregesh-video-rx.service", "sheregesh-video-nft.service", "sheregesh-console.service",
                     "sheregesh-deploy@install.service", "ssh.service", "sheregesh-video-allow@none.timer",
                     "xsheregesh-video-allow@none.service", "sheregesh-video-allow@none.service.d", None):
            cases.append(("sheregesh-ui", UNITS, unit, "start", "NOT_HANDLED"))
        for user in ("someone", "root", "sheregesh-uix", ""):
            cases.append((user, UNITS, allow("none"), "start", "NOT_HANDLED"))
            cases.append((user, UNITS, allow("192.168.1.92"), "start", "NOT_HANDLED"))
        cases += [("sheregesh-ui", "org.freedesktop.login1.reboot", None, None, "NOT_HANDLED"),
                  ("sheregesh-ui", "org.freedesktop.systemd1.manage-unit-files", allow("none"), "start", "NOT_HANDLED")]
        for case, res in zip(cases, evaluate(REGULAR, cases)):
            self.assertEqual(res, case[4], str(case[:4]))

    def test_dev_rule_manages_video_services_like_neighbours(self):
        cases = []
        for unit in ("sheregesh-video-tx.service", "sheregesh-video-rx.service", "sheregesh-remote-net.service"):
            for verb in ("start", "stop", "restart"):
                cases.append(("sheregesh-ui", UNITS, unit, verb, "YES"))
            cases.append(("sheregesh-ui", UNITS, unit, "set-property", "NOT_HANDLED"))
            cases.append(("sheregesh-ui", UNITS, unit, "clean", "NOT_HANDLED"))
            cases.append(("someone", UNITS, unit, "restart", "NOT_HANDLED"))
        for unit in ("sheregesh-video-nft.service", "sheregesh-video-allow@none.service", "sheregesh-video-tx.timer"):
            cases.append(("sheregesh-ui", UNITS, unit, "stop", "NOT_HANDLED"))
        for case, res in zip(cases, evaluate(DEV, cases)):
            self.assertEqual(res, case[4], str(case[:4]))


if __name__ == "__main__":
    unittest.main()
