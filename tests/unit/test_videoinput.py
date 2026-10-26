# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S3.3 Universal Video Input: обнаружение устройств V4L2/UVC и конвейеры источников (L2, без устройств и GStreamer).

sysfs, /dev и ioctl — поддельные (tests/system/fake_v4l2.py), структуры videodev2.h — настоящие.
"""
import subprocess

import pytest

from fake_v4l2 import FakeV4L2
from sheregesh import video as gst
from sheregesh import videoinput as vi


@pytest.fixture
def v4l2(tmp_path):
    return FakeV4L2(tmp_path)


def test_ioctl_request_numbers_match_videodev2():
    """Номера запросов не зависят от разрядности (в структурах нет указателей): одинаковы на arm64 и x86_64."""
    assert (vi.VIDIOC_QUERYCAP, vi.VIDIOC_ENUM_FMT, vi.VIDIOC_ENUM_FRAMESIZES, vi.VIDIOC_ENUM_FRAMEINTERVALS) == \
        (0x80685600, 0xC0405602, 0xC02C564A, 0xC034564B)
    assert (vi.CAPABILITY.size, vi.FMTDESC.size, vi.FRMSIZE.size, vi.FRMIVAL.size) == (104, 64, 44, 52)
    assert vi.fourcc_str(vi.fourcc("MJPG")) == "MJPG"


def test_capture_device_distinguished_from_service_nodes(v4l2):
    nodes = {n.name: n for n in vi.scan(**v4l2.pi4().scan_kw())}
    assert [n.name for n in nodes.values() if n.capture] == ["video0"]
    cap = nodes["video0"]
    assert cap.driver == "uvcvideo" and cap.usb_id == "534d:2109" and cap.card == "USB Video: USB Video"
    assert cap.dev.endswith("/v4l/by-id/usb-MACROSILICON_USB_Video-video-index0")     # переживает переподключение
    assert "metadata" in nodes["video1"].reason
    for n in ("video10", "video11", "video12", "video13", "video19"):
        assert not nodes[n].capture and "auxiliary" in nodes[n].reason, n
    assert "video10" not in v4l2.opened and "video19" not in v4l2.opened   # служебные узлы даже не открываются


def test_modes_parsed_from_real_structures(v4l2):
    cap = vi.capture_devices(**v4l2.pi4().scan_kw())[0]
    assert vi.Mode(1920, 1080, 60, "MJPG") in cap.modes and vi.Mode(1280, 720, 10, "YUYV") in cap.modes
    assert len(cap.modes) == 10                                           # MJPEG 6 + YUYV 4


def test_stepwise_sizes_and_continuous_intervals(v4l2):
    v4l2.add("video0", "uvcvideo", "Stepwise cam", 0, ("1234", "5678", "SN 1/A"),
             formats=[("YUYV", "stepwise", [((320, 1920, 16, 240, 1080, 8), None)]), ("NV12", "discrete", [])])
    cap = vi.capture_devices(**v4l2.scan_kw())[0]
    assert cap.usb_id == "1234:5678:SN1A"                                  # серийный номер очищен для конфигурации
    assert {(m.width, m.height, m.fps) for m in cap.modes} == {(w, h, r) for w, h in vi._SIZES for r in (30, 60)}


def test_scan_without_probe_opens_nothing(v4l2):
    nodes = vi.scan(probe=False, **v4l2.pi4().scan_kw())
    assert [n.name for n in nodes if n.capture] == ["video0"] and v4l2.opened == []


def test_no_sysfs_and_no_permission(v4l2, tmp_path):
    assert vi.scan(sys_root=tmp_path / "absent", dev_root=tmp_path) == []
    v4l2.pi4().deny.add("video0")
    node = vi.scan(**v4l2.scan_kw())[0]
    assert not node.capture and "cannot open" in node.reason
    src = vi.open_uvc(**v4l2.scan_kw())
    assert not src.available and src.missing


def test_open_uvc_selects_device_and_exact_mode(v4l2):
    src = vi.open_uvc("", "1280x720@60", "auto", **v4l2.pi4().scan_kw())
    assert src.available and src.kind == "uvc" and src.id == "uvc:534d:2109"
    assert src.mode == vi.Mode(1280, 720, 60, "MJPG")
    assert vi.open_uvc("", "640x480@30", "auto", **v4l2.scan_kw()).mode.pixfmt == "YUYV"   # без декодирования
    assert vi.open_uvc("", "640x480@30", "mjpeg", **v4l2.scan_kw()).mode.pixfmt == "MJPG"
    assert vi.open_uvc("534d:2109", **v4l2.scan_kw()).available
    assert vi.open_uvc("video0", **v4l2.scan_kw()).available
    info = src.info()
    assert info["format"] == "mjpeg" and info["resolution"] == "1280x720" and info["fps"] == 60 and info["available"]


def test_device_absent_or_other_device(v4l2):
    src = vi.open_uvc(**v4l2.pi4(with_uvc=False).scan_kw())
    assert not src.available and src.missing and "not found" in src.error
    assert vi.open_uvc("046d:0825", **FakeV4L2(v4l2.root / "x").pi4().scan_kw()).missing


def test_unsupported_format_reports_what_device_offers(v4l2):
    src = vi.open_uvc("", "1920x1080@60", "yuyv", **v4l2.pi4().scan_kw())
    assert not src.available and not src.missing
    assert "YUYV 1920×1080@60 not supported" in src.error and "MJPEG 1920×1080@60" in src.error
    h264 = vi.open_uvc("", "1280x720@30", "h264", **v4l2.scan_kw())
    assert not h264.available and "H264" in h264.error


def test_several_devices_require_explicit_choice(v4l2):
    v4l2.pi4()
    v4l2.add("video2", "uvcvideo", "HD Webcam", 0, ("046d", "0825", "ABC"), formats=[("MJPG", "discrete", [((1280, 720), [30])])])
    src = vi.open_uvc(**v4l2.scan_kw())
    assert not src.available and "several UVC devices" in src.error
    assert vi.open_uvc("046d:0825", **v4l2.scan_kw()).name == "HD Webcam"
    assert vi.open_uvc("046d:0825:ABC", **v4l2.scan_kw()).available


def test_unplug_and_replug(v4l2):
    v4l2.pi4()
    assert vi.open_uvc(**v4l2.scan_kw()).available
    v4l2.remove("video0")
    v4l2.remove("video1")
    assert vi.open_uvc(**v4l2.scan_kw()).missing
    v4l2.add("video2", "uvcvideo", "USB Video: USB Video", 0, ("534d", "2109", ""), formats=[("MJPG", "discrete", [((1280, 720), [30])])],
             by_id="usb-MACROSILICON_USB_Video-video-index0")
    src = vi.open_uvc(**v4l2.scan_kw())
    assert src.available and src.node.name == "video2" and src.node.dev.endswith("video-index0")


def test_vanishing_device_during_probe_is_not_fatal(v4l2):
    v4l2.pi4()
    real_ioctl = v4l2.ioctl

    def flaky(fd, req, buf):
        if req == vi.VIDIOC_ENUM_FRAMEINTERVALS:
            raise OSError(19, "No such device")                         # ENODEV: отключили во время опроса
        return real_ioctl(fd, req, buf)
    node = vi.scan(**{**v4l2.scan_kw(), "ioctl": flaky})[0]
    assert not node.capture and "V4L2 query failed" in node.reason


# ---------------- конвейеры (sheregesh.video) ----------------
@pytest.fixture
def elements(monkeypatch):
    monkeypatch.setattr(gst, "has_element", lambda name: True)
    monkeypatch.setattr(gst, "h264_elements", lambda: (gst.V4L2_ENC, "v4l2h264dec"))


def test_synthetic_pipeline_unchanged(elements):
    """Регрессия: синтетический источник даёт прежний конвейер (до S3.3); в аппаратный кодер H.264 — без videoconvert."""
    gen = [gst.GST_LAUNCH, "-q", "videotestsrc", "is-live=true", "pattern=smpte", "!",
              "video/x-raw,width=1280,height=720,framerate=30/1", "!",
              "timeoverlay", "halignment=right", "valignment=bottom", "font-desc=Sans 28", "!",
              "textoverlay", "text=SHEREGESH SYNTHETIC TEST PATTERN", "valignment=top", "font-desc=Sans 24", "!"]
    before = gen + ["videoconvert", "!", "video/x-raw,format=I420", "!"]
    assert gst.stream_sender_args("jpeg") == before + gst.ENCODERS["jpeg"] + ["!", "rtpstreampay", "!", "fdsink",
                                                                               "fd=1", "sync=false"]
    assert gst.sender_args("h264", 5600) == gen + gst.V4L2_ENC + gst.RTP_H264 + \
        ["!", "udpsink", "host=127.0.0.1", "port=5600", "bind-address=127.0.0.1", "sync=false"]


def _src(v4l2, mode="1280x720@30", fmt="auto", formats=None):
    if formats:
        v4l2.add("video0", "uvcvideo", "Cam", 0, ("1234", "0001", ""), formats=formats)
    else:
        v4l2.pi4()
    return vi.open_uvc("", mode, fmt, **v4l2.scan_kw())


def test_local_capture_decodes_device_format_without_encoding(elements, v4l2):
    args = gst.capture_args(_src(v4l2, fmt="mjpeg"), 640, 360)
    text = " ".join(args)
    assert args[2] == "v4l2src" and "image/jpeg,width=1280,height=720,framerate=30/1 ! jpegdec ! videoscale" in text
    assert "enc" not in text and "udp" not in text and "rtp" not in text
    assert args[-7:] == gst._to_rgbx(640, 360)[-7:]                          # общий хвост с приёмником потока


def test_yuyv_needs_no_decoder(elements, tmp_path):
    src = _src(FakeV4L2(tmp_path), "640x480@60")
    assert src.mode.pixfmt == "YUYV"
    assert "video/x-raw,format=YUY2,width=640,height=480,framerate=60/1 ! videoscale" in " ".join(gst.capture_args(src, 640, 360))


def test_h264_device_uses_selected_hardware_decoder(elements, v4l2):
    src = _src(v4l2, fmt="h264", formats=[("H264", "discrete", [((1920, 1080), [30])])], mode="1920x1080@30")
    assert "video/x-h264,stream-format=byte-stream,width=1920,height=1080,framerate=30/1 ! h264parse ! v4l2h264dec !" \
        in " ".join(gst.capture_args(src, 640, 360))


def test_passthrough_when_device_format_matches_channel_codec(elements, v4l2):
    mjpeg = _src(v4l2, fmt="mjpeg")
    text = " ".join(gst.stream_sender_args("jpeg", src=mjpeg))
    assert "image/jpeg,width=1280,height=720,framerate=30/1 ! rtpjpegpay ! rtpstreampay" in text
    assert "jpegdec" not in text and "jpegenc" not in text                  # без перекодирования
    trans = " ".join(gst.stream_sender_args("h264", src=mjpeg))               # иначе — декодирование и кодер
    assert "jpegdec ! videoconvert ! video/x-raw,format=I420 ! v4l2h264enc" in trans


def test_h264_passthrough(elements, tmp_path):
    src = _src(FakeV4L2(tmp_path), fmt="h264", formats=[("H264", "discrete", [((1280, 720), [30])])])
    text = " ".join(gst.sender_args("h264", 5600, src=src))
    assert "h264parse config-interval=-1 ! video/x-h264,alignment=au ! rtph264pay config-interval=1 pt=96 ! udpsink" in text
    assert "v4l2h264enc" not in text and "openh264enc" not in text


def test_synthetic_source_info():
    s = vi.SyntheticSource(1280, 720, 30)
    assert s.info()["kind"] == "synthetic" and s.available and s.decode() == []


# ---------------- настоящий GStreamer (CI, devbox): конвейеры согласуются и дают кадры ----------------
class _Emulated(vi.VideoSource):
    """Формат устройства UVC из videotestsrc (без устройства): проверка согласования capture_args/сквозной передачи."""
    available = True

    def __init__(self, pixfmt, n=5):
        self.mode, self.n = vi.Mode(320, 240, 30, pixfmt), n

    def capture(self):
        size = "width=320,height=240,framerate=30/1"
        enc = {"YUYV": ["videoconvert", "!", f"video/x-raw,format=YUY2,{size}", "!"],
               # MJPEG камер UVC — 4:2:0/4:2:2 (rtpjpegpay молча отбрасывает 4:4:4)
               "MJPG": ["videoconvert", "!", "video/x-raw,format=I420", "!", "jpegenc", "!", f"image/jpeg,{size}", "!"],
               }[self.mode.pixfmt]
        return ["videotestsrc", f"num-buffers={self.n}", "!", f"video/x-raw,{size}", "!", *enc]


needs_gst = pytest.mark.skipif(not (gst.available() and gst.has_element("jpegenc") and gst.has_element("jpegdec")),
                               reason="нет GStreamer с jpegenc/jpegdec")


@needs_gst
@pytest.mark.parametrize("pixfmt", ["YUYV", "MJPG"])
def test_capture_pipeline_runs_in_gstreamer(pixfmt):
    r = subprocess.run(gst.capture_args(_Emulated(pixfmt), 160, 90), capture_output=True, timeout=30)
    assert r.returncode == 0 and len(r.stdout) == 5 * 160 * 90 * 4, r.stderr[-500:]


@needs_gst
def test_mjpeg_passthrough_pipeline_runs_in_gstreamer():
    r = subprocess.run(gst.stream_sender_args("jpeg", src=_Emulated("MJPG")), capture_output=True, timeout=30)
    assert r.returncode == 0 and len(r.stdout) > 5 * 1000, r.stderr[-500:]
    assert r.stdout[2] >> 6 == 2                                         # RFC 4571: длина, затем RTP версии 2
