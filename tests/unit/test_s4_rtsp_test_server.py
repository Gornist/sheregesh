# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Тестовый RTSP-источник стенда (tools/probe/s4_rtsp_test_server.py): конвейер генератора, сеанс RTSP по TCP,
«падение» и возврат сервера; сквозной приём настоящим rtspsrc через видеоприём блока (если есть GStreamer)."""
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "probe"))
import s4_rtsp_test_server as rt  # noqa: E402


def test_producer_pipeline_overlays_and_encoders():
    every = rt.producer("jpeg", 640, 480, 25, "CAM-A", have=lambda e: True)
    text = " ".join(every)
    assert "timeoverlay" in text and "clockoverlay" in text and "text=CAM-A" in text
    assert text.endswith("jpegenc quality=80 ! rtpjpegpay pt=26 ! rtpstreampay ! fdsink fd=1")
    assert "video/x-raw,width=640,height=480,framerate=25/1" in every
    bare = rt.producer("jpeg", have=lambda e: e not in ("timeoverlay", "clockoverlay", "textoverlay"))
    assert not any("overlay" in x for x in bare)                  # без pango поток идёт без надписей
    pi = " ".join(rt.producer("h264", have=lambda e: e != "x264enc"))
    assert "v4l2h264enc" in pi and "rtph264pay pt=96 config-interval=-1" in pi
    pc = " ".join(rt.producer("h264", have=lambda e: e == "x264enc"))
    assert "x264enc" in pc and "v4l2h264enc" not in pc
    with pytest.raises(RuntimeError, match="no H.264 encoder"):
        rt.producer("h264", have=lambda e: False)
    assert "a=rtpmap:26 JPEG/90000" in rt.sdp("jpeg") and "a=rtpmap:96 H264/90000" in rt.sdp("h264")


FAKE_GEN = [sys.executable, "-c", (
    "import sys, time\n"
    "pkt = bytes([0x80, 26, 0, 1]) + bytes(8) + bytes([0, 0, 0, 0, 1, 50, 80, 60]) + bytes(100)\n"
    "while True:\n"
    "    sys.stdout.buffer.write(len(pkt).to_bytes(2, 'big') + pkt); sys.stdout.flush(); time.sleep(0.02)\n")]


def rtsp_play(port):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    url = f"rtsp://127.0.0.1:{port}/cam"
    for n, (m, extra) in enumerate((("OPTIONS", ""), ("DESCRIBE", ""),
                                    ("SETUP", "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n"), ("PLAY", ""))):
        s.sendall(f"{m} {url} RTSP/1.0\r\nCSeq: {n + 1}\r\n{extra}\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += s.recv(1)
        assert head.startswith(b"RTSP/1.0 200 OK"), head
        n_body = int(next((x.split(b":")[1] for x in head.split(b"\r\n") if x.lower().startswith(b"content-length")),
                          b"0"))
        body = b""
        while len(body) < n_body:
            body += s.recv(n_body - len(body))
        if m == "DESCRIBE":
            assert b"JPEG/90000" in body
    return s


def test_session_outage_and_return_without_leaks():
    srv = rt.Server("127.0.0.1", 0, "jpeg", FAKE_GEN).start()
    port = srv.port
    try:
        s = rtsp_play(port)
        data = b""
        while len(data) < 2000:
            data += s.recv(65536)
        assert data[:2] == b"$\x00" and data[4] == 0x80 and data[5] & 0x7F == 26     # RTP/JPEG по каналу 0
        assert len(srv.procs) == 1
        srv.stop()                                                                 # «падение» сервера
        s.settimeout(3)
        try:
            while s.recv(65536):
                pass
        except OSError:
            pass
        assert not srv.procs and not srv.clients
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
        srv.start()                                                               # возврат на том же порту
        assert srv.port == port
        s2 = rtsp_play(port)
        assert s2.recv(65536)[:2] == b"$\x00"
        s2.close()
        s.close()
    finally:
        srv.stop()
    time.sleep(0.2)
    assert not srv.procs


def _gst_ok():
    from sheregesh import video
    return shutil.which("gst-launch-1.0") and all(video.has_element(e) for e in
                                                  ("rtspsrc", "rtpjpegdepay", "rtpjpegpay", "jpegenc", "rtpstreampay"))


@pytest.mark.skipif(not _gst_ok(), reason="нет GStreamer с rtspsrc/jpegenc")
def test_real_rtspsrc_through_remote_ingest_loss_and_return():
    """Генератор инструмента (с таймкодом) → RTSP → rtspsrc видеоприёма блока: streaming; сервер упал →
    connect-error; вернулся → streaming."""
    from sheregesh import video
    from sheregesh import videoinput as vi
    from sheregesh.videoingest import Ingest
    srv = rt.Server("127.0.0.1", 0, "jpeg", rt.producer("jpeg", 320, 240, 15)).start()
    url = f"rtsp://127.0.0.1:{srv.port}/cam"
    ing = Ingest(lambda: vi.RTSPSource(url, "mjpeg", latency_ms=50, timeout_s=3),
                 lambda s: video.stream_sender_args("jpeg", src=s), no_signal_s=1.0, start_timeout_s=10,
                 stall_restart_s=6, presence_s=0.3, max_backoff_s=1.0).start()
    tok = ing.attach()
    stop = threading.Event()

    def drain():
        import select
        while not stop.is_set():
            select.select([ing.wake_fd], [], [], 0.1)
            ing.take()
    t = threading.Thread(target=drain, daemon=True)
    t.start()

    def wait(states, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end and ing.state not in states:
            time.sleep(0.1)
        return ing.state in states
    try:
        assert wait(("streaming",), 20), (ing.state, ing.error)
        time.sleep(1.5)
        st = ing.status()
        assert (st["stream_width"], st["stream_height"]) == (320, 240) and 8 <= st["fps_actual"] <= 20, st
        srv.stop()
        assert wait(("connect-error",), 20), (ing.state, ing.error)
        srv.start()
        assert wait(("streaming",), 25), (ing.state, ing.error)
    finally:
        stop.set()
        t.join(2)
        ing.detach(tok)
        ing.close()
        srv.stop()
