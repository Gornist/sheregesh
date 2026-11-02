# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""labpki pair: обе платы проверяются до выпуска ключей — недоступный блок не оставляет пульт с новым ключом;
ключ хоста — accept-new (новая плата без ручной правки known_hosts), сменившийся ключ — подсказка ssh-keygen -R."""
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("labpki", ROOT / "tools/lab-pki/labpki.py")
labpki = importlib.util.module_from_spec(spec)
spec.loader.exec_module(labpki)


def done(rc, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class PairTest(unittest.TestCase):
    def test_unreachable_remote_stops_before_any_key(self):
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd[-1])
            host = cmd[-2].split("@")[1]
            return done(0, "dev-1\n") if host == "10.0.0.1" else done(255, "", "ssh: connect: No route to host")
        with mock.patch.object(labpki.subprocess, "run", side_effect=fake), \
                mock.patch.object(labpki, "enroll") as enroll:
            with self.assertRaisesRegex(SystemExit, "remote 10.0.0.2: недоступна"):
                labpki.main(["pair", "--console", "10.0.0.1", "--remote", "10.0.0.2"])
        enroll.assert_not_called()
        self.assertTrue(all("keygen" not in c and "install" not in c for c in calls), calls)

    def test_changed_host_key_hint(self):
        with mock.patch.object(labpki.subprocess, "run",
                               return_value=done(255, "", "Host key verification failed.")):
            with self.assertRaisesRegex(SystemExit, "ssh-keygen -R 10.0.0.5"):
                labpki.preflight("10.0.0.5", "console")

    def test_ssh_accepts_new_host_keys(self):
        self.assertIn("StrictHostKeyChecking=accept-new", labpki.boards.SSH_OPTS)
        with mock.patch.object(labpki.subprocess, "run", return_value=done(0, "ok")) as run:
            labpki.ssh("10.0.0.1", "sheregesh-link status")
        self.assertIn("StrictHostKeyChecking=accept-new", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
