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


# ---------------- переход источника (S5 GUI-01): «Воспроизведение» — только по кадрам нового источника ----------------
class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class Rx:
    """Приёмник пульта без Qt: метки декодированных кадров в шкале подменных часов (VideoReceiver.frames_since)."""

    def __init__(self, clock):
        self.clock, self.frames = clock, []

    def run(self, seconds, fps=25):
        """Кадры идут seconds секунд (fps=0 — кадров нет, время идёт)."""
        end = self.clock.t + seconds
        while self.clock.t < end - 1e-9:
            self.clock.t = round(self.clock.t + (1 / fps if fps else 0.05), 4)
            if fps:
                self.frames.append(self.clock.t)

    def since(self, t):
        return sum(1 for f in self.frames if f > t)


def remote(input_, state, device="", restarts=0):
    return vs.describe("remote", {**TEL_11, "source": {"input": input_, "state": state, "device": device,
                                                        "restarts": restarts}}, {})


def screen(h, rx, src, rx_state):
    return vs.display(src, rx_state, h.update(src, rx.since))


def test_display_not_fresh_is_connecting_not_playing():
    d = remote("rtsp", "starting")
    assert vs.display(d, "playing", fresh=False) == {"display": vs.CONNECTING, "live": False}
    assert vs.display(d, "waiting", fresh=False) == {"display": vs.CONNECTING, "live": False}
    assert vs.display(d, "no-video", fresh=False) == {"display": vs.CONNECTING, "live": False}
    s = remote("rtsp", "streaming")
    assert vs.display(s, "no-video", fresh=False) == {"display": "no-video", "live": False}   # REQ-SW-002
    assert vs.display(remote("rtsp", "error"), "playing", fresh=False)["display"] == "error"   # причина важнее
    assert vs.display(d, "playing", fresh=True) == {"display": "playing", "live": True}


def test_switch_synthetic_to_rtsp_while_old_frames_continue():
    """Смена synthetic → rtsp: приёмник ещё «playing» (буфер, NO_VIDEO_AFTER_S), старые кадры идут ещё FLUSH_S —
    «Подключение», а не «Воспроизведение»; затем пауза (подключение RTSP) и кадры нового — «Воспроизведение»."""
    clock = Clock()
    h, rx = vs.Handover(clock), Rx(clock)
    rx.run(2.0)
    assert screen(h, rx, remote("synthetic", "streaming", "videotestsrc"), "playing")["live"]
    assert screen(h, rx, remote("rtsp", "starting", "rtsp://cam/1"), "playing") == \
        {"display": vs.CONNECTING, "live": False}
    assert h.reason == "change" and h.guard == pytest.approx(clock.t + vs.FLUSH_S)
    rx.run(vs.FLUSH_S - 0.05)                      # кадры прежнего источника, бывшие в пути
    assert screen(h, rx, remote("rtsp", "starting", "rtsp://cam/1"), "playing")["display"] == vs.CONNECTING
    rx.run(0.6, fps=0)                             # RTSP подключается: кадров нет
    assert screen(h, rx, remote("rtsp", "starting", "rtsp://cam/1"), "playing")["display"] == vs.CONNECTING
    rx.run(0.04)                                   # первый кадр нового источника — ещё не подтверждение
    assert not screen(h, rx, remote("rtsp", "starting", "rtsp://cam/1"), "playing")["live"]
    rx.run(0.04)                                   # NEW_FRAMES кадров: блок ещё «starting», а кадр уже показывается
    assert screen(h, rx, remote("rtsp", "starting", "rtsp://cam/1"), "playing") == {"display": "playing", "live": True}
    assert screen(h, rx, remote("rtsp", "streaming", "rtsp://cam/1"), "playing")["live"]


def test_rtsp_wrong_codec_starting_then_error():
    """Блок starting → error (кодек не поддерживается): «Подключение», затем причина; кадры прежнего не показываются."""
    clock = Clock()
    h, rx = vs.Handover(clock), Rx(clock)
    rx.run(1.0)
    screen(h, rx, remote("synthetic", "streaming"), "playing")
    assert screen(h, rx, remote("rtsp", "starting", "rtsp://cam/h265"), "playing")["display"] == vs.CONNECTING
    rx.run(0.3)
    assert screen(h, rx, remote("rtsp", "error", "rtsp://cam/h265"), "playing") == {"display": "error", "live": False}
    rx.run(2.0, fps=0)
    assert screen(h, rx, remote("rtsp", "error", "rtsp://cam/h265"), "no-video")["display"] == "error"
    # повторная попытка блока (error → starting) — снова переход; кадров нет — «Подключение», не «нет видео»
    assert screen(h, rx, remote("rtsp", "starting", "rtsp://cam/h265", 1), "no-video")["display"] == vs.CONNECTING


