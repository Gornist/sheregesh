# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""DEV-образ: root по SSH только по ключу владельца (решение 2026-10-30), пароли выключены; проверка образа ssh.root."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class DevRootAccessTest(unittest.TestCase):
    def test_layer_key_only_root(self):
        layer = (ROOT / "os/layer/sheregesh-dev-access.yaml").read_text()
        self.assertIn("PermitRootLogin prohibit-password", layer)
        self.assertNotIn("PermitRootLogin yes", layer)
        self.assertIn("PasswordAuthentication no", layer)
        self.assertIn("$1/root/.ssh/authorized_keys", layer)

    def test_strict_image_has_no_root_ssh(self):
        for f in ("os/layer/sheregesh-lab.yaml", "os/layer/sheregesh-base.yaml", "os/config/sheregesh-lab-pi4.yaml"):
            self.assertNotIn("PermitRootLogin", (ROOT / f).read_text(), f)

    def test_verify_checks_root_key(self):
        v = (ROOT / "tools/image/verify_image.py").read_text()
        self.assertIn('"ssh.root"', v)
        self.assertIn("PermitRootLogin prohibit-password", v)


if __name__ == "__main__":
    unittest.main()
