# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""QA video-ingest: средство аппаратной приёмки захватов tools/probe/video_hw_probe.py (замена v4l2-ctl на плате).

Дерево sysfs и ioctl — tests/system/fake_v4l2.py (узлы Pi 4 сняты с плат); сверх него — аналоговый узел на USB
(usbtv, входы и ТВ-стандарты), чтобы проверить разбор структур v4l2_input/v4l2_standard и ENOTTY без устройства.
"""
import errno
import json
import os
import struct
import sys
from pathlib import Path

import pytest

from fake_v4l2 import FakeV4L2
from sheregesh import videoinput as vi

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/probe"))
import video_hw_probe as hp  # noqa: E402

PAL = 0x000000FF       # V4L2_STD_PAL
NTSC = 0x0000B000      # V4L2_STD_NTSC


class AnalogFake(FakeV4L2):
    """Открывать разрешено узлы на USB с любым драйвером; узлы платформы по-прежнему не открываются никогда."""

    def open(self, path, flags):
        d = self.devices.get(os.path.basename(path))
        if d is not None and d["driver"] not in hp.PLATFORM_DRIVERS and d["driver"] != "uvcvideo":
            fd = os.open(path, os.O_RDONLY)
            self.fds[fd] = os.path.basename(path)
            self.opened.append(os.path.basename(path))
            return fd
        return super().open(path, flags)

    def ioctl(self, fd, req, buf):
        d = self.devices.get(self.fds.get(fd))
        if d is not None and d.get("no_framesizes") and req in (vi.VIDIOC_ENUM_FRAMESIZES, vi.VIDIOC_ENUM_FRAMEINTERVALS):
            raise OSError(errno.ENOTTY, "Inappropriate ioctl")
        if d is not None and "inputs" in d:
            if req == hp.VIDIOC_ENUMINPUT:
                index = hp.INPUT.unpack(buf)[0]
                if index >= len(d["inputs"]):
                    raise OSError(errno.EINVAL, "Invalid argument")
                return hp.INPUT.pack(index, d["inputs"][index].encode(), 2, 0, 0, PAL | NTSC, 0, 0)
            if req == hp.VIDIOC_ENUMSTD:
                index = hp.STANDARD.unpack(buf)[0]
                stds = [(PAL, b"PAL", 1, 25), (NTSC, b"NTSC", 1001, 30000)]
                if index >= len(stds):
                    raise OSError(errno.EINVAL, "Invalid argument")
                sid, name, n, den = stds[index]
                return hp.STANDARD.pack(index, sid, name, n, den, 625 if sid == PAL else 525)
            if req == hp.VIDIOC_G_INPUT:
                return struct.pack("<i", 0)
            if req == hp.VIDIOC_G_STD:
                return struct.pack("<Q", PAL)
        return super().ioctl(fd, req, buf)


def kw(fake):
    return {"sys_root": fake.sys, "dev_root": fake.dev, "ioctl": fake.ioctl, "opener": fake.open}


def test_struct_sizes_and_request_numbers_match_videodev2():
    assert (hp.CAPABILITY.size, hp.FMTDESC.size, hp.FRMSIZE.size, hp.FRMIVAL.size) == (104, 64, 44, 52)
    assert (hp.INPUT.size, hp.STANDARD.size) == (80, 72)
    assert (hp.VIDIOC_QUERYCAP, hp.VIDIOC_ENUM_FMT) == (vi.VIDIOC_QUERYCAP, vi.VIDIOC_ENUM_FMT)
    assert (hp.VIDIOC_ENUM_FRAMESIZES, hp.VIDIOC_ENUM_FRAMEINTERVALS) == (vi.VIDIOC_ENUM_FRAMESIZES,
                                                                          vi.VIDIOC_ENUM_FRAMEINTERVALS)
    assert hp.VIDIOC_ENUMINPUT == 0xC050561A and hp.VIDIOC_ENUMSTD == 0xC0485619
    assert hp.VIDIOC_G_STD == 0x80085617 and hp.VIDIOC_G_INPUT == 0x80045626


def test_pi4_with_hdmi_adapter_platform_nodes_never_opened(tmp_path):
    fake = AnalogFake(tmp_path).pi4(with_uvc=True)
    nodes = {n["node"]: n for n in hp.video_nodes(**kw(fake))}
    assert fake.aux_opened == [] and set(fake.opened) == {"video0", "video1"}
    assert all("skipped" in n for name, n in nodes.items() if name not in ("video0", "video1"))
    v0 = nodes["video0"]
    assert v0["usb_id"] == "534d:2109" and v0["driver"] == "uvcvideo" and v0["video_capture"]
    mjpg = next(f for f in v0["formats"] if f["fourcc"] == "MJPG")
    assert {"size": "1280x720", "rates": ["30/1", "60/1"], "rates_error": None} in mjpg["sizes"]
    assert nodes["video1"]["meta"] and not nodes["video1"]["video_capture"] and "formats" not in nodes["video1"]


def test_no_probe_opens_nothing(tmp_path):
    fake = AnalogFake(tmp_path).pi4(with_uvc=True)
    nodes = hp.video_nodes(probe=False, **kw(fake))
    assert fake.opened == [] and len(nodes) == 16 and nodes[0]["usb_id"] == "534d:2109"


def _analog(fake, **extra):
    fake.add("video0", "usbtv", "usbtv", 0, ("1b71", "3002", ""),
             formats=[("YUYV", "discrete", [((720, 576), [25]), ((720, 480), [(1001, 30000)])])])
    fake.devices["video0"]["inputs"] = ["Composite", "S-Video"]
    fake.devices["video0"].update(extra)
    return fake


def test_analog_capture_on_usb_is_probed_with_inputs_and_standards(tmp_path):
    fake = _analog(AnalogFake(tmp_path))
    n = hp.video_nodes(**kw(fake))[0]
    assert n["driver"] == "usbtv" and n["usb_id"] == "1b71:3002" and n["video_capture"]
    sizes = {s["size"]: s["rates"] for s in n["formats"][0]["sizes"]}
    assert sizes == {"720x576": ["25/1"], "720x480": ["30000/1001"]}
    assert [i["name"] for i in n["inputs"]] == ["Composite", "S-Video"] and n["inputs_error"] is None
    assert n["standards"] == ["PAL 25/1", "NTSC 30000/1001"] and n["current_std"] == f"0x{PAL:x}"
    assert n["current_input"] == 0


def test_driver_without_framesize_enumeration_is_reported_not_fatal(tmp_path):
    fake = _analog(AnalogFake(tmp_path), no_framesizes=True)
    n = hp.video_nodes(**kw(fake))[0]
    assert n["formats"][0]["fourcc"] == "YUYV" and n["formats"][0]["sizes"] == []
    assert n["formats"][0]["sizes_error"] == "ENOTTY" and "error" not in n


def test_open_failure_is_recorded(tmp_path):
    fake = AnalogFake(tmp_path).pi4(with_uvc=True)
    fake.deny.add("video0")
    n = {x["node"]: x for x in hp.video_nodes(**kw(fake))}["video0"]
    assert n["error"].startswith("open:") and "formats" not in n


def test_text_output_lists_formats_inputs_and_skips(tmp_path, capsys):
    fake = _analog(AnalogFake(tmp_path))
    fake.add("video10", "bcm2835-codec", "bcm2835-codec-decode", 0, caps=0x4000)
    hp._print_nodes(hp.video_nodes(**kw(fake)))
    out = capsys.readouterr().out
    assert "usbtv" in out and "720x576 @ 25/1" in out and "Composite" in out and "PAL 25/1" in out
    assert "platform node, never opened" in out


def _usb(root: Path, port, vid, pid, product, speed, ifaces, cls="00"):
    d = root / "sys" / "bus" / "usb" / "devices" / port
    d.mkdir(parents=True)
    for k, v in (("idVendor", vid), ("idProduct", pid), ("product", product), ("speed", speed), ("bDeviceClass", cls)):
        (d / k).write_text(v + "\n")
    for i, (icls, drv) in enumerate(ifaces):
        f = d / f"{port}:1.{i}"
        f.mkdir()
        (f / "bInterfaceClass").write_text(icls + "\n")
        if drv:
            target = root / "sys" / "bus" / "usb" / "drivers" / drv
            target.mkdir(parents=True, exist_ok=True)
            (f / "driver").symlink_to(target)


def test_usb_inventory_hypothesis_and_unbound_interfaces(tmp_path, capsys):
    _usb(tmp_path, "1-1", "2109", "3431", "USB2.0 Hub", "480", [("09", "hub")], cls="09")
    _usb(tmp_path, "1-1.2", "534d", "2109", "USB Video", "480", [("0e", "uvcvideo"), ("0e", "uvcvideo"),
                                                                  ("01", "snd-usb-audio")])
    _usb(tmp_path, "1-1.3", "1c88", "003c", "", "480", [("ff", "")])
    _usb(tmp_path, "1-1.4", "abcd", "0001", "Something", "12", [("03", "usbhid")])
    devs = {d["usb_id"]: d for d in hp.usb_devices(tmp_path / "sys")}
    assert "2109:3431" not in devs                                   # концентратор не перечисляется
    assert devs["534d:2109"]["hypothesis"]["expected_driver"] == "uvcvideo"
    assert devs["1c88:003c"]["interfaces"]["1-1.3:1.0"]["driver"] == ""
    assert devs["abcd:0001"]["hypothesis"] is None
    hp._print_usb(list(devs.values()))
    out = capsys.readouterr().out
    assert "(нет драйвера)" in out and "гипотеза по VID:PID: MacroSilicon MS2109" in out


def test_classify_segments():
    frames = [(0.1 * i, 120.0, 30.0, 5.0) for i in range(1, 11)]          # live до 1,0 с
    frames += [(1.05, 120.0, 30.0, 0.0)]                                  # одиночный повтор — сцена, не «frozen»
    frames += [(2.0 + 0.1 * i, 16.0, 0.5, 0.0) for i in range(5)]         # пауза ~1 с, затем равномерное поле
    frames += [(2.5 + 0.1 * i, 90.0, 25.0, 0.0) for i in range(5)]        # кадры идут, но тождественны
    segs = hp.classify(frames, t_end=4.5)
    assert [s["state"] for s in segs] == ["live", "no-frames", "blank", "frozen", "no-frames"]
    assert segs[1] == {"state": "no-frames", "from_s": 1.05, "to_s": 2.0}


GEN = r"""
import os, sys, time
out = sys.stdout.buffer
W = 64 * 36
for i in range(25):                                  # «живые» кадры 50 к/с
    out.write(bytes((i * 7 + j * 13) % 256 for j in range(W))); out.flush(); time.sleep(0.02)
