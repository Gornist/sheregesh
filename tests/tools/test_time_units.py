# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Время между платами (ADR-0017): юниты sheregesh-time-*, пакет (зависимость chrony, помощник и обёртка), слой
образа (chrony.conf, правило polkit, включение) и проверки verify_image.time_checks."""
import configparser
import contextlib
import importlib.util
import io
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNITS = ROOT / "tools/image/units"
LAB = ROOT / "os/layer/sheregesh-lab.d"
_spec = importlib.util.spec_from_file_location("verify_image", ROOT / "tools/image/verify_image.py")
vi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vi)
_bspec = importlib.util.spec_from_file_location("build_deb", ROOT / "tools/image/build_deb.py")
bd = importlib.util.module_from_spec(_bspec)
_bspec.loader.exec_module(bd)


def unit(name):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    text = (UNITS / name).read_text()
    cp.read_string(text)
    return cp, text


class TimeUnitsTest(unittest.TestCase):
    def test_role_unit_after_chrony_and_sandboxed(self):
        cp, _ = unit("sheregesh-time-role.service")
        self.assertEqual(cp["Service"]["ExecStart"], "/usr/bin/sheregesh-time role")
        self.assertEqual(cp["Unit"]["PartOf"], "chrony.service")
        self.assertIn("chrony.service", cp["Unit"]["After"])
        self.assertEqual(cp["Service"]["IPAddressDeny"], "any")
        self.assertEqual(cp["Service"]["RestrictAddressFamilies"], "AF_UNIX")
        self.assertEqual(cp["Service"]["RuntimeDirectory"], "sheregesh-time")
        self.assertEqual(cp["Install"]["WantedBy"], "multi-user.target")

    def test_peer_template(self):
        cp, text = unit("sheregesh-time-peer@.service")
        self.assertEqual(cp["Service"]["ExecStart"], "/usr/bin/sheregesh-time peer %i")
        self.assertEqual(cp["Service"]["Type"], "oneshot")
        self.assertIn("/run/chrony", cp["Service"]["ReadWritePaths"])
        self.assertNotIn("[Install]", text)                         # запускается только по сеансу

    def test_clock_mark_units(self):
        cp, _ = unit("sheregesh-time-save.service")
        self.assertEqual(cp["Service"]["ExecStart"], "/usr/bin/sheregesh-time save")
        self.assertEqual(cp["Service"]["ExecStop"], "/usr/bin/sheregesh-time save")    # отметка при выключении
        self.assertEqual(cp["Unit"]["Conflicts"], "shutdown.target")
        self.assertIn("/persistent", cp["Unit"]["RequiresMountsFor"])
        tick, _ = unit("sheregesh-time-tick.service")
        self.assertEqual(tick["Service"]["ExecStart"], "/usr/bin/sheregesh-time save")
        timer, _ = unit("sheregesh-time-tick.timer")
        self.assertEqual(timer["Timer"]["OnUnitActiveSec"], "10min")
        self.assertIn("d /persistent/chrony 0750 _chrony _chrony",
                      (ROOT / "tools/image/tmpfiles/sheregesh-chrony.conf").read_text())

    def test_package_and_layer(self):
        self.assertIn("chrony", bd.DEPENDS)
        self.assertIn("sheregesh-time", bd.HELPERS)
        lab = (ROOT / "os/layer/sheregesh-lab.yaml").read_text()
        for x in ("chrony.conf", "53-sheregesh-time-peer.rules", "mask systemd-timesyncd.service",
                  "chrony sheregesh-time-role sheregesh-time-save sheregesh-time-tick.timer", 'DAEMON_OPTS="-F 1 -s"',
                  "chrony.service.d/50-sheregesh.conf", "ReadWritePaths=/persistent/chrony"):
            self.assertIn(x, lab)
        self.assertTrue((LAB / "chrony.conf").exists() and (LAB / "53-sheregesh-time-peer.rules").exists())

    def image(self):
        files = {"/usr/sbin/chronyd": "x", "/etc/chrony/chrony.conf": (LAB / "chrony.conf").read_text(),
                 "/usr/lib/systemd/system/sheregesh-time-role.service": unit("sheregesh-time-role.service")[1],
                 "/usr/lib/systemd/system/sheregesh-time-peer@.service": unit("sheregesh-time-peer@.service")[1],
                 "/etc/systemd/system/multi-user.target.wants/chrony.service": "x",
                 "/etc/systemd/system/multi-user.target.wants/sheregesh-time-role.service": "x",
                 "/usr/bin/sheregesh-time": "exec /usr/bin/python3 -I /usr/lib/sheregesh/tools/image/bin/sheregesh-time",
                 "/usr/lib/sheregesh/tools/image/bin/sheregesh-time": "x",
                 vi.TIME_POLKIT: (LAB / "53-sheregesh-time-peer.rules").read_text(),
                 "/etc/default/chrony": 'DAEMON_OPTS="-F 1 -s"\n',
                 "/etc/systemd/system/chrony.service.d/50-sheregesh.conf":
                     "[Unit]\nRequiresMountsFor=/persistent\n[Service]\nReadWritePaths=/persistent/chrony\n",
                 "/usr/lib/tmpfiles.d/sheregesh-chrony.conf": (ROOT / "tools/image/tmpfiles/sheregesh-chrony.conf").read_text(),
                 "/etc/systemd/system/multi-user.target.wants/sheregesh-time-save.service": "x",
                 "/etc/systemd/system/timers.target.wants/sheregesh-time-tick.timer": "x"}
        return files, "chrony\t4.6\tabc\npython3\t3.13\tabc\n"

    def run_checks(self, files, tsv):
        class Root:
            cat = staticmethod(lambda p: files.get(p, ""))
            exists = staticmethod(lambda p: p in files)
            ls = staticmethod(lambda p: [f for f in files if f.startswith(p.rstrip("/") + "/")])
        rep = vi.Report()
        with contextlib.redirect_stdout(io.StringIO()):
            vi.time_checks(rep, Root, tsv)
        return {c["id"]: c["status"] for c in rep.checks}

    def test_verify_passes_and_catches_regressions(self):
        files, tsv = self.image()
        r = self.run_checks(files, tsv)
        self.assertEqual({k: v for k, v in r.items() if v != "PASS"}, {})
        self.assertEqual(len(r), 7)
        self.assertEqual(self.run_checks(files, tsv + "systemd-timesyncd\t257\tabc\n")["time.chrony"], "FAIL")
        masked = {**files, "/etc/systemd/system/systemd-timesyncd.service": ""}
        self.assertEqual(self.run_checks(masked, tsv + "systemd-timesyncd\t257\tabc\n")["time.chrony"], "PASS")
        broken = dict(files)
        broken["/etc/chrony/chrony.conf"] += "pool 2.debian.pool.ntp.org iburst\nallow 0.0.0.0/0\n"
        del broken[vi.TIME_POLKIT]
        del broken["/etc/systemd/system/multi-user.target.wants/sheregesh-time-role.service"]
        broken["/etc/default/chrony"] = 'DAEMON_OPTS="-F 1"\n'                    # без -s
        broken["/usr/lib/systemd/system/sheregesh-time-role.service"] = "ExecStart=/usr/bin/sheregesh-time role\n"
        r = self.run_checks(broken, tsv)
        for cid in ("time.conf", "time.polkit", "time.enabled", "time.units", "time.boot_clock"):
            self.assertEqual(r[cid], "FAIL", cid)


if __name__ == "__main__":
    unittest.main()
