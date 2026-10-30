# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Имя системы по роли платы (tools/image/bin/sheregesh-hostname): пульт pi1, блок pi2, файл hostname — с проверкой;
юнит до systemd-networkd; проверка образа hostname.role."""
import configparser
import contextlib
import importlib.machinery
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_loader = importlib.machinery.SourceFileLoader("sheregesh_hostname", str(ROOT / "tools/image/bin/sheregesh-hostname"))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
mod = importlib.util.module_from_spec(_spec)
_loader.exec_module(mod)
_vspec = importlib.util.spec_from_file_location("verify_image", ROOT / "tools/image/verify_image.py")
vi = importlib.util.module_from_spec(_vspec)
_vspec.loader.exec_module(vi)


class HostnameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "cfg"
        self.cfg.mkdir()
        self.kernel = Path(self.tmp.name) / "hostname"
        self.kernel.write_text("sheregesh-dev")

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return mod.main(config=self.cfg, kernel=self.kernel)

    def test_role_names(self):
        (self.cfg / "console.role").write_text("urn")
        self.assertEqual((self.run_main(), self.kernel.read_text()), (0, "pi1"))
        (self.cfg / "console.role").unlink()
        (self.cfg / "remote.role").write_text("urn")
        self.assertEqual((self.run_main(), self.kernel.read_text()), (0, "pi2"))

    def test_no_or_both_roles_keep_image_name(self):
        self.assertEqual((self.run_main(), self.kernel.read_text()), (0, "sheregesh-dev"))
        (self.cfg / "console.role").write_text("urn")
        (self.cfg / "remote.role").write_text("urn")
        self.assertEqual((self.run_main(), self.kernel.read_text()), (0, "sheregesh-dev"))

    def test_override_file_validated(self):
        (self.cfg / "remote.role").write_text("urn")
        (self.cfg / "hostname").write_text("bench-remote-2\n")
        self.assertEqual((self.run_main(), self.kernel.read_text()), (0, "bench-remote-2"))
        for bad in ("-x", "a" * 64, "Pi1", "pi 1", "pi1;reboot", "", "x.example"):
            self.kernel.write_text("sheregesh-dev")
            (self.cfg / "hostname").write_text(bad)
            self.assertEqual((self.run_main(), self.kernel.read_text()), (2, "sheregesh-dev"), bad)

    def test_write_failure_is_code_1(self):
        (self.cfg / "console.role").write_text("urn")
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mod.main(config=self.cfg, kernel=Path(self.tmp.name) / "нет" / "hostname"), 1)

    def test_unit_before_network(self):
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        text = (ROOT / "tools/image/units/sheregesh-hostname.service").read_text()
        cp.read_string(text)
        self.assertIn("systemd-networkd.service", cp["Unit"]["Before"])
        self.assertEqual(cp["Unit"]["After"], "local-fs.target")
        self.assertEqual(cp["Install"]["WantedBy"], "sysinit.target")
        self.assertIn("sheregesh-hostname", (ROOT / "os/layer/sheregesh-lab.yaml").read_text())
        files = {"/usr/lib/systemd/system/sheregesh-hostname.service": text,
                 "/usr/bin/sheregesh-hostname": "exec /usr/bin/python3 -I /usr/lib/sheregesh/tools/image/bin/x",
                 "/usr/lib/sheregesh/tools/image/bin/sheregesh-hostname": "x",
                 "/etc/systemd/system/sysinit.target.wants/sheregesh-hostname.service": "x"}

        class Root:
            cat = staticmethod(lambda p: files.get(p, ""))
            exists = staticmethod(lambda p: p in files)
            ls = staticmethod(lambda p: [f for f in files if f.startswith(p.rstrip("/") + "/")])
        rep = vi.Report()
        with contextlib.redirect_stdout(io.StringIO()):
            vi.hostname_checks(rep, Root)
        self.assertEqual([c["status"] for c in rep.checks], ["PASS"])
        del files["/etc/systemd/system/sysinit.target.wants/sheregesh-hostname.service"]
        rep = vi.Report()
        with contextlib.redirect_stdout(io.StringIO()):
            vi.hostname_checks(rep, Root)
        self.assertEqual([c["status"] for c in rep.checks], ["FAIL"])


if __name__ == "__main__":
    unittest.main()
