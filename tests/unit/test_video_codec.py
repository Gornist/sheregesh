# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Выбор связки H.264: аппаратная v4l2 (Pi 4) в приоритете, откат по пробному прогону (подготовка к пробе S2-4).

Элементы GStreamer подменяются: на x86 и macOS v4l2h264enc нет, проверяется только логика выбора и сборка конвейеров.
"""
import pytest

from sheregesh import video


@pytest.fixture
def fake_gst(monkeypatch):
    def setup(elements, working):
        """elements — какие элементы «есть»; working — какие пары (кодер, декодер) проходят пробный прогон."""
        monkeypatch.setattr(video, "has_element", lambda name: name in elements)
        monkeypatch.setattr(video, "_pipeline_ok",
                            lambda els: any(e in els and d in els for e, d in working) or
                            ("jpegenc" in els and "jpegenc" in elements))
        video.h264_elements.cache_clear()
        video.codec_works.cache_clear()
    yield setup
    video.h264_elements.cache_clear()
    video.codec_works.cache_clear()


COMMON = {"rtph264pay", "rtph264depay", "h264parse", "jpegenc", "rtpjpegpay", "jpegdec", "rtpjpegdepay"}


def test_hardware_pair_preferred_on_pi(fake_gst):
    fake_gst(COMMON | {"v4l2h264enc", "v4l2h264dec", "openh264enc", "avdec_h264"},
             [("v4l2h264enc", "v4l2h264dec"), ("openh264enc", "avdec_h264")])
    assert video.choose_codec() == "h264"
    assert video.describe("h264") == "H.264 (v4l2h264enc → v4l2h264dec)"
    tx = video.sender_args("h264", 5600)
    assert tx[tx.index("v4l2h264enc") + 1].startswith("extra-controls=controls,repeat_sequence_header=1")
    assert "video/x-h264,level=(string)4" in tx and tx.index("rtph264pay") > tx.index("h264parse")
    rx = video.receiver_args("h264", 5600, 640, 360)
    assert rx.index("rtph264depay") < rx.index("h264parse") < rx.index("v4l2h264dec")


def test_broken_hardware_falls_back_to_software(fake_gst):
    """Элемент есть, но пробный прогон не проходит (например, нет /dev/video11) — программная связка."""
    fake_gst(COMMON | {"v4l2h264enc", "v4l2h264dec", "openh264enc", "avdec_h264"},
             [("openh264enc", "avdec_h264")])
    assert video.choose_codec() == "h264"
    assert video.describe("h264") == "H.264 (openh264enc → avdec_h264)"
    assert "v4l2h264enc" not in video.sender_args("h264", 5600)


def test_hardware_encoder_with_software_decoder(fake_gst):
    fake_gst(COMMON | {"v4l2h264enc", "avdec_h264"}, [("v4l2h264enc", "avdec_h264")])
    assert video.describe("h264") == "H.264 (v4l2h264enc → avdec_h264)"


def test_no_working_h264_falls_back_to_mjpeg(fake_gst):
    fake_gst(COMMON | {"openh264enc", "openh264dec"}, [])         # заглушка libopenh264 в Ubuntu
    assert video.h264_elements() is None
    assert video.choose_codec() == "jpeg"
    assert video.describe("jpeg").startswith("MJPEG")


def test_nothing_works(fake_gst):
    fake_gst(set(), [])
    assert video.choose_codec() is None


def test_sender_without_pango_overlays_still_streams(fake_gst):
    """В Debian timeoverlay/textoverlay — отдельный пакет gstreamer1.0-x; без него генератор не должен падать."""
    fake_gst(COMMON, [])
    tx = video.sender_args("jpeg", 5600)
    assert "timeoverlay" not in tx and "textoverlay" not in tx and "jpegenc" in tx
    fake_gst(COMMON | {"timeoverlay", "textoverlay"}, [])
    tx = video.sender_args("jpeg", 5600)
    assert tx.index("videotestsrc") < tx.index("timeoverlay") < tx.index("textoverlay") < tx.index("jpegenc")


def test_probe_errors_ignore_environment_warnings():
    """S2-BOOT: GLib-WARNING об uid DynamicUser не отказ пробы; ошибки gst-launch и кодеков — отказ."""
    env = ("(gst-launch-1.0:783): GLib-WARNING **: getpwuid_r(): failed due to unknown user id (64421)\n"
           "Fontconfig error: No writable cache directories\nSetting pipeline to PLAYING ...\nGot EOS")
    assert video.probe_errors(env) == []
    assert video.probe_errors('WARNING: erroneous pipeline: no element "h264parse"')
    assert video.probe_errors("ERROR: from element /GstPipeline:pipeline0/GstV4l2H264Enc: Device busy")
    assert video.probe_errors("[OpenH264] this = 0x7f, Error:WelsInitEncoderExt(), ParamValidationExt failed")


def test_sender_socket_bound_to_loopback(fake_gst):
    """S2-BOOT: udpsink без bind-address занимал 0.0.0.0:<случайный порт> — отправитель тоже только на loopback."""
    fake_gst(COMMON, [])
    tx = video.sender_args("jpeg", 5600)
    assert tx[tx.index("udpsink"):] == ["udpsink", "host=127.0.0.1", "port=5600", "bind-address=127.0.0.1", "sync=false"]
