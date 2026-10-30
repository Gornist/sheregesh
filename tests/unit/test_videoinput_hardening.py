# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S3.3, укрепление (L2, без устройств): дерево узлов как на Pi 4, исчезновение устройства в разных фазах опроса,
выбор формата, ошибки open/ioctl, регрессия синтетического режима.

sysfs, /dev и ioctl — поддельные (tests/system/fake_v4l2.py), структуры videodev2.h — настоящие. Открытие служебного
узла в подделке — AssertionError, поэтому «не открывается» проверяется в каждом испытании, а не только здесь.
"""
import errno
import functools
import os

import pytest

from fake_v4l2 import PI4_SYSTEM_NODES, FakeV4L2
from sheregesh import video as gst
from sheregesh import videoinput as vi

PI4_NAMES = [f"video{n}" for n in (10, 11, 12, 13, 14, 15, 16, 18, 19, 20, 21, 22, 23, 31)]
ALL3 = [("YUYV", "discrete", [((1280, 720), [30])]), ("MJPG", "discrete", [((1280, 720), [30, 60])]),
        ("H264", "discrete", [((1280, 720), [30])])]


@pytest.fixture
def v4l2(tmp_path):
    return FakeV4L2(tmp_path)


def open_fds():
    return len(os.listdir("/dev/fd"))


# ---------------- 1. дерево Pi 4 ----------------
@pytest.mark.parametrize("probe", [True, False])
def test_pi4_system_nodes_exactly_as_on_board_are_auxiliary_and_never_opened(v4l2, probe):
    """14 узлов pi1/pi2 (bcm2835-codec ×5, bcm2835-isp ×8, rpi-hevc-dec): все «auxiliary», ни один не открыт."""
    nodes = vi.scan(probe=probe, **v4l2.pi4(with_uvc=False).scan_kw())
    assert [n.name for n in nodes] == PI4_NAMES                               # числовой, а не строковый порядок
    expect = {f"video{n}": (drv, name) for n, drv, name, _i, _c in PI4_SYSTEM_NODES}
    for n in nodes:
        assert (n.driver, n.card) == expect[n.name], n
        assert not n.capture and n.reason == f"auxiliary node (driver {n.driver})" and n.modes == [], n
        assert n.usb_id == "" and n.dev.endswith("/" + n.name)
    assert sorted({n.driver for n in nodes}) == ["bcm2835-codec", "bcm2835-isp", "rpi-hevc-dec"]
    assert v4l2.opened == [] and v4l2.aux_opened == []
    src = vi.open_uvc(**v4l2.scan_kw())
    assert src.missing and src.error == "UVC device not found"               # причины служебных узлов не примешаны
    assert v4l2.opened == [] and v4l2.aux_opened == []


def test_pi4_with_adapter_opens_only_uvc_nodes(v4l2):
    nodes = vi.scan(**v4l2.pi4().scan_kw())
    assert [n.name for n in nodes if n.capture] == ["video0"] and len(nodes) == 16
    assert v4l2.opened == ["video0", "video1"] and v4l2.aux_opened == []


def test_fake_tree_trips_if_auxiliary_node_is_opened(v4l2):
    """Подделка сама ловит открытие служебного узла (иначе испытания выше ничего бы не доказывали)."""
    v4l2.pi4(with_uvc=False)
    with pytest.raises(AssertionError, match="video19"):
        v4l2.open(str(v4l2.dev / "video19"), os.O_RDWR)


def test_diagnostic_cli_on_pi4_tree(v4l2, monkeypatch, capsys):
    monkeypatch.setattr(vi, "scan", functools.partial(vi.scan, **v4l2.pi4(with_uvc=False).scan_kw()))
    assert vi.main([]) == 0 and vi.main(["--no-probe"]) == 0
    lines = [x for x in capsys.readouterr().out.splitlines() if x.strip()]
    assert len(lines) == 28 and all(x.startswith("other") and "auxiliary" in x for x in lines)
    assert v4l2.aux_opened == []


def test_scan_survives_odd_sysfs_entries(v4l2):
    """Недописанные/исчезающие записи sysfs и посторонние имена не роняют обнаружение."""
    v4l2.pi4()
    base = v4l2.sys / "class" / "video4linux"
    (base / "v4l-subdev0").mkdir()                                            # не videoN — игнорируется
    (base / "vbi0").mkdir()
    (base / "video7").symlink_to(v4l2.sys / "nowhere" / "video7")             # битая ссылка: узел исчезает
    unbound = base / "video8"                                                 # драйвер отвязан: нет device/driver
    unbound.mkdir()
    (unbound / "name").write_text("orphan\n")
    (v4l2.devices["video0"]["real"] / "name").unlink()                        # имя не прочитать
    nodes = {n.name: n for n in vi.scan(**v4l2.scan_kw())}
    assert "v4l-subdev0" not in nodes and "vbi0" not in nodes
    assert "unknown" in nodes["video7"].reason and "unknown" in nodes["video8"].reason
    assert nodes["video0"].capture and nodes["video0"].card == "USB Video: USB Video"   # имя из QUERYCAP
    assert v4l2.aux_opened == [] and "video7" not in v4l2.opened and "video8" not in v4l2.opened


# ---------------- 2. исчезновение USB в разных фазах ----------------
def _vanish_at(v4l2, request):
    """Отключить адаптер при первом ioctl request (ядро отвечает ENODEV на открытый дескриптор)."""
    def hook(name, req):
        if req == request and "video0" in v4l2.devices:
            v4l2.remove("video0")
            v4l2.remove("video1")
    v4l2.hooks.append(hook)


def test_vanish_between_scan_and_open(v4l2):
    v4l2.pi4()
    assert vi.scan(probe=False, **v4l2.scan_kw())[0].capture                  # sysfs ещё видит узел

    def opener(path, flags):
        if "video0" in v4l2.devices:
            v4l2.remove("video0")
            v4l2.remove("video1")
        return v4l2.open(path, flags)
    fds = open_fds()
    src = vi.open_uvc(**{**v4l2.scan_kw(), "opener": opener})
    assert not src.available and src.missing and src.error.startswith("UVC device not found (video0: cannot open")
    assert open_fds() == fds


@pytest.mark.parametrize("request_", ["VIDIOC_QUERYCAP", "VIDIOC_ENUM_FMT", "VIDIOC_ENUM_FRAMESIZES"])
def test_vanish_during_query_is_reported_and_fd_closed(v4l2, request_):
    v4l2.pi4()
    _vanish_at(v4l2, getattr(vi, request_))
    fds = open_fds()
    src = vi.open_uvc(**v4l2.scan_kw())
    assert src.missing and "video0: V4L2 query failed: No such device" in src.error, src.error
    assert open_fds() == fds                                                  # дескриптор закрыт и при ошибке
    assert v4l2.aux_opened == []


def test_reappears_as_other_node_with_and_without_by_id(v4l2):
    v4l2.pi4()
    assert vi.open_uvc("534d:2109", **v4l2.scan_kw()).node.name == "video0"
    v4l2.remove("video0")
    v4l2.remove("video1")
    # video0 занят другой камерой, наш адаптер — video2/video3 с обновлённой ссылкой by-id
    v4l2.add("video0", "uvcvideo", "HD Webcam", 0, ("046d", "0825", "W1"), formats=ALL3)
    v4l2.add("video2", "uvcvideo", "USB Video: USB Video", 0, ("534d", "2109", ""), formats=ALL3,
             by_id="usb-MACROSILICON_USB_Video-video-index0")
    v4l2.add("video3", "uvcvideo", "USB Video: USB Video", 1, ("534d", "2109", ""), caps=vi.V4L2_CAP_META_CAPTURE)
    src = vi.open_uvc("534d:2109", **v4l2.scan_kw())
    assert src.available and src.node.name == "video2"                       # по vvvv:pppp, а не по старому номеру
    assert "device=" + str(v4l2.dev / "v4l" / "by-id" / "usb-MACROSILICON_USB_Video-video-index0") in src.capture()
    v4l2.remove("video2")
    v4l2.add("video5", "uvcvideo", "USB Video: USB Video", 0, ("534d", "2109", ""), formats=ALL3)   # без by-id
    src = vi.open_uvc("534d:2109", **v4l2.scan_kw())
    assert src.node.name == "video5" and f"device={v4l2.dev / 'video5'}" in src.capture()


# ---------------- 3. выбор формата ----------------
@pytest.mark.parametrize("fmt,code,caps", [
    ("auto", "YUYV", "video/x-raw,format=YUY2,width=1280,height=720,framerate=30/1"),
    ("yuyv", "YUYV", "video/x-raw,format=YUY2,width=1280,height=720,framerate=30/1"),
    ("mjpeg", "MJPG", "image/jpeg,width=1280,height=720,framerate=30/1"),
    ("h264", "H264", "video/x-h264,stream-format=byte-stream,width=1280,height=720,framerate=30/1")])
def test_format_choice_on_device_with_all_three(v4l2, fmt, code, caps):
    v4l2.add("video0", "uvcvideo", "Cam", 0, ("1234", "0001", ""), formats=ALL3)
    src = vi.open_uvc("", "1280x720@30", fmt, **v4l2.scan_kw())
    assert src.available and src.mode == vi.Mode(1280, 720, 30, code) and caps in src.capture()


@pytest.mark.parametrize("formats,code", [(ALL3[1:], "MJPG"), (ALL3[2:], "H264"), (ALL3[::2], "YUYV")])
def test_auto_order_without_decoding_first(v4l2, formats, code):
    v4l2.add("video0", "uvcvideo", "Cam", 0, ("1234", "0001", ""), formats=formats)
    assert vi.open_uvc("", "1280x720@30", "auto", **v4l2.scan_kw()).mode.pixfmt == code


@pytest.mark.parametrize("mode,fmt", [("1280x720@60", "yuyv"), ("1280x720@60", "h264"), ("1920x1080@30", "auto"),
                                      ("640x480@30", "mjpeg")])
def test_absent_mode_is_never_substituted(v4l2, mode, fmt):
    v4l2.add("video0", "uvcvideo", "Cam", 0, ("1234", "0001", ""), formats=ALL3)
    src = vi.open_uvc("", mode, fmt, **v4l2.scan_kw())
    assert not src.available and not src.missing and "not supported by Cam" in src.error
    assert src.formats == tuple(vi.capture_devices(**v4l2.scan_kw())[0].modes)    # перечень для экрана «Видео»


def test_only_unusable_formats(v4l2):
    v4l2.add("video0", "uvcvideo", "Cam", 0, ("1234", "0001", ""),
             formats=[("NV12", "discrete", [((1280, 720), [30])]), ("GREY", "discrete", [((640, 480), [30])])])
    src = vi.open_uvc(**v4l2.scan_kw())
    assert not src.available and src.error.endswith("offered: no YUYV/UYVY/MJPEG/H.264")


def test_stepwise_sizes_respect_step(v4l2):
    """Ступенчатый диапазон: размер = min + k·step. 640/1280/1920 не кратны шагу 100 от 600 — не предлагаются
    (раньше предлагались: v4l2src подогнал бы размер, caps не согласовались бы — вечный перезапуск вместо «unsupported»)."""
    v4l2.add("video0", "uvcvideo", "Odd", 0, ("1234", "0002", ""),
             formats=[("YUYV", "stepwise", [((600, 2000, 100, 400, 1200, 80), None)])])
    assert vi.capture_devices(**v4l2.scan_kw())[0].modes == []
    src = vi.open_uvc("", "640x480@30", "auto", **v4l2.scan_kw())
    assert not src.available and not src.missing


def test_continuous_sizes_ignore_step_and_intervals_bound_rates(v4l2):
    v4l2.add("video0", "uvcvideo", "Cont", 0, ("1234", "0003", ""),
             formats=[("MJPG", "continuous", [((16, 4096, 1, 16, 2160, 1), (2, 1, 30, 1, 1, 1, 1))])])
    modes = vi.capture_devices(**v4l2.scan_kw())[0].modes
    assert {(m.width, m.height, m.fps) for m in modes} == {(w, h, 30) for w, h in vi._SIZES}   # 60 к/с вне 1/30…1 с


def test_stepwise_intervals_within_range(v4l2):
    v4l2.add("video0", "uvcvideo", "Step", 0, ("1234", "0004", ""),
             formats=[("YUYV", "stepwise", [((640, 640, 1, 480, 480, 1), (3, 1, 60, 1, 15, 1, 60))])])
    modes = vi.capture_devices(**v4l2.scan_kw())[0].modes
    assert [(m.width, m.height, m.fps) for m in modes] == [(640, 480, 30), (640, 480, 60)]


def test_ntsc_interval_is_requested_exactly(v4l2):
    """Интервал 1001/30000 (29,97 к/с): caps v4l2src — ровно 30000/1001, иначе не согласуются (риск L4, исправлен)."""
    v4l2.add("video0", "uvcvideo", "NTSC", 0, ("1234", "0005", ""),
             formats=[("MJPG", "discrete", [((1280, 720), [(1001, 30000)])])])
    src = vi.open_uvc("", "1280x720@30", "auto", **v4l2.scan_kw())
    assert "framerate=30000/1001" in " ".join(src.capture())


def test_integer_rate_preferred_over_ntsc_when_both_offered(v4l2):
    v4l2.add("video0", "uvcvideo", "Both", 0, ("1234", "0006", ""),
             formats=[("MJPG", "discrete", [((1280, 720), [(1001, 30000), (1, 30)])])])
    src = vi.open_uvc("", "1280x720@30", "auto", **v4l2.scan_kw())
    caps = " ".join(src.capture())
    assert "framerate=30/1" in caps and "30000/1001" not in caps


def test_several_devices_and_identical_adapters(v4l2):
    v4l2.pi4(usb=("534d", "2109", "SER1"))
    v4l2.add("video2", "uvcvideo", "USB Video: USB Video", 0, ("534d", "2109", "SER2"), formats=ALL3,
             by_id="usb-MACROSILICON_USB_Video_SER2-video-index0")
    kw = v4l2.scan_kw()
    assert "several UVC devices" in vi.open_uvc(**kw).error
    assert "several UVC devices" in vi.open_uvc("534d:2109", **kw).error      # vvvv:pppp обоих — выбор не угадывается
    one, two = vi.open_uvc("534d:2109:SER1", **kw), vi.open_uvc("534d:2109:SER2", **kw)
    assert (one.node.name, two.node.name) == ("video0", "video2")
    assert one.node.dev.endswith("index0") and two.node.dev.endswith("SER2-video-index0")
    assert vi.open_uvc("video2", **kw).node.usb_id == "534d:2109:SER2"
    assert vi.open_uvc("534d:21", **kw).missing                              # префикс без «:» не совпадает
    assert v4l2.aux_opened == []


def test_identical_adapters_without_serial_need_node_name(v4l2):
    v4l2.add("video0", "uvcvideo", "A", 0, ("534d", "2109", ""), formats=ALL3)
    v4l2.add("video2", "uvcvideo", "A", 0, ("534d", "2109", ""), formats=ALL3)
    kw = v4l2.scan_kw()
    err = vi.open_uvc("534d:2109", **kw).error
    assert "several UVC devices" in err and err.count("534d:2109") == 2
    assert vi.open_uvc("video2", **kw).node.name == "video2"


# ---------------- 5. ошибки open/ioctl и восстановление ----------------
@pytest.mark.parametrize("code", [errno.EACCES, errno.EBUSY, errno.ENODEV, errno.ENOENT])
def test_open_errors_are_reported_and_recover(v4l2, code):
    v4l2.pi4().open_errors["video0"] = code
    src = vi.open_uvc(**v4l2.scan_kw())
    assert not src.available and src.missing, src.error                      # «нет устройства»: идёт повторный поиск
    assert src.error.startswith("UVC device not found (video0: cannot open") and os.strerror(code) in src.error
    del v4l2.open_errors["video0"]
    assert vi.open_uvc(**v4l2.scan_kw()).available


def test_ioctl_error_other_than_einval_aborts_only_that_node(v4l2):
    v4l2.pi4()
    v4l2.add("video2", "uvcvideo", "Good", 0, ("046d", "0825", ""), formats=ALL3)

    def eio(name, req):
        if name == "video0" and req == vi.VIDIOC_ENUM_FMT:
            raise OSError(errno.EIO, "Input/output error")
    v4l2.hooks.append(eio)
    nodes = {n.name: n for n in vi.scan(**v4l2.scan_kw())}
    assert not nodes["video0"].capture and "Input/output error" in nodes["video0"].reason
    assert nodes["video2"].capture
    assert vi.open_uvc(**v4l2.scan_kw()).node.name == "video2"               # единственное рабочее устройство


def test_open_uvc_errors_only_for_selected_device(v4l2):
    v4l2.pi4().open_errors["video0"] = errno.EACCES
    assert vi.open_uvc("046d:0825", **v4l2.scan_kw()).error == "UVC device 046d:0825 not found"


# ---------------- 7. регрессия синтетического режима ----------------
def test_synthetic_without_pango_elements(monkeypatch):
    monkeypatch.setattr(gst, "has_element", lambda name: name not in ("timeoverlay", "textoverlay"))
    src = gst.synthetic()
    assert src.capture() == ["videotestsrc", "is-live=true", "pattern=smpte", "!",
                             "video/x-raw,width=1280,height=720,framerate=30/1", "!"]
    assert src.decode() == [] and src.mode.pixfmt == "I420"


def test_synthetic_source_never_passes_through(monkeypatch):
    monkeypatch.setattr(gst, "has_element", lambda name: True)
    text = " ".join(gst.stream_sender_args("jpeg"))
    assert text.count("videotestsrc") == 1 and "jpegenc quality=70 ! rtpjpegpay" in text and "v4l2src" not in text
    assert gst.stream_sender_args("jpeg", src=gst.synthetic()) == gst.stream_sender_args("jpeg")
