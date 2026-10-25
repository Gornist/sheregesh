# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Проект п. 7 (tools/lab-video/pi2): правило polkit, помощник root (адрес, слоты, attach/detach против настоящего
эмулятора с подменённым sysfs vhci), разбор DV timings в lab-vivid. Ядро и systemd не нужны.

  python3 -m unittest tools/lab-video/pi2/tests/test_pi2_lab_video.py
Место в дереве тестов (tests/tools) выбирает владелец tests/** при встраивании в образ."""
import asyncio
import importlib.machinery
import importlib.util
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PI2 = Path(__file__).resolve().parents[1]
LAB = PI2.parent
sys.path.insert(0, str(LAB))
import usbip_attach  # noqa: E402
import usbip_uvc  # noqa: E402


def load(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(PI2 / "bin" / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


helper = load("sheregesh-lab-video")
labvivid = load("lab-vivid")

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


@unittest.skipUnless(shutil.which("node"), "нужен node для движка правил polkit")
class PolkitRule(unittest.TestCase):
    CASES = [
        ("sheregesh-ui", UNITS, "sheregesh-lab-vivid.service", "start", "YES"),
        ("sheregesh-ui", UNITS, "sheregesh-lab-vivid.service", "stop", "YES"),
        ("sheregesh-ui", UNITS, "sheregesh-lab-vivid-feed@cvbs.service", "restart", "YES"),
        ("sheregesh-ui", UNITS, "sheregesh-lab-vivid-feed@hdmi.service", "start", "YES"),
        ("sheregesh-ui", UNITS, "sheregesh-lab-usbip@hdmi.service", "start", "YES"),
        ("sheregesh-ui", UNITS, "sheregesh-lab-usbip@cvbs.service", "stop", "YES"),
        ("sheregesh-ui", UNITS, "sheregesh-lab-usbip@camera.service", "restart", "YES"),
        # чужие экземпляры шаблонов, другие глаголы, другие пользователи и действия — не наше дело
        ("sheregesh-ui", UNITS, "sheregesh-lab-usbip@evil.service", "start", "NOT_HANDLED"),
        ("sheregesh-ui", UNITS, "sheregesh-lab-usbip@hdmi;x.service", "start", "NOT_HANDLED"),
        ("sheregesh-ui", UNITS, "sheregesh-lab-vivid-feed@sh.service", "start", "NOT_HANDLED"),
        ("sheregesh-ui", UNITS, "sheregesh-lab-vivid.service", "enable", "NOT_HANDLED"),
        ("sheregesh-ui", UNITS, "sheregesh-lab-vivid.service", "reload-or-restart", "NOT_HANDLED"),
        ("sheregesh-ui", UNITS, "ssh.service", "stop", "NOT_HANDLED"),
        ("pilot", UNITS, "sheregesh-lab-vivid.service", "start", "NOT_HANDLED"),
        ("sheregesh-ui", "org.freedesktop.systemd1.manage-unit-files", "sheregesh-lab-vivid.service", "start",
         "NOT_HANDLED"),
    ]

    def test_rule(self):
        cases = [{"user": u, "id": a, "details": {"unit": un, "verb": v}} for u, a, un, v, _ in self.CASES]
        with tempfile.NamedTemporaryFile("w", suffix=".js") as h:
            h.write(HARNESS)
            h.flush()
            out = subprocess.run(["node", h.name, str(PI2 / "etc/51-sheregesh-lab-video.rules")],
                                 input=json.dumps(cases), capture_output=True, text=True, check=True).stdout
        for case, got in zip(self.CASES, json.loads(out)):
            self.assertEqual(got, case[-1], case)


class HelperConfig(unittest.TestCase):
    def conf(self, **kw):
        base = {"SHCAM_HOST": "192.168.1.193", "SHCAM_USBIP_PORT": "3240", "LAB_NET": "192.168.1.0/24"}
        base.update(kw)
        return base

    def test_shipped_config_is_valid(self):
        c = helper.read_conf(PI2 / "etc/lab-video.conf")
        self.assertEqual(helper.usbip_target(c), ("192.168.1.193", 3240))

    def test_refusals(self):
        for kw in ({"SHCAM_HOST": "10.0.0.5"}, {"SHCAM_HOST": "shcam.local"}, {"SHCAM_HOST": "8.8.8.8",
                   "LAB_NET": "8.8.8.0/24"}, {"LAB_NET": "192.168.1.7/24"}, {"SHCAM_USBIP_PORT": "0"},
                   {"SHCAM_USBIP_PORT": "x"}, {"SHCAM_HOST": ""}):
            with self.subTest(kw), self.assertRaises(helper.Refused):
                helper.usbip_target(self.conf(**kw))

    def test_slots_are_fixed(self):
        self.assertEqual(helper.SLOTS, {"hdmi": "1-1", "cvbs": "1-2", "camera": "1-3"})
        with self.assertRaises(SystemExit):
            helper.main(["usbip", "start", "evil"])

    def test_bad_config_line(self):
        with tempfile.NamedTemporaryFile("w") as f:
            f.write("SHCAM_HOST 192.168.1.193\n")
            f.flush()
            with self.assertRaises(helper.Refused):
                helper.read_conf(f.name)


class HelperUsbipCycle(unittest.TestCase):
    """start → OP_REQ_IMPORT к настоящему эмулятору → запись «порт fd devid скорость» в подменённый attach;
    stop → detach того же порта; повторный stop без файла порта — без ошибок."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ctl = root / "platform/vhci_hcd.0"
        self.ctl.mkdir(parents=True)
        (self.ctl / "status").write_text("hub port sta spd dev      sockfd local_busid\n"
                                         "hs  0000 004 000 00000000 000000 0-0\n"
                                         "hs  0001 004 000 00000000 000000 0-0\n")
        self.orig_vhci, usbip_attach.VHCI = usbip_attach.VHCI, root / "platform"
        self.loop = asyncio.new_event_loop()
        server = usbip_uvc.Server([usbip_uvc.PROFILES["cvbs-ms2106"]], "test", root / "state", log=lambda m: None)
        (root / "state").mkdir()
        self.srv = self.loop.run_until_complete(asyncio.start_server(server.handle, "127.0.0.1", 0))
        self.port = self.srv.sockets[0].getsockname()[1]
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.run_dir = root / "run"
        self.run_dir.mkdir()

    def tearDown(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        usbip_attach.VHCI = self.orig_vhci
        self.tmp.cleanup()

    def test_start_stop(self):
        conf = {"SHCAM_HOST": "127.0.0.1", "SHCAM_USBIP_PORT": str(self.port), "LAB_NET": "127.0.0.0/8"}
        helper.usbip_start("cvbs", conf, self.run_dir)
        vport, fd, devid, speed = (self.ctl / "attach").read_text().split()
        self.assertEqual((vport, devid, speed), ("0", str(1 << 16 | 2), str(usbip_uvc.USB_SPEED_HIGH)))
        self.assertEqual((self.run_dir / "port").read_text().strip(), "0")
        (self.ctl / "status").write_text("hub port sta spd dev      sockfd local_busid\n"
                                         "hs  0000 006 003 00010002 000003 5-1\n")
        helper.usbip_stop(self.run_dir)
        self.assertEqual((self.ctl / "detach").read_text(), "0")
        self.assertFalse((self.run_dir / "port").exists())
        helper.usbip_stop(self.run_dir)

    def test_stop_after_remote_unplug_is_quiet(self):
        (self.run_dir / "port").write_text("1\n")                       # порт уже свободен (shcam «выдернул»)
        helper.usbip_stop(self.run_dir)
        self.assertFalse((self.run_dir / "port").exists())
        self.assertFalse((self.ctl / "detach").exists())


class LabVivid(unittest.TestCase):
    @staticmethod
    def bt(w, h, pclk, hfp, hs, hbp, vfp, vs, vbp, il=0, ivfp=0, ivs=0, ivbp=0):
        return struct.pack("<I", 0) + struct.pack("<IIIIQIIIIIIIII", w, h, il, 0, pclk, hfp, hs, hbp, vfp, vs, vbp,
                                                  ivfp, ivs, ivbp).ljust(128, b"\0")

    def test_bt_fps(self):
        w, h, f, il = labvivid.bt_fps(self.bt(1280, 720, 74_250_000, 1760, 40, 220, 5, 5, 20))   # CEA 720p30
        self.assertEqual((w, h, round(f, 2), il), (1280, 720, 30.0, False))
        w, h, f, il = labvivid.bt_fps(self.bt(1920, 1080, 74_250_000, 528, 44, 148, 2, 5, 15, 1, 2, 5, 16))
        self.assertEqual((w, h, round(f, 2), il), (1920, 1080, 25.0, True))                     # 1080i50: кадров 25

    def test_control_names_match_v4l2_ctl(self):
        self.assertEqual(labvivid.norm("S-Video 000-0 Is Connected To"), "s_video_000_0_is_connected_to")
        self.assertEqual(labvivid.norm("DV Timings Signal Mode"), "dv_timings_signal_mode")

    def test_signal_table_matches_contract(self):
        self.assertEqual(labvivid.SIGNAL["nosignal"], (1, 2))
        self.assertEqual(labvivid.SIGNAL["cable-out"], (0, 1))
        self.assertEqual(labvivid.STD, {"pal": 0xFF, "ntsc": 0xB000})

    def test_rejects_unknown_arguments(self):
        with self.assertRaises(SystemExit):
            labvivid.main(["hdmi", "pal"])
        with self.assertRaises(SystemExit):
            labvivid.set_signal("cvbs", "outofrange")


if __name__ == "__main__":
    unittest.main()
