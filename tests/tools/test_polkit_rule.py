# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Правило polkit dev-образа (S3.1-DEPLOY): логика исполняется настоящим движком JavaScript (node) с заглушкой
объекта polkit. Проверяется, что sheregesh-ui получает только «start» трёх экземпляров установщика, а прежние
права (службы стенда, перезагрузка) не расширились. Интеграция с polkitd — на плате (план испытаний)."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "os/layer/sheregesh-dev-access.d/50-sheregesh-dev.rules"
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
CASES = [  # (пользователь, действие, unit, verb, ожидание)
    ("sheregesh-ui", UNITS, "sheregesh-deploy@install.service", "start", "YES"),
    ("sheregesh-ui", UNITS, "sheregesh-deploy@rollback.service", "start", "YES"),
    ("sheregesh-ui", UNITS, "sheregesh-deploy@factory.service", "start", "YES"),
    ("sheregesh-ui", UNITS, "sheregesh-deploy@install.service", "stop", "NOT_HANDLED"),
    ("sheregesh-ui", UNITS, "sheregesh-deploy@install.service", "restart", "NOT_HANDLED"),
    ("sheregesh-ui", UNITS, "sheregesh-deploy@shell.service", "start", "NOT_HANDLED"),
    ("sheregesh-ui", UNITS, "sheregesh-deploy@.service", "start", "NOT_HANDLED"),
    ("sheregesh-ui", UNITS, "sheregesh-ota@0.service", "start", "NOT_HANDLED"),
    ("sheregesh-ui", UNITS, "ssh.service", "stop", "NOT_HANDLED"),
    ("sheregesh-ui", UNITS, "sheregesh-console.service", "restart", "YES"),      # права S2/S3.1 сохранены
    ("sheregesh-ui", UNITS, "sheregesh-remote-net.service", "start", "YES"),
    ("sheregesh-ui", UNITS, "sheregesh-remote-net.service", "kill", "YES"),          # SIGHUP: перечитать video-input.json
    ("sheregesh-ui", UNITS, "sheregesh-remote-net.service", "reset-failed", "YES"),
    ("sheregesh-ui", UNITS, "sheregesh-remote-net.service", "set-property", "NOT_HANDLED"),   # S5 LS-6
    ("sheregesh-ui", UNITS, "sheregesh-console.service", "set-property", "NOT_HANDLED"),
    ("sheregesh-ui", UNITS, "sheregesh-remote-net.service", "clean", "NOT_HANDLED"),
    ("someone", UNITS, "sheregesh-deploy@install.service", "start", "NOT_HANDLED"),
    ("sheregesh-ui", "org.freedesktop.login1.reboot", None, None, "YES"),
    ("sheregesh-ui", "org.freedesktop.login1.set-reboot-parameter", None, None, "YES"),
    ("sheregesh-ui", "org.freedesktop.systemd1.manage-unit-files", "x.service", None, "NOT_HANDLED"),
]


@unittest.skipUnless(shutil.which("node"), "нет node")
class PolkitRuleTest(unittest.TestCase):
    def test_rule_grants_only_listed_actions(self):
        cases = [{"user": u, "id": a, "details": {"unit": unit, "verb": verb}} for u, a, unit, verb, _ in CASES]
        r = subprocess.run(["node", "-e", HARNESS, str(RULES)], input=json.dumps(cases), capture_output=True,
                           text=True, check=True)
        got = json.loads(r.stdout)
        for (u, a, unit, verb, want), res in zip(CASES, got):
            self.assertEqual(res, want, f"{u} {a} {unit} {verb}")


if __name__ == "__main__":
    unittest.main()
