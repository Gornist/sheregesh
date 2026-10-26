# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S5 VIDEO: путь V4L2 видеоприёма против свойств вероятных драйверов дешёвых адаптеров (L2, без устройств).

Профили драйверов — по исходникам ядра (rpi-6.12.y; на pi2 6.18.50+rpt-rpi-v8 модули usbtv, stk1160, em28xx, uvc есть):
  usbtv   (UTV007, 1b71:3002): только YUYV, без ENUM_FRAMESIZES и без QUERYSTD (ENOTTY), умолчание драйвера — 525/60
          (NTSC), входы «Composite»/«S-Video» без status, поле V4L2_FIELD_INTERLACED;
  stk1160 (05e1:0408): только UYVY, без ENUM_FRAMESIZES, QUERYSTD через saa711x, умолчание NTSC-M,
          входы «Composite0»…«Composite3», «S-Video», поле INTERLACED;
  em28xx  (EM2860, eb1a:2861): YUYV + RGB565/Bayer/YUV411P, ENUM_FRAMESIZES ступенчато 48×38…720×576, без
          ENUM_FRAMEINTERVALS, QUERYSTD есть, умолчание PAL, входы «Composite»/«S-Video»;
  uvcvideo MS2109 (534d:2109): MJPEG до 1080p30, YUYV ≥ 720p — 5–10 к/с (типичный перечень по открытым отчётам).
