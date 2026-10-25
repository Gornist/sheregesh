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
import time
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
        self.assertEqual(helper.SLOTS, {"hdmi": ("1-1", "534d:2109"), "cvbs": ("1-2", "534d:0021"),
                                        "camera": ("1-3", "1d6b:0102")})
        with self.assertRaises(SystemExit):
            helper.main(["usbip", "start", "evil"])

    def test_bad_config_line(self):
        with tempfile.NamedTemporaryFile("w") as f:
            f.write("SHCAM_HOST 192.168.1.193\n")
            f.flush()
            with self.assertRaises(helper.Refused):
                helper.read_conf(f.name)


HEAD = "hub port sta spd dev      sockfd local_busid\n"
FREE = "hs  {p:04d} 004 000 00000000 000000 0-0\n"
USED = "hs  {p:04d} 006 003 {devid:08x} 000003 {busid}\n"


class FakeSysfs(unittest.TestCase):
    """Подменённые sysfs vhci (VHCI) и /sys/bus/usb/devices (USB_DEVICES) + настоящий эмулятор USB/IP."""
    PROFILES = (usbip_uvc.PROFILES["cvbs-ms2106"],)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ctl = root / "platform/vhci_hcd.0"
        self.ctl.mkdir(parents=True)
        self.status(FREE.format(p=0) + FREE.format(p=1))
        self.usb = root / "usb"
        self.usb.mkdir()
        self.orig = usbip_attach.VHCI, helper.USB_DEVICES
        usbip_attach.VHCI, helper.USB_DEVICES = root / "platform", self.usb
        (root / "state").mkdir()
        self.loop = asyncio.new_event_loop()
        server = usbip_uvc.Server(list(self.PROFILES), "test", root / "state", log=lambda m: None)
        self.srv = self.loop.run_until_complete(asyncio.start_server(server.handle, "127.0.0.1", 0))
        self.conf = {"SHCAM_HOST": "127.0.0.1", "SHCAM_USBIP_PORT": str(self.srv.sockets[0].getsockname()[1]),
                     "LAB_NET": "127.0.0.0/8"}
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.run_dir = root / "run"
        self.run_dir.mkdir()

    def tearDown(self):
        async def shutdown():                                           # закрыть сервер и обработчики соединений
            self.srv.close()
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(shutdown(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        usbip_attach.VHCI, helper.USB_DEVICES = self.orig
        self.tmp.cleanup()

    def status(self, rows):
        (self.ctl / "status").write_text(HEAD + rows)

    def device(self, busid, vidpid, classes=("0e", "0e")):
        d = self.usb / busid
        d.mkdir()
        vid, pid = vidpid.split(":")
        (d / "idVendor").write_text(vid + "\n")
        (d / "idProduct").write_text(pid + "\n")
        for n, c in enumerate(classes):
            i = self.usb / f"{busid}:1.{n}"
            i.mkdir()
            (i / "bInterfaceClass").write_text(c + "\n")


class HelperUsbipCycle(FakeSysfs):
    """start → OP_REQ_IMPORT к настоящему эмулятору → «порт fd devid скорость» в подменённый attach;
    stop → detach, только если на порту всё ещё наш devid (LS-2)."""
    DEVID = 1 << 16 | 2                                                   # busnum 1, devnum 2 (первый профиль)

    def test_start_stop(self):
        helper.usbip_start("cvbs", self.conf, self.run_dir, check=lambda *a: None)
        vport, _fd, devid, speed = (self.ctl / "attach").read_text().split()
        self.assertEqual((vport, devid, speed), ("0", str(self.DEVID), str(usbip_uvc.USB_SPEED_HIGH)))
        state = json.loads((self.run_dir / "state.json").read_text())
        self.assertEqual((state["vport"], state["devid"], state["expect"]), (0, self.DEVID, "534d:0021"))
        self.status(USED.format(p=0, devid=self.DEVID, busid="5-1"))
        helper.usbip_stop(self.run_dir)
        self.assertEqual((self.ctl / "detach").read_text(), "0")
        self.assertFalse((self.run_dir / "state.json").exists())
        helper.usbip_stop(self.run_dir)                                  # повторный stop — без ошибок

    def test_stop_after_remote_unplug_is_quiet(self):
        (self.run_dir / "state.json").write_text(json.dumps({"vport": 1, "devid": self.DEVID}))
        helper.usbip_stop(self.run_dir)                                  # порт 1 свободен
        self.assertFalse((self.run_dir / "state.json").exists())
        self.assertFalse((self.ctl / "detach").exists())

    def test_ls2_stop_does_not_detach_foreign_device_on_reused_port(self):
        # наш слот «выдернули» со стороны shcam, порт 0 занял другой слот (другой devid) — его не трогаем
        (self.run_dir / "state.json").write_text(json.dumps({"vport": 0, "devid": self.DEVID}))
        self.status(USED.format(p=0, devid=1 << 16 | 3, busid="5-1"))
        helper.usbip_stop(self.run_dir)
        self.assertFalse((self.ctl / "detach").exists())
        self.assertFalse((self.run_dir / "state.json").exists())

    def test_ls1_refuses_wrong_claimed_device_before_attach(self):
        with self.assertRaises(helper.Refused):                           # слот hdmi ждёт 534d:2109 в 1-1
            helper.usbip_start("hdmi", self.conf, self.run_dir, check=lambda *a: None)
        self.assertFalse((self.ctl / "attach").exists())
        self.assertFalse((self.run_dir / "state.json").exists())

    def test_ls1_failed_post_check_detaches(self):
        with self.assertRaises(helper.Refused):
            helper.usbip_start("cvbs", self.conf, self.run_dir, check=lambda *a: "класс 03")
        self.assertFalse((self.run_dir / "state.json").exists())


class HidPretender(FakeSysfs):
    """Подменённый сервер заявляет в 1-1 HID-приёмник: отказ до attach."""
    PROFILES = (usbip_uvc.Profile("evil", "1-1", 0x046D, 0xC52B, "Logitech", "USB Receiver",
                                  [(usbip_uvc.FMT_MJPEG, [usbip_uvc.Frame(640, 480, (30,))])], 0x1201),)

    def test_claimed_vidpid_mismatch(self):
        with self.assertRaises(helper.Refused) as e:
            helper.usbip_start("hdmi", self.conf, self.run_dir, check=lambda *a: None)
        self.assertIn("046d:c52b", str(e.exception))
        self.assertFalse((self.ctl / "attach").exists())


class CheckAttached(FakeSysfs):
    """LS-1 после attach: VID:PID и классы интерфейсов в sysfs на порту с нашим devid."""
    DEVID = 1 << 16 | 2

    def test_ok(self):
        self.status(USED.format(p=0, devid=self.DEVID, busid="5-1"))
        self.device("5-1", "534d:0021")
        self.assertIsNone(helper.check_attached(0, self.DEVID, "534d:0021", timeout=0.5))

    def test_wrong_vidpid(self):
        self.status(USED.format(p=0, devid=self.DEVID, busid="5-1"))
        self.device("5-1", "0781:5567")
        self.assertIn("0781:5567", helper.check_attached(0, self.DEVID, "534d:0021", timeout=0.5))

    def test_non_video_interface(self):
        self.status(USED.format(p=0, devid=self.DEVID, busid="5-1"))
        self.device("5-1", "534d:0021", classes=("0e", "03"))           # «видео» + HID
        self.assertIn("03", helper.check_attached(0, self.DEVID, "534d:0021", timeout=0.5))

    def test_other_device_on_port_times_out(self):
        self.status(USED.format(p=0, devid=1 << 16 | 9, busid="5-1"))
        self.device("5-1", "534d:0021")
        self.assertIn("не появилось", helper.check_attached(0, self.DEVID, "534d:0021", timeout=0.3))


class Ls3Lock(FakeSysfs):
    """LS-3: выбор свободного порта и attach — под flock; два одновременных start берут разные порты."""

    def test_concurrent_starts_get_distinct_ports(self):
        orig = usbip_attach.import_device
        taken = []

        def slow_import(host, port, busid, expect=None):              # читает свободный порт, «думает», занимает
            free = [p for _c, _h, p, st in usbip_attach.vhci_ports() if st == usbip_attach.VDEV_ST_NULL]
            time.sleep(0.2)
            vport = free[0]
            rows = "".join(USED.format(p=p, devid=1, busid="5-9") if p in taken + [vport] else FREE.format(p=p)
                           for p in (0, 1))
            self.status(rows)
            taken.append(vport)
            return {"vport": vport, "devid": 1, "vid": 0x534D, "pid": 0x0021, "speed": 3}

        usbip_attach.import_device = slow_import
        try:
            dirs = [self.run_dir / "a", self.run_dir / "b"]
            for d in dirs:
                d.mkdir()
            ts = [threading.Thread(target=helper.usbip_start, args=("cvbs", self.conf, d),
                                   kwargs={"check": lambda *a: None}) for d in dirs]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        finally:
            usbip_attach.import_device = orig
        self.assertEqual(sorted(taken), [0, 1])


class Hardening(unittest.TestCase):
    def test_ls1_udev_authorizes_only_video_on_vhci(self):
        rules = (PI2 / "etc/98-sheregesh-lab-usbip.rules").read_text()
        self.assertIn('ATTR{interface_authorized_default}="0"', rules)
        self.assertIn('DEVPATH=="/devices/platform/vhci_hcd.*/usb[0-9]*"', rules)
        authorize = [ln for ln in rules.splitlines() if 'ATTR{authorized}="1"' in ln]
        self.assertEqual(len(authorize), 1)
        self.assertIn('ATTR{bInterfaceClass}=="0e"', authorize[0])
        self.assertIn("drivers_probe", authorize[0])          # без повторного probe uvcvideo не привяжется

    def test_ls4_vivid_symlinks_require_driver(self):
        for line in (PI2 / "etc/99-sheregesh-lab-vivid.rules").read_text().splitlines():
            if "SYMLINK" in line:
                self.assertIn('DRIVERS=="vivid"', line)

    def test_ls4_node_requires_vivid_driver(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            for n, drv in (("video3", "uvcvideo"), ("video7", "vivid")):
                d = root / "class" / n
                (d / "device").mkdir(parents=True)
                (d / "name").write_text("vivid-000-vid-cap\n")
                (root / "drivers" / drv).mkdir(parents=True, exist_ok=True)
                (d / "device" / "driver").symlink_to(root / "drivers" / drv)
            self.assertEqual(labvivid.node("cvbs", "cap", sys_root=root / "class"), Path("/dev/video7"))

    def test_ls7_root_units_keep_only_sys_module(self):
        for name in ("sheregesh-lab-vivid.service", "sheregesh-lab-usbip@.service"):
            text = (PI2 / "units" / name).read_text()
            self.assertIn("CapabilityBoundingSet=CAP_SYS_MODULE\n", text, name)
            self.assertNotIn("ProtectKernelModules=yes", text, name)
        feed = (PI2 / "units/sheregesh-lab-vivid-feed@.service").read_text()
        self.assertIn("CapabilityBoundingSet=\n", feed)
        self.assertIn("User=sheregesh-ui", feed)


class Manifest(unittest.TestCase):
    """manifest.tsv — единый список для dev-слоя и verify_image: все источники есть, все файлы проекта учтены."""

    def rows(self):
        return [ln.split("\t") for ln in (PI2 / "manifest.tsv").read_text().splitlines()
                if ln and not ln.startswith("#")]

    def test_sources_exist_and_dests_sane(self):
        rows = self.rows()
        for src, dest, mode in rows:
            with self.subTest(src):
                self.assertTrue((LAB / src).is_file())
                self.assertTrue(dest.startswith(("/usr/lib/", "/etc/")))
                self.assertIn(mode, ("0644", "0755"))
                self.assertEqual(mode == "0755", src.startswith("pi2/bin/"))
        self.assertEqual(len({d for _s, d, _m in rows}), len(rows))

    def test_every_project_file_is_listed(self):
        listed = {src for src, _d, _m in self.rows()}
        files = {str(p.relative_to(LAB)) for d in ("bin", "etc", "units") for p in (PI2 / d).iterdir()}
        self.assertEqual(files - listed, set())

    def test_units_dev_only_and_rule_matches_units(self):
        for unit in (PI2 / "units").iterdir():
            self.assertIn("ConditionPathExists=/etc/sheregesh/DEV-MODE", unit.read_text(), unit.name)
        rule = (PI2 / "etc/51-sheregesh-lab-video.rules").read_text()
        templates = {u.name for u in (PI2 / "units").iterdir()}
        for name in __import__("re").findall(r'"(sheregesh-lab-[a-z-]+(?:@[a-z]+)?\.service)"', rule):
            base = name.split("@")[0] + "@.service" if "@" in name else name
            self.assertIn(base, templates, name)


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
