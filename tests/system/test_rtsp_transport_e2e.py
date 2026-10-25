# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""RTSP backend 1.0 (S5), сквозной: настоящий rtspsrc в видеоприёме блока (Ingest) ↔ тестовый сервер стенда
(tools/probe/s4_rtsp_test_server.py) по RTP/TCP interleaved и RTP/UDP.

Матрица transport tcp | udp | auto × mjpeg | h264 (h264 — если есть кодер для сервера и h264parse/rtph264pay);
«один транспорт недоступен» (сервер только tcp → auto по TCP, только udp → auto по UDP, transport=udp при сервере
только tcp → error без зависания); «фаервол» (UDP-сеанс без датаграмм → auto переходит на TCP); потеря сервера и
возврат при UDP и auto. Фактический транспорт проверяется по журналу SETUP сервера (Server.sessions)."""
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from procs import wait_for
from sheregesh import video
from sheregesh import videoinput as vi
from sheregesh.videoingest import Ingest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "probe"))
import s4_rtsp_test_server as rt  # noqa: E402

BASE = ("rtspsrc", "rtpstreampay", "videotestsrc", "videoconvert")
NEED = {"mjpeg": ("jpegenc", "rtpjpegpay", "rtpjpegdepay"), "h264": ("h264parse", "rtph264pay", "rtph264depay")}


def _missing(codec):
    if not shutil.which("gst-launch-1.0"):
        return "нет gst-launch-1.0"
    lost = [e for e in (*BASE, *NEED[codec]) if not video.has_element(e)]
    if lost:
        return f"нет элементов GStreamer: {', '.join(lost)}"
    if codec == "h264" and rt.h264_encoder() is None:
        return "нет кодера H.264 для сервера (v4l2h264enc / x264enc / openh264enc / vtenc_h264)"
    return ""


def need(codec):
    why = _missing(codec)
    if why:
        pytest.skip(why)


class Run:
    """Сервер + Ingest (настоящий rtspsrc) + потребитель пакетов; закрывается всегда."""

    def __init__(self, transport, codec="mjpeg", server_tr=("tcp", "udp"), udp_drop=False, timeout_s=1.5):
        srv_codec = "jpeg" if codec == "mjpeg" else "h264"
        self.srv = rt.Server("127.0.0.1", 0, srv_codec, rt.producer(srv_codec, 320, 240, 15, label=""), server_tr,
                             udp_drop).start()
        self.url = f"rtsp://op:secret@127.0.0.1:{self.srv.port}/cam"
        url = self.url
        self.ing = Ingest(lambda: vi.RTSPSource(url, codec, latency_ms=50, timeout_s=timeout_s, transport=transport),
                          lambda s: video.stream_sender_args("jpeg" if codec == "mjpeg" else "h264", src=s),
                          no_signal_s=1.0, start_timeout_s=6, stall_restart_s=3, presence_s=0.3,
                          max_backoff_s=1.0).start()
        self.tok = self.ing.attach()
        self.pkts = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._drain, daemon=True)
        self._t.start()

    def _drain(self):
        import select
        while not self._stop.is_set():
            select.select([self.ing.wake_fd], [], [], 0.1)
            self.pkts += len(self.ing.take())

    def state(self, *states):
        return lambda: self.ing.state in states

    def close(self):
        self._stop.set()
        self._t.join(2)
        self.ing.detach(self.tok)
        self.ing.close()
        self.srv.stop()


@pytest.fixture
def run():
    runs = []

    def make(*a, **kw):
        runs.append(Run(*a, **kw))
        return runs[-1]
    yield make
    for r in runs:
        r.close()


@pytest.mark.parametrize("codec", ["mjpeg", "h264"])
@pytest.mark.parametrize("transport, negotiated, reported", [("tcp", "tcp", "tcp"), ("udp", "udp", "udp"),
                                                             ("auto", "udp", "")])
def test_transport_matrix(run, transport, negotiated, reported, codec):
    need(codec)
    r = run(transport, codec)
    assert wait_for(r.state("streaming"), 10), (r.ing.state, r.ing.error)
    assert wait_for(lambda: r.ing.status()["stream_width"] == 320, 5), r.ing.status()
    st = r.ing.status()
    assert (st["stream_width"], st["stream_height"]) == (320, 240) and st["format"] == codec, st
    assert (st["transport"], st["transport_mode"]) == (reported, transport), st
    assert r.srv.sessions == [negotiated], r.srv.sessions               # auto: сначала UDP, сервер его принял
    assert "secret" not in str(st) and r.pkts > 0


@pytest.mark.parametrize("server_tr, negotiated", [(("tcp",), ["tcp"]), (("udp",), ["udp"])])
def test_auto_uses_the_only_offered_transport(run, server_tr, negotiated):
    """Камера только с TCP: SETUP по UDP → 461 → rtspsrc сразу повторяет по TCP; только с UDP — UDP."""
    need("mjpeg")
    r = run("auto", server_tr=server_tr)
    assert wait_for(r.state("streaming"), 10), (r.ing.state, r.ing.error)
    assert r.srv.sessions == negotiated, r.srv.sessions


def test_forced_udp_against_tcp_only_server_is_error_not_hang(run):
    need("mjpeg")
    r = run("udp", server_tr=("tcp",))
    assert wait_for(r.state("error"), 6), (r.ing.state, r.ing.error)
    assert "pipeline exited" in r.ing.error and "secret" not in r.ing.error, r.ing.error
    time.sleep(2)
    assert r.ing.state != "streaming" and r.ing.restarts >= 1 and r.srv.sessions == []


def test_firewalled_udp_falls_back_to_tcp(run):
    """UDP-сеанс принят, датаграммы не доходят: auto через timeout_s переподключается по TCP; udp — ошибка."""
    need("mjpeg")
    r = run("auto", udp_drop=True, timeout_s=1.0)
    assert wait_for(r.state("streaming"), 10), (r.ing.state, r.ing.error)
    assert r.srv.sessions[:1] == ["udp"] and r.srv.sessions[-1] == "tcp", r.srv.sessions
    u = run("udp", udp_drop=True, timeout_s=1.0)
    assert wait_for(u.state("error"), 8), (u.ing.state, u.ing.error)
    assert u.ing.state != "streaming"


@pytest.mark.parametrize("transport", ["udp", "auto"])
def test_server_loss_and_return(run, transport):
    need("mjpeg")
    r = run(transport)
    assert wait_for(r.state("streaming"), 10), (r.ing.state, r.ing.error)
    port = r.srv.port
    r.srv.stop()
    assert wait_for(r.state("connect-error"), 15), (r.ing.state, r.ing.error)
    assert "unreachable" in r.ing.error and "secret" not in r.ing.error
    r.srv.start()
    assert r.srv.port == port
    assert wait_for(r.state("streaming"), 15), (r.ing.state, r.ing.error)
    assert r.srv.sessions[-1] == "udp"