sysfs, /dev и ioctl — поддельные (tests/system/fake_v4l2.py), структуры videodev2.h — настоящие.
"""
import errno
import shutil
import subprocess

import pytest

from fake_v4l2 import NTSC_STD, PAL_STD, FakeV4L2
from sheregesh import video as gst
from sheregesh import videoinput as vi

DEINT = ["deinterlace", "mode=auto", "method=linear", "fields=top", "!"]
STD_CAP = vi.V4L2_IN_CAP_STD          # ядро (v4l_enuminput) ставит его само, если у узла есть tvnorms и s_std


@pytest.fixture
def v4l2(tmp_path):
    return FakeV4L2(tmp_path)


@pytest.fixture
def elements(monkeypatch):
    monkeypatch.setattr(gst, "has_element", lambda name: True)
    monkeypatch.setattr(gst, "h264_elements", lambda: (gst.V4L2_ENC, "v4l2h264dec"))


def usbtv(v4l2):
    v4l2.add("video0", "usbtv", "usbtv", 0, ("1b71", "3002", ""), formats=[("YUYV", "discrete", [])],
             inputs=[{"name": "Composite", "capabilities": STD_CAP}, {"name": "S-Video", "capabilities": STD_CAP}],
             standards=[NTSC_STD, PAL_STD], std=vi.V4L2_STD_525_60, sizes_ioctl=False, querystd_ioctl=False)
    return v4l2


def stk1160(v4l2, querystd=None):
    v4l2.add("video0", "stk1160", "stk1160", 0, ("05e1", "0408", ""), formats=[("UYVY", "discrete", [])],
             inputs=[{"name": f"Composite{i}", "capabilities": STD_CAP} for i in range(4)] +
             [{"name": "S-Video", "capabilities": STD_CAP}],
             standards=[NTSC_STD, PAL_STD], std=0x00001000, querystd=querystd, sizes_ioctl=False)   # NTSC-M
    return v4l2


def em28xx(v4l2, querystd=vi.V4L2_STD_PAL):
    step = ((48, 720, 1, 38, 576, 1), None)
    v4l2.add("video0", "em28xx", "EM2860/SAA711X Reference Design", 0, ("eb1a", "2861", ""),
             formats=[("YUYV", "stepwise", [step]), ("RGBP", "stepwise", [step]), ("411P", "stepwise", [step])],
             inputs=[{"name": "Composite", "capabilities": STD_CAP}, {"name": "S-Video", "capabilities": STD_CAP}],
             standards=[PAL_STD, NTSC_STD], std=vi.V4L2_STD_PAL, querystd=querystd, intervals_ioctl=False)
    return v4l2


MS2109 = [("MJPG", "discrete", [((1920, 1080), [30, 25, 20, 10, 5]), ((1280, 720), [60, 50, 30, 20, 10]),
                                ((720, 576), [50, 25]), ((720, 480), [60, 30]), ((640, 480), [60, 30])]),
          ("YUYV", "discrete", [((1920, 1080), [5]), ((1280, 720), [10]), ((720, 480), [30]), ((640, 480), [30])])]


def ms2109(v4l2):
    v4l2.add("video0", "uvcvideo", "USB Video: USB Video", 0, ("534d", "2109", ""), formats=MS2109)
    v4l2.add("video1", "uvcvideo", "USB Video: USB Video", 1, ("534d", "2109", ""), caps=vi.V4L2_CAP_META_CAPTURE)
    return v4l2


def test_s_input_request_number_and_detected_family():
    assert vi.VIDIOC_S_INPUT == 0xC0045627
    assert vi.detected_family(vi.V4L2_STD_PAL) == "PAL" and vi.detected_family(vi.V4L2_STD_NTSC) == "NTSC"
    assert vi.detected_family(0x00FF0000) == "PAL"                                  # SECAM — 625/50
    assert vi.detected_family(vi.V4L2_STD_525_60 | vi.V4L2_STD_625_50) == ""       # несуженный ответ: не распознан
    assert vi.detected_family(0) == "" and vi.detected_family(None) == ""


# ---------------- usbtv ----------------
def test_usbtv_auto_standard_is_pal_not_driver_default_ntsc(v4l2):
    """usbtv: QUERYSTD нет (ENOTTY), G_STD — умолчание драйвера 525/60. «auto» → PAL (DEFAULT_STANDARD),
    а не NTSC из G_STD (раньше: NTSC 720×480 на PAL-сигнале — нет изображения)."""
    src = vi.open_source({"source": "v4l2", "device": "1b71:3002"}, **usbtv(v4l2).scan_kw())
    assert src.available and src.standard == "PAL" and src.standard_source == "default"
    assert src.mode == vi.Mode(720, 576, 25, "YUYV")
    assert src.capture()[2] == "norm=PAL" and "video/x-raw,format=YUY2,width=720,height=576,framerate=25/1" in src.capture()
    assert src.info()["standard_source"] == "default" and src.info()["input_name"] == "Composite"
    assert v4l2.s_input == []                                                     # вход не задан — не трогается


def test_usbtv_ntsc_must_be_explicit(v4l2):
    src = vi.open_source({"source": "v4l2", "device": "1b71:3002", "standard": "ntsc"}, **usbtv(v4l2).scan_kw())
    assert src.standard == "NTSC" and src.standard_source == "config"
    assert src.mode == vi.Mode(720, 480, 30, "YUYV", "30000/1001") and "norm=NTSC" in src.capture()
    assert "framerate=30000/1001" in src.capture()[-2]


def test_usbtv_s_video_input_selected_and_signal_probed_on_it(v4l2):
    usbtv(v4l2)
    src = vi.open_source({"source": "v4l2", "device": "1b71:3002", "input": "s-video"}, **v4l2.scan_kw())
    assert src.available and v4l2.s_input == [("video0", 1)] and src.node.input == 1
    assert src.info()["input_name"] == "S-Video"
    v4l2.devices["video0"]["inputs"][1]["status"] = vi.V4L2_IN_ST_NO_H_LOCK
    assert src.signal() == "no-signal"                                            # опрашивается выбранный вход
    v4l2.devices["video0"]["inputs"][1]["status"] = 0
    assert src.signal() is None


@pytest.mark.parametrize("want,index", [("composite", 0), ("Composite", 0), ("cvbs", 0), ("av", 0), ("0", 0),
                                        ("S-Video", 1), ("svideo", 1), ("s_video", 1), ("yc", 1), ("1", 1)])
def test_usbtv_input_names_and_numbers(v4l2, want, index):
    src = vi.open_v4l2("1b71:3002", input=want, **usbtv(v4l2).scan_kw())
    assert src.available and src.node.input == index
    assert v4l2.s_input == ([("video0", index)] if index else [])                # вход 0 уже текущий — без S_INPUT


@pytest.mark.parametrize("want", ["", "auto", "AUTO"])
def test_input_auto_leaves_driver_input_alone(v4l2, want):
    usbtv(v4l2).devices["video0"]["input"] = 1
    src = vi.open_source({"source": "v4l2", "input": want}, **v4l2.scan_kw())
    assert src.available and src.node.input == 1 and v4l2.s_input == []


def test_input_selected_before_standard_is_queried(v4l2):
    """QUERYSTD измеряет текущий вход: S_INPUT обязан идти раньше."""
    stk1160(v4l2, querystd=vi.V4L2_STD_PAL)
    order = []
    v4l2.hooks.append(lambda name, req: order.append(req) if req in (vi.VIDIOC_S_INPUT, vi.VIDIOC_QUERYSTD) else None)
    src = vi.open_v4l2(input="s-video", **v4l2.scan_kw())
    assert src.available and order == [vi.VIDIOC_S_INPUT, vi.VIDIOC_QUERYSTD]


def test_unknown_input_is_reported_with_offered_inputs(v4l2):
    src = vi.open_v4l2(input="hdmi", **usbtv(v4l2).scan_kw())
    assert not src.available and not src.missing and v4l2.s_input == []
    assert "input 'hdmi' not found on usbtv; inputs: 0 Composite, 1 S-Video" == src.error
    assert not vi.open_v4l2(input="2", **v4l2.scan_kw()).available


def test_input_on_device_without_inputs(v4l2):
    src = vi.open_v4l2(input="composite", **ms2109(v4l2).scan_kw())             # подделка UVC: ENUMINPUT → ENOTTY
    assert not src.available and "inputs: none (VIDIOC_ENUMINPUT)" in src.error


@pytest.mark.parametrize("code,missing", [(errno.EBUSY, False), (errno.ENODEV, True)])
def test_s_input_failure(v4l2, code, missing):
    usbtv(v4l2)

    def fail(name, req):
        if req == vi.VIDIOC_S_INPUT:
            raise OSError(code, "boom")
    v4l2.hooks.append(fail)
    src = vi.open_v4l2(input="s-video", **v4l2.scan_kw())
    assert not src.available and src.missing is missing and "cannot be selected" in src.error


# ---------------- stk1160 ----------------
@pytest.mark.parametrize("want,index", [("composite", 0), ("composite2", 2), ("3", 3), ("s-video", 4)])
def test_stk1160_inputs(v4l2, want, index):
    src = vi.open_v4l2(input=want, **stk1160(v4l2).scan_kw())
    assert src.available and src.node.input == index


def test_stk1160_auto_detects_ntsc_and_uses_uyvy(v4l2, elements):
    src = vi.open_v4l2(**stk1160(v4l2, querystd=vi.V4L2_STD_NTSC).scan_kw())
    assert src.standard == "NTSC" and src.standard_source == "detected"
    assert src.mode == vi.Mode(720, 480, 30, "UYVY", "30000/1001")
    text = " ".join(gst.stream_sender_args("h264", src=src))
    assert "video/x-raw,format=UYVY,width=720,height=480,framerate=30000/1001 ! " \
           "deinterlace mode=auto method=linear fields=top ! v4l2h264enc" in text


def test_ambiguous_querystd_falls_back_to_default(v4l2):
    src = vi.open_v4l2(**stk1160(v4l2, querystd=vi.V4L2_STD_525_60 | vi.V4L2_STD_625_50).scan_kw())
    assert src.standard == "PAL" and src.standard_source == "default"


def test_auto_standard_redetected_when_signal_appears(v4l2):
    """Блок включился раньше камеры: при открытии сигнала нет (PAL по умолчанию), к следующему запуску конвейера
    появился NTSC — capture() перепроверяет QUERYSTD и меняет стандарт и режим."""
    stk1160(v4l2, querystd=None)
    src = vi.open_v4l2(**v4l2.scan_kw())
    assert (src.standard, src.standard_source, src.mode.height) == ("PAL", "default", 576)
    assert src.capture()[2] == "norm=PAL"                                         # сигнала всё ещё нет — без изменений
    v4l2.devices["video0"]["querystd"] = vi.V4L2_STD_NTSC
    cap = src.capture()
    assert cap[2] == "norm=NTSC" and "format=UYVY,width=720,height=480,framerate=30000/1001" in cap[-2]
    assert (src.standard, src.standard_source, src.info()["resolution"]) == ("NTSC", "detected", "720x480")
    v4l2.devices["video0"]["querystd"] = vi.V4L2_STD_PAL
    assert src.capture()[2] == "norm=PAL" and src.mode == vi.Mode(720, 576, 25, "UYVY")


def test_explicit_standard_or_unfitting_mode_is_not_redetected(v4l2):
    stk1160(v4l2, querystd=vi.V4L2_STD_PAL)
    fixed = vi.open_v4l2(standard="pal", **v4l2.scan_kw())
    v4l2.devices["video0"]["querystd"] = vi.V4L2_STD_NTSC
    asked = []
    v4l2.hooks.append(lambda name, req: asked.append(req) if req == vi.VIDIOC_QUERYSTD else None)
    assert fixed.capture()[2] == "norm=PAL" and asked == []                       # явный стандарт: QUERYSTD не нужен
    v4l2.devices["video0"]["querystd"] = vi.V4L2_STD_PAL
    exact = vi.open_v4l2(mode="720x576@25", **v4l2.scan_kw())
    v4l2.devices["video0"]["querystd"] = vi.V4L2_STD_NTSC
    assert exact.capture()[2] == "norm=PAL" and exact.mode.height == 576         # 720×576 в NTSC нет — не подменяется


# ---------------- em28xx ----------------
def test_em28xx_stepwise_sizes_without_intervals_use_standard_modes(v4l2, elements):
    em28xx(v4l2)
    node = vi.capture_devices(**v4l2.scan_kw())[0]
    assert node.modes == [] and node.formats == ["YUYV", "RGBP", "411P"] and node.analog   # без интервалов — режимов нет
    src = vi.open_v4l2("eb1a:2861", **v4l2.scan_kw())
    assert src.standard == "PAL" and src.standard_source == "detected" and src.mode == vi.Mode(720, 576, 25, "YUYV")
    assert {m.pixfmt for m in src.formats} == {"YUYV"}                            # RGB565/YUV411P не выбираются
    assert src.decode() == DEINT and src.input_type == "CVBS"


# ---------------- HDMI MS2109 (UVC) ----------------
def test_ms2109_auto_prefers_mjpeg_720p30_over_1080p_and_slow_yuyv(v4l2, elements):
    ms2109(v4l2)
    src = vi.open_v4l2(**v4l2.scan_kw())
    assert src.kind == "uvc" and src.input_type == "HDMI" and src.standard == "" and src.standard_source == ""
    assert src.mode == vi.Mode(1280, 720, 30, "MJPG")                              # не YUYV 1280×720@10, не 1080p
    assert src.decode() == ["jpegdec", "!"]                                       # без deinterlace: UVC построчный
    assert "norm=" not in " ".join(src.capture())
    assert vi.open_v4l2(mode="1920x1080@30", **v4l2.scan_kw()).mode == vi.Mode(1920, 1080, 30, "MJPG")
    assert vi.open_v4l2(mode="720x480@30", **v4l2.scan_kw()).mode.pixfmt == "YUYV"   # малый режим — без декодера
    assert not vi.open_v4l2(mode="3840x2160@30", **v4l2.scan_kw()).available       # «4K» — только если устройство даёт
    text = " ".join(gst.stream_sender_args("h264", src=src))
    assert "image/jpeg,width=1280,height=720,framerate=30/1 ! jpegdec ! videoconvert" in text and "deinterlace" not in text


def test_ms2109_without_720p30_takes_1080p30(v4l2):
    v4l2.add("video0", "uvcvideo", "USB Video: USB Video", 0, ("534d", "2109", ""),
             formats=[("MJPG", "discrete", [((1920, 1080), [30]), ((1280, 720), [60])])])
    assert vi.open_v4l2(**v4l2.scan_kw()).mode == vi.Mode(1920, 1080, 30, "MJPG")


# ---------------- настоящий GStreamer: deinterlace даёт построчные кадры той же частоты ----------------
@pytest.mark.skipif(not (shutil.which(gst.GST_LAUNCH) and gst.has_element("deinterlace")),
                    reason="нет GStreamer с deinterlace")
@pytest.mark.parametrize("fmt,size,rate", [("YUY2", "width=720,height=576", "25/1"),
                                           ("UYVY", "width=720,height=480", "30000/1001")])
def test_deinterlace_negotiates_progressive_at_standard_rate(fmt, size, rate):
    caps = f"video/x-raw,format={fmt},{size},framerate={rate},interlace-mode=interleaved"
    r = subprocess.run([gst.GST_LAUNCH, "-q", "videotestsrc", "num-buffers=5", "!", caps, "!", *DEINT,
                        f"video/x-raw,interlace-mode=progressive,framerate={rate}", "!", "fakesink"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


# UVC-захват CVBS MacroSilicon MS210x «AV TO USB2.0» (534d:0021, EasierCAP): профиль — tools/lab-video/CONTRACT.md §1
# (эмулятор USB/IP стенда shcam); стандартов в V4L2 нет — PAL/NTSC выбираются размером кадра.
MS210X_CVBS = [("MJPG", "discrete", [((720, 480), [30]), ((720, 576), [25])]),
               ("YUYV", "discrete", [((720, 480), [30]), ((720, 576), [25])])]


def ms210x_cvbs(v4l2):
    v4l2.add("video0", "uvcvideo", "AV TO USB2.0: AV TO USB2.0", 0, ("534d", "0021", ""), formats=MS210X_CVBS)
    return v4l2


@pytest.mark.parametrize("standard, size, fps", [("pal", (720, 576), 25), ("ntsc", (720, 480), 30),
                                                 ("PAL", (720, 576), 25)])
def test_uvc_cvbs_standard_selects_frame_mode(v4l2, standard, size, fps):
    src = vi.open_v4l2(standard=standard, **ms210x_cvbs(v4l2).scan_kw())
    assert isinstance(src, vi.UVCSource) and (src.mode.width, src.mode.height, src.mode.fps) == (*size, fps)
    assert "norm=" not in " ".join(src.capture())       # S_STD у UVC нет — v4l2src без norm
    assert src.node.input_type == "CVBS"


def test_uvc_cvbs_auto_and_explicit_mode_unchanged(v4l2):
    kw = ms210x_cvbs(v4l2).scan_kw()
    src = vi.open_v4l2(**kw)
    assert (src.mode.width, src.mode.height) in ((720, 576), (720, 480))
    src = vi.open_v4l2(mode="720x480@30", standard="pal", **kw)
    assert isinstance(src, vi.Unavailable) and "not supported" in src.error   # режим противоречит стандарту


def test_platform_capture_id_and_type_same_with_and_without_probe(v4l2):
    """S5 pi2 (vivid): каталог блока видит платформенные узлы захвата с тем же id, что у открытого источника, и с
    типом CVBS — id из sysfs («устройство:имя»), не из bus_info/by-path и не videoN; тип — опрос без захвата."""
    from sheregesh import videosources as vs
    v4l2.add("video2", "vivid", "vivid-000-vid-cap", 0, None, formats=[("YUYV", "discrete", [((720, 576), [25])])],
             inputs=[{"name": "S-Video 000-0", "capabilities": STD_CAP}], standards=[PAL_STD, NTSC_STD],
             std=vi.V4L2_STD_PAL, bus="platform:vivid-000")          # by-path одна на двоих — у второго (udev)
    v4l2.add("video4", "vivid", "vivid-001-vid-cap", 0, None, formats=[("YUYV", "discrete", [((1280, 720), [30])])],
             bus="platform:vivid-001", by_path="platform-vivid.0-video-index0")
    kw = v4l2.scan_kw()
    ids = {p: [vi.stable_id(n) for n in vi.scan(probe=p, **kw) if n.capture] for p in (False, True, "non-usb")}
    assert ids[False] == ids[True] == ids["non-usb"] and len(set(ids[True])) == 2
    assert all(i.endswith(("vivid-000-vid-cap", "vivid-001-vid-cap")) and ":" in i for i in ids[True])
    got = {e["id"]: e["input"] for e in vs.build({"source": "synthetic"}, [], vi.scan(probe="non-usb", **kw))}
    cvbs = [i for i, t in got.items() if t == "cvbs"]
    assert len(cvbs) == 1 and cvbs[0].endswith("vivid-000-vid-cap")
    src = vi.open_v4l2(device=cvbs[0][5:], standard="pal", **kw)
    assert f"v4l2:{vi.stable_id(src.node)}" == cvbs[0]          # выбор из каталога = id открытого источника
