# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Описание источника видео для экранов пульта (sheregesh_console.videosource), без Qt.

Контракт полей блока ещё уточняется (агент B, единый VideoSource на блоке): здесь — устойчивость к отсутствию полей
(блок 1.1), к близким именам полей и к учётным данным в адресах RTSP.
"""
import pytest

from sheregesh_console import videosource as vs

TEL_11 = {"enabled": True, "active": True, "codec": "h264", "port": 5600, "transport": "tls"}   # блок 1.1
INFO_11 = {"codec": "h264", "width": 1280, "height": 720, "fps": 30}


def test_block_11_without_source_fields_is_synthetic_legacy():
    d = vs.describe("remote", TEL_11, INFO_11, rx_state="playing")
    assert d["origin"] == "remote" and d["input"] == "synthetic" and d["legacy"] and not d["reported"]
    assert d["state"] == "" and (d["width"], d["height"], d["fps"]) == (1280, 720, 30)
    assert vs.display(d, "playing") == {"display": "playing", "live": True}


def test_remote_without_any_report_is_unknown():
    d = vs.describe("remote", None, None, rx_state="off")
    assert d["input"] == "" and not d["legacy"] and vs.display(d, "off") == {"display": "off", "live": False}


@pytest.mark.parametrize("tel,info", [
    ({**TEL_11, "source": {"input": "hdmi", "state": "streaming", "device": "tc358743", "width": 1920,
                           "height": 1080, "fps": 30}}, {}),
    (TEL_11, {**INFO_11, "source": {"kind": "HDMI", "status": "playing", "name": "tc358743",
                                    "resolution": "1920x1080", "framerate": "30/1"}}),
])
def test_source_fields_from_telemetry_or_session_info(tel, info):
    d = vs.describe("remote", tel, info)
    assert (d["input"], d["state"], d["device"]) == ("hdmi", "streaming", "tc358743") and d["reported"]
    assert (d["width"], d["height"]) in ((1920, 1080), (1280, 720))    # режим — сначала из INFO, как в 1.1
    assert d["fps"] == 30


def test_telemetry_state_wins_over_stale_session_info():
    info = {**INFO_11, "source": {"input": "cvbs", "state": "streaming"}}
    tel = {**TEL_11, "source": {"input": "cvbs", "state": "no-signal", "error": "no sync"}}
    d = vs.describe("remote", tel, info)
    assert d["state"] == "no-signal" and d["error"] == "no sync"


@pytest.mark.parametrize("state", ["no-signal", "no-device", "connect-error", "error"])
def test_blocking_source_state_hides_frames_even_if_receiver_plays(state):
    d = vs.describe("remote", {**TEL_11, "source": {"input": "cvbs", "state": state}}, INFO_11)
    assert vs.display(d, "playing") == {"display": state, "live": False}


def test_starting_source_and_recovery():
    d = vs.describe("remote", {**TEL_11, "source": {"input": "rtsp", "state": "starting"}}, {})
    assert vs.display(d, "waiting") == {"display": "starting", "live": False}
    assert vs.display(d, "playing")["live"]
    d = vs.describe("remote", {**TEL_11, "source": {"input": "rtsp", "state": "streaming"}}, {})
    assert vs.display(d, "no-video") == {"display": "no-video", "live": False}   # источник в порядке, кадров нет


def test_rtsp_credentials_are_not_shown():
    url = "rtsp://admin:s3cret@192.168.1.64:554/stream1"
    d = vs.describe("remote", {"source": {"input": "rtsp", "state": "connect-error", "url": url,
                                          "error": f"Could not open {url}: Connection refused"}}, {})
    assert d["device"] == "rtsp://192.168.1.64:554/stream1"
    assert "s3cret" not in d["error"] and "admin" not in d["error"] and "Connection refused" in d["error"]


@pytest.mark.parametrize("raw,norm", [("uvc", "usb"), ("composite", "cvbs"), ("V4L2", "v4l2"), ("videotestsrc", "synthetic"),
                                      ("sdi", "sdi")])
def test_input_aliases(raw, norm):
    d = vs.describe("remote", {"source": raw}, {})
    assert d["input"] == norm and d["reported"] and not d["legacy"]


def test_local_uvc_input_states():
    info = {"kind": "uvc", "name": "USB Video", "available": False, "missing": True, "error": "UVC device not found"}
    d = vs.describe("local-input", local_info=info, rx_state="no-device", rx_error="UVC device not found")
    assert (d["origin"], d["input"], d["state"]) == ("local", "usb", "no-device") and "not found" in d["error"]
    assert vs.display(d, "no-device")["display"] == "no-device"
    info = {"kind": "uvc", "name": "USB Video", "available": False, "error": "mode MJPEG 1920×1080@60 not supported"}
    d = vs.describe("local-input", local_info=info, rx_state="unsupported")
    assert vs.display(d, "unsupported") == {"display": "unsupported", "live": False}
    info = {"kind": "uvc", "name": "USB Video", "available": True, "format": "MJPEG", "resolution": "1280x720", "fps": 60}
    d = vs.describe("local-input", local_info=info, rx_state="playing")
    assert (d["width"], d["height"], d["fps"], d["state"]) == (1280, 720, 60, "")
    assert vs.display(d, "playing")["live"]


def test_off_and_local_synthetic():
    assert vs.describe("off") == vs.empty()
    d = vs.describe("local-synthetic", rx_state="playing")
    assert d["input"] == "synthetic" and d["origin"] == "local"
