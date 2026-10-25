# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Видеоприём блока: источники V4L2 любого драйвера (аналоговые usbtv/stk1160, HDMI-захваты, камеры UVC) и RTSP
(L2, без устройств и GStreamer). sysfs, /dev и ioctl — поддельные (tests/system/fake_v4l2.py), структуры videodev2.h —
настоящие, включая v4l2_input/v4l2_standard (VIDIOC_ENUMINPUT/ENUMSTD/G_STD/QUERYSTD)."""
import socket

import pytest

from fake_v4l2 import MJPEG_HDMI, NTSC_STD, PAL_STD, FakeV4L2, add_usbtv
from sheregesh import video as gst
from sheregesh import videoinput as vi


@pytest.fixture
def v4l2(tmp_path):
    return FakeV4L2(tmp_path)


@pytest.fixture
def elements(monkeypatch):
    monkeypatch.setattr(gst, "has_element", lambda name: True)
    monkeypatch.setattr(gst, "h264_elements", lambda: (gst.V4L2_ENC, "v4l2h264dec"))


def test_new_ioctl_numbers_and_struct_sizes_match_videodev2():
    """v4l2_input и v4l2_standard содержат __u64: размеры 80 и 72 на arm64 и x86_64 (выравнивание 8)."""
    assert (vi.INPUT.size, vi.STANDARD.size) == (80, 72)
    assert (vi.VIDIOC_ENUMINPUT, vi.VIDIOC_ENUMSTD, vi.VIDIOC_G_INPUT, vi.VIDIOC_G_STD, vi.VIDIOC_QUERYSTD) == \
        (0xC050561A, 0xC0485619, 0x80045626, 0x80085617, 0x8008563F)
    assert vi.std_family(vi.V4L2_STD_PAL) == "PAL" and vi.std_family(vi.V4L2_STD_NTSC) == "NTSC"
    assert vi.std_family(0) == "" and vi.std_family(0x00FF0000) == "PAL"          # SECAM — 625/50
    assert vi.parse_mode("720x480@29.97") == (720, 480, 30)


# ---------------- аналоговый захват (CVBS) ----------------
def test_usbtv_pal_by_stable_id(v4l2):
    add_usbtv(v4l2.pi4(with_uvc=False))
    node = next(n for n in vi.scan(**v4l2.scan_kw()) if n.capture)
    assert node.driver == "usbtv" and node.usb_id == "1b71:3002" and node.input_type == "CVBS" and node.analog
    assert node.modes == [] and node.formats == ["YUYV"]                        # ENUM_FRAMESIZES нет (ENOTTY)
    assert [i["name"] for i in node.inputs] == ["Composite", "S-Video"] and node.input == 0
    assert node.dev.endswith("/v4l/by-id/usb-Fushicai_USBTV007_0001-video-index0")
    src = vi.open_v4l2("usb-Fushicai_USBTV007_0001-video-index0", **v4l2.scan_kw())
    assert src.available and src.kind == "v4l2" and src.id == "v4l2:1b71:3002"
    assert src.standard == "PAL" and src.mode == vi.Mode(720, 576, 25, "YUYV")  # QUERYSTD: PAL на входе
    cap = " ".join(src.capture())
    assert cap == f"v4l2src device={node.dev} norm=PAL do-timestamp=true ! " \
                  "video/x-raw,format=YUY2,width=720,height=576,framerate=25/1 !"
    info = src.info()
    assert info["input_type"] == "CVBS" and info["standard"] == "PAL" and info["resolution"] == "720x576"
    assert {str(m) for m in src.formats} == {"YUYV 720×576@25", "YUYV 720×480@30"}
    assert v4l2.aux_opened == []


def test_usbtv_ntsc_explicit_and_auto(v4l2):
    add_usbtv(v4l2, querystd=vi.V4L2_STD_NTSC)
    auto = vi.open_v4l2("1b71:3002", **v4l2.scan_kw())
    assert auto.standard == "NTSC" and auto.mode == vi.Mode(720, 480, 30, "YUYV", "30000/1001")
    assert "norm=NTSC" in auto.capture() and "framerate=30000/1001" in auto.capture()[-2]
    assert vi.open_v4l2("1b71:3002", "720x480@29.97", standard="ntsc", **v4l2.scan_kw()).available
    pal = vi.open_v4l2("1b71:3002", standard="pal", **v4l2.scan_kw())
    assert pal.standard == "PAL" and pal.mode.height == 576
    bad = vi.open_v4l2("1b71:3002", "720x576@25", standard="ntsc", **v4l2.scan_kw())   # режим ≠ стандарт
    assert not bad.available and "(NTSC) not supported" in bad.error and "720×480@30" in bad.error
    assert "SECAM" in vi.open_v4l2("1b71:3002", standard="secam", **v4l2.scan_kw()).error.upper()


def test_auto_standard_without_signal_falls_back_to_current_std(v4l2):
    add_usbtv(v4l2, querystd=None)                       # QUERYSTD: ENODATA (нет сигнала) → G_STD (PAL)
    assert vi.open_v4l2(**v4l2.scan_kw()).standard == "PAL"


def test_stk1160_uyvy(v4l2):
    v4l2.add("video0", "stk1160", "stk1160", 0, ("05e1", "0408", ""), formats=[("UYVY", "discrete", [])],
             inputs=[{"name": "Composite0", "capabilities": vi.V4L2_IN_CAP_STD}], standards=[PAL_STD, NTSC_STD],
             std=vi.V4L2_STD_NTSC, sizes_ioctl=False)
    src = vi.open_v4l2(**v4l2.scan_kw())
    assert src.input_type == "CVBS" and src.standard == "NTSC"
    assert "video/x-raw,format=UYVY,width=720,height=480,framerate=30000/1001" in src.capture()
    assert src.decode() == []                                                   # сырой формат: без декодера


def test_open_uvc_still_ignores_non_uvc_capture(v4l2):
    """Пульт (S3.3) выбирает только uvcvideo: аналоговый захват на pi1 не подхватывается молча."""
    add_usbtv(v4l2.pi4(with_uvc=False))
    assert vi.open_uvc(**v4l2.scan_kw()).missing


# ---------------- HDMI / USB / прочие V4L2 ----------------
def test_input_type_heuristics(v4l2):
    v4l2.pi4()                                                                   # MS2109 «USB Video» — HDMI по VID:PID
    v4l2.add("video2", "uvcvideo", "HD Pro Webcam C920", 0, ("046d", "082d", "AB12"), formats=MJPEG_HDMI)
    v4l2.add("video3", "unicam", "unicam-image", 0, formats=[("UYVY", "discrete", [((1920, 1080), [30])])],
             inputs=[{"name": "unicam", "capabilities": vi.V4L2_IN_CAP_DV_TIMINGS}], bus="platform:fe801000.csi")
    v4l2.add("video4", "vivid", "vivid", 0, formats=[("YUYV", "discrete", [((640, 480), [30])])], bus="platform:vivid-000")
    v4l2.add("video5", "uvcvideo", "AV TO USB2.0", 0, ("534d", "0021", ""), formats=MJPEG_HDMI)   # EasyCAP MS210x
    types = {n.name: n.input_type for n in vi.scan(**v4l2.scan_kw()) if n.capture}
    assert types == {"video0": "HDMI", "video2": "USB", "video3": "HDMI", "video4": "V4L2", "video5": "CVBS"}


def test_select_by_every_stable_identifier(v4l2):
    v4l2.pi4()
    v4l2.add("video2", "uvcvideo", "HD Pro Webcam C920", 0, ("046d", "082d", "AB12"), formats=MJPEG_HDMI,
             by_id="usb-046d_HD_Pro_Webcam_C920_AB12-video-index0", bus="usb-0000:01:00.0-1.3",
             by_path="platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.0-video-index0")
    kw = v4l2.scan_kw()
    for sel in ("usb-046d_HD_Pro_Webcam_C920_AB12-video-index0", str(v4l2.dev / "v4l/by-id/usb-046d_HD_Pro_Webcam_C920_AB12-video-index0"),
                "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.0-video-index0", "046d:082d", "046d:082d:AB12",
                "HD Pro Webcam C920", "usb-0000:01:00.0-1.3", "video2"):
        src = vi.open_v4l2(sel, "1280x720@30", **kw)
        assert src.available and src.node.name == "video2", sel
    assert "several V4L2 devices" in vi.open_v4l2("", **kw).error
    assert vi.open_v4l2("046d:0000", **kw).missing


def test_auto_mode_uses_real_device_modes(v4l2):
    v4l2.add("video0", "uvcvideo", "HDMI Capture", 0, ("1234", "5678", ""),
             formats=[("MJPG", "discrete", [((1024, 768), [60, 30]), ((800, 600), [60])])])
    src = vi.open_v4l2(**v4l2.scan_kw())
    assert src.available and src.mode == vi.Mode(1024, 768, 60, "MJPG")         # нестандартный размер — как есть
    assert vi.open_v4l2(mode="1024x768@30", **v4l2.scan_kw()).mode.fps == 30
    other = FakeV4L2(v4l2.root / "other").pi4()
    assert vi.open_v4l2("534d:2109", **other.scan_kw()).mode == vi.Mode(1280, 720, 30, "MJPG")


def test_m2m_and_vbi_nodes_are_not_capture(v4l2):
    v4l2.add("video5", "some-m2m", "scaler", 0, caps=vi.V4L2_CAP_VIDEO_M2M | vi.V4L2_CAP_VIDEO_CAPTURE)
    v4l2.add("video6", "em28xx", "em28xx vbi", 0, ("eb1a", "2861", ""), caps=0x10)     # V4L2_CAP_VBI_CAPTURE
    nodes = {n.name: n for n in vi.scan(**v4l2.scan_kw())}
    assert "memory-to-memory" in nodes["video5"].reason and not nodes["video5"].capture
    assert "no V4L2_CAP_VIDEO_CAPTURE" in nodes["video6"].reason
    assert vi.open_v4l2(**v4l2.scan_kw()).error == "V4L2 device not found"


# ---------------- сигнал и устройство ----------------
def test_signal_reported_by_driver_and_device_loss(v4l2):
    add_usbtv(v4l2)
    src = vi.open_v4l2(**v4l2.scan_kw())
    assert src.present() == (True, "") and src.signal() is None
    v4l2.devices["video0"]["inputs"][0]["status"] = vi.V4L2_IN_ST_NO_SIGNAL      # кабель CVBS вынут
    assert src.signal() == "no-signal"
    v4l2.devices["video0"]["inputs"][0]["status"] = vi.V4L2_IN_ST_NO_SYNC
    assert src.signal() == "no-signal"
    v4l2.devices["video0"]["inputs"][0]["status"] = vi.V4L2_IN_ST_NO_H_LOCK          # аналог: нет строчной синхр.
    assert src.signal() == "no-signal"
    v4l2.devices["video0"]["inputs"][0]["status"] = 0
    assert src.signal() is None
    v4l2.remove("video0")                                                        # USB вынут
    ok, why = src.present()
    assert not ok and "disappeared" in why and src.signal() == "no-device"


def test_signal_check_on_vanishing_fd(v4l2, tmp_path):
    add_usbtv(v4l2)
    src = vi.open_v4l2(**v4l2.scan_kw())

    def gone(name, req):
        if req == vi.VIDIOC_ENUMINPUT:
            raise OSError(19, "No such device")
    v4l2.hooks.append(gone)
    assert src.signal() == "no-device"


def test_uvc_without_input_status_is_unknown(v4l2):
    src = vi.open_v4l2(**v4l2.pi4().scan_kw())
    assert src.kind == "uvc" and src.signal() is None                            # решает поток кадров


# ---------------- RTSP ----------------
def test_rtsp_source_redacts_credentials_and_builds_pipeline():
    s = vi.RTSPSource("rtsp://admin:secret@192.168.77.50:8554/stream1", "h264", latency_ms=100, timeout_s=3)
    assert s.id == "rtsp:192.168.77.50:8554/stream1" and s.device == "rtsp://192.168.77.50:8554/stream1"
    assert "secret" not in str(s.info()) and s.info()["input_type"] == "RTSP" and s.info()["resolution"] == ""
    assert s.capture() == ["rtspsrc", "location=rtsp://admin:secret@192.168.77.50:8554/stream1", "protocols=tcp",
                           "latency=100", "tcp-timeout=3000000", "timeout=3000000", "!", "rtph264depay", "!"]
    assert vi.RTSPSource("rtsp://cam/x", "mjpeg").capture()[-2] == "rtpjpegdepay"
    assert vi.RTSPSource("rtsp://cam/x").port == 554


def test_rtsp_presence_is_a_plain_tcp_check():
    srv = socket.create_server(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    s = vi.RTSPSource(f"rtsp://127.0.0.1:{port}/live")
    assert s.present() == (True, "")
    srv.close()
    ok, why = s.present()
    assert not ok and f"RTSP server 127.0.0.1:{port} unreachable" in why


def test_rtsp_pipelines(elements):
    h264 = vi.RTSPSource("rtsp://cam/x")
    text = " ".join(gst.stream_sender_args("h264", src=h264))
    assert "rtph264depay ! h264parse config-interval=-1 ! video/x-h264,alignment=au ! rtph264pay" in text
    assert "v4l2h264enc" not in text                                             # без перекодирования
    trans = " ".join(gst.stream_sender_args("jpeg", src=h264))
    assert "rtph264depay ! h264parse ! v4l2h264dec ! videoconvert ! video/x-raw,format=I420 ! jpegenc" in trans
    mj = " ".join(gst.stream_sender_args("jpeg", src=vi.RTSPSource("rtsp://cam/x", "mjpeg")))
    assert "rtpjpegdepay ! rtpjpegpay ! rtpstreampay" in mj


# ---------------- описание конфигурации ----------------
def test_open_source_from_config(v4l2):
    add_usbtv(v4l2)
    kw = v4l2.scan_kw()
    assert vi.open_source({"source": "synthetic"}).kind == "synthetic"
    assert vi.open_source({}).kind == "synthetic"
    a = vi.open_source({"source": "v4l2", "device": "1b71:3002", "standard": "ntsc"}, **kw)
    assert a.available and a.standard == "NTSC"
    assert vi.open_source({"source": "uvc"}, **kw).missing                        # usbtv — не UVC
    r = vi.open_source({"source": "rtsp", "url": "rtsp://u:p@10.0.0.5/s", "codec": "mjpeg"})
    assert r.kind == "rtsp" and r.mode.pixfmt == "MJPG"
    bad = vi.open_source({"source": "rtsp", "url": "http://x/"})
    assert not bad.available and "expected rtsp://" in bad.error
    assert "unknown video source" in vi.open_source({"source": "dji"}).error
    assert "bad video input configuration" in vi.open_source({"source": "rtsp"}).error