time.sleep(0.8)                                       # сигнал пропал, кадров нет
for i in range(20):                                   # равномерное поле («нет сигнала» синим/чёрным)
    out.write(bytes([16]) * W); out.flush(); time.sleep(0.02)
frame = bytes((j * 5) % 256 for j in range(W))
for i in range(20):                                   # застывший кадр
    out.write(frame); out.flush(); time.sleep(0.02)
"""


def test_frames_with_substituted_pipeline_detects_live_gap_blank_frozen(tmp_path, capsys):
    gen = tmp_path / "gen.py"
    gen.write_text(GEN)
    rc = hp.main(["--json", "frames", "--device", "/dev/null", "--seconds", "4", "--script", "0.3:снять сигнал",
                  "--command", sys.executable, str(gen)])
    res = json.loads(capsys.readouterr().out)
    assert rc == 0 and res["frames"] == 65 and res["exit_code"] == 0
    states = [s["state"] for s in res["segments"]]
    # первый застывший кадр отличается от поля, поэтому между blank и frozen бывает один кадр «live»
    assert states[:3] == ["live", "no-frames", "blank"] and "frozen" in states[3:5]
    assert res["interval_ms"]["max"] >= 700 and res["marks"][0]["action"] == "снять сигнал"
    assert 0.25 <= res["marks"][0]["t_s"] < 1.0 and len(res["timeline"]) == 65


def test_frames_failed_pipeline_returns_error(tmp_path, capsys):
    bad = tmp_path / "bad.py"
    bad.write_text("import sys; sys.stderr.write('ERROR: from element /GstPipeline:pipeline0/GstV4l2Src: Cannot "
                   "identify device'); sys.exit(1)")
    rc = hp.main(["frames", "--device", "/dev/null", "--seconds", "2", "--command", sys.executable, str(bad)])
    out = capsys.readouterr().out
    assert rc == 1 and "кадров 0" in out and "Cannot identify device" in out


def test_gst_command_shape():
    cmd = hp.gst_command("/dev/video0", "image/jpeg,width=1280,height=720,framerate=30/1")
    assert cmd[:4] == ["gst-launch-1.0", "-q", "v4l2src", "device=/dev/video0"]
    assert "image/jpeg,width=1280,height=720,framerate=30/1" in cmd and cmd[-3:] == ["fdsink", "fd=1", "sync=false"]
    assert "video/x-raw,format=GRAY8,width=64,height=36" in cmd
    assert hp.parse_script("25:вернуть, 10:снять") == [(10.0, "снять"), (25.0, "вернуть")]


@pytest.mark.parametrize("usb_id", ["534d:2109", "345f:2130", "1b71:3002", "05e1:0408", "1c88:003c", "eb1a:2861"])
def test_known_ids_documented_in_compat_list(usb_id):
    text = (ROOT / "docs/hardware/VIDEO_CAPTURE_COMPAT.md").read_text(encoding="utf-8")
    assert usb_id in text