def test_session_start_shows_frames_as_soon_as_they_arrive():
    """Старт сеанса: блок сообщает starting (idle → starting) и отстаёт от кадров на 0,3–1,7 с. Кадров до перехода
    нет — guard = момент перехода: «Воспроизведение» с NEW_FRAMES-м кадром, без ожидания streaming и FLUSH_S."""
    clock = Clock()
    h, rx = vs.Handover(clock), Rx(clock)
    assert screen(h, rx, remote("synthetic", "starting"), "waiting")["display"] == vs.CONNECTING
    assert h.reason == "start" and h.guard == clock.t
    rx.run(0.4, fps=0)
    assert screen(h, rx, remote("synthetic", "starting"), "waiting")["display"] == vs.CONNECTING
    rx.run(2 / 25)
    assert screen(h, rx, remote("synthetic", "starting"), "playing") == {"display": "playing", "live": True}
    rx.run(1.0)                                    # блок подтверждает позже — без нового перехода и чёрного кадра
    assert screen(h, rx, remote("synthetic", "streaming"), "playing")["live"] and not h.pending


def test_reconnect_without_change_and_block_restart():
    clock = Clock()
    h, rx = vs.Handover(clock), Rx(clock)
    rx.run(1.0)
    assert screen(h, rx, remote("synthetic", "streaming"), "playing")["live"]
    lost = vs.describe("remote", None, None, link="RECONNECTING")
    assert vs.display(lost, "playing", h.update(lost, rx.since))["live"]       # кадры идут — актуальны
    assert screen(h, rx, remote("synthetic", "streaming"), "playing")["live"]  # тот же источник — без перехода
    rx.run(1.0)
    assert not screen(h, rx, remote("synthetic", "streaming", restarts=1), "playing")["live"]   # перезапуск конвейера
    rx.run(vs.FLUSH_S + 0.1)
    assert screen(h, rx, remote("synthetic", "streaming", restarts=1), "playing")["live"]


def test_select_from_console_waits_for_block_change():
    clock = Clock()
    h, rx = vs.Handover(clock), Rx(clock)
    rx.run(1.0)
    screen(h, rx, remote("synthetic", "streaming"), "playing")
    h.select()
    rx.run(1.0)                                    # блок ещё не сменил источник: все кадры — прежнего
    assert not screen(h, rx, remote("synthetic", "streaming"), "playing")["live"]
    h.confirmed()                                  # sources[].active — новый, сведения источника ещё прежние
    rx.run(1.0)
    assert not screen(h, rx, remote("synthetic", "streaming"), "playing")["live"]
    assert screen(h, rx, remote("rtsp", "starting", "rtsp://cam/1"), "playing")["display"] == vs.CONNECTING
    rx.run(vs.FLUSH_S + 0.1)
    assert screen(h, rx, remote("rtsp", "streaming", "rtsp://cam/1"), "playing")["live"]
    # отказ блока — источник прежний, кадры снова актуальны сразу
    h.select()
    assert not screen(h, rx, remote("rtsp", "streaming", "rtsp://cam/1"), "playing")["live"]
    h.cancel()
    assert screen(h, rx, remote("rtsp", "streaming", "rtsp://cam/1"), "playing")["live"]
    # блок принял выбор, но смены источника не сообщил (тот же источник под другим id) — не дольше SELECT_GRACE_S
    h.select()
    h.confirmed()
    rx.run(vs.SELECT_GRACE_S + 0.1)
    assert not screen(h, rx, remote("rtsp", "streaming", "rtsp://cam/1"), "playing")["live"]
    rx.run(vs.FLUSH_S + 0.1)
    assert screen(h, rx, remote("rtsp", "streaming", "rtsp://cam/1"), "playing")["live"]
