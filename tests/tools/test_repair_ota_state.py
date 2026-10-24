# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""repair_ota_state (S3.2-OTA, pi2): решение «чинить ли» по содержимому state.json и переименование state.json
через debugfs на настоящем ext4 (файл-образ, без sudo и без карты). Без e2fsprogs проверка ext4 пропускается."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/image"))
import repair_ota_state as r  # noqa: E402

PI2 = {"state": "TRYBOOT", "active": "A", "target": "B", "boot_attempts": 1, "expect": {}}


class CheckTests(unittest.TestCase):
    def test_pi2_state_needs_repair(self):
        self.assertIsNone(r.check(json.dumps(PI2), "TRYBOOT", "B")[1])

    def test_state_without_expect_is_left_alone(self):
        d = {k: v for k, v in PI2.items() if k != "expect"}
        self.assertIn("ремонт не нужен", r.check(json.dumps(d), "TRYBOOT", "B")[1])

    def test_unexpected_state_is_refused(self):
        self.assertIn("ожидалось", r.check(json.dumps({**PI2, "state": "COMMITTED"}), "TRYBOOT", "B")[1])

    def test_not_json_is_refused(self):
        self.assertEqual(r.check("{oops", "TRYBOOT", "B")[1], "state.json не JSON")

    def test_multiline_request_goes_through_stdin(self):
        seen = {}

        def run(cmd, input=None, **kw):
            seen.update(cmd=cmd, input=input)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        r.debugfs("disk9s6", f"link {r.STATE} {r.KEPT}\nunlink {r.STATE}\n", write=True, run=run)
        self.assertEqual(seen["cmd"][0], "sudo")
        self.assertIn("-w", seen["cmd"])
        self.assertEqual(seen["cmd"][-3:], ["-f", "-", "/dev/disk9s6"])
        self.assertIn("unlink /sheregesh-ota/state.json", seen["input"])


@unittest.skipUnless(Path("/opt/homebrew/opt/e2fsprogs/sbin/debugfs").exists() or shutil.which("debugfs"),
                     "нет e2fsprogs")
class Ext4RenameTests(unittest.TestCase):
    def test_link_unlink_renames_and_fsck_is_clean(self):
        tool = r.tool
        with tempfile.TemporaryDirectory() as t:
            src = Path(t, "root/sheregesh-ota")
            src.mkdir(parents=True)
            (src / "state.json").write_text(json.dumps(PI2))
            (src / "enable.json").write_text("{}")
            img = Path(t, "p6.img")
            subprocess.run([tool("mke2fs"), "-q", "-t", "ext4", "-d", str(Path(t, "root")), str(img), "16M"], check=True)
            req = f"link {r.STATE} {r.KEPT}\nunlink {r.STATE}\n"
            subprocess.run([tool("debugfs"), "-w", "-f", "-", str(img)], input=req, text=True, capture_output=True,
                           check=True)
            ls = subprocess.run([tool("debugfs"), "-R", "ls /sheregesh-ota", str(img)], capture_output=True,
                                text=True).stdout.split()
            self.assertIn("state.json.a7f1b74", ls)
            self.assertNotIn("state.json", ls)
            kept = subprocess.run([tool("debugfs"), "-R", f"cat {r.KEPT}", str(img)], capture_output=True, text=True)
            self.assertEqual(json.loads(kept.stdout), PI2)
            fsck = subprocess.run([tool("e2fsck"), "-fn", str(img)], capture_output=True, text=True)
            self.assertEqual(fsck.returncode, 0, fsck.stdout)


if __name__ == "__main__":
    unittest.main()
