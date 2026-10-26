# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Кадр видео мимо Python (ADR-0015): конвейер декодера с unixfdsink и учёт кадров VideoReceiver на пути GL.
Приёмник GL подменный (на рабочей станции и в CI нет qml6glsink): проверяется выбор пути, учёт кадров, состояние,
остановка и перезапуск по сбою приёмника. Сам qml6glsink и dmabuf проверяются только на плате."""
import os
import sys

import pytest

from sheregesh import video as gst
from sheregesh_console import video as V
from sheregesh_console.qtwait import pump, wait_until


class FakeGl:
    def __init__(self, ready=True):
        self.is_ready, self.started, self.stopped = ready, [], 0

    def ready(self):
        return self.is_ready

    def start(self, path, on_frame, on_error):
        self.started.append(path)
        self.on_frame, self.on_error = on_frame, on_error

    def stop(self):
        self.stopped += 1


@pytest.fixture(autouse=True)
def hw_decoder(monkeypatch):
    """Аппаратный декодер платы (на рабочей станции и в CI его нет): конвейеры строятся как на Pi 4."""
    monkeypatch.setattr(gst, "h264_elements", lambda: (["v4l2h264enc"], "v4l2h264dec"))


def test_gl_decoder_pipelines_end_in_unixfdsink():
    a = gst.stream_receiver_gl_args("h264", "/run/x.sock")
    assert a[:4] == [gst.GST_LAUNCH, "-q", "fdsrc", "fd=0"] and a[-3:] == ["unixfdsink", "socket-path=/run/x.sock",
                                                                          "sync=false"]
    assert "videoconvert" not in a and "fdsink" not in a and "rtph264depay" in a
    b = gst.receiver_gl_args("h264", 5600, "/run/y.sock")
    assert "port=5600" in b and b[-2] == "socket-path=/run/y.sock"


RECEIVERS = []


@pytest.fixture(autouse=True)
def shutdown_receivers():
    """Таймер и потоки приёмника — до конца теста: иначе после теста он перезапускает конвейер уже без подмен."""
    yield
    while RECEIVERS:
        RECEIVERS.pop().shutdown()


def _receiver(monkeypatch, tmp_path, gl):
    spawned = []
    monkeypatch.setattr(gst, "available", lambda: True)
    monkeypatch.setattr(gst, "gl_capable", lambda codec: codec == "h264")
    monkeypatch.setattr(gst, "spawn", lambda args, **kw: spawned.append((args, kw)) or
                        __import__("subprocess").Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                                       start_new_session=True))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    rx = V.VideoReceiver()
    rx.gl = gl
    RECEIVERS.append(rx)
    return rx, spawned


def test_gl_path_counts_frames_and_stops(qapp, monkeypatch, tmp_path):
    gl = FakeGl()
    rx, spawned = _receiver(monkeypatch, tmp_path, gl)
    rx.configure("h264", 5600)
    try:
        args, kw = spawned[-1]
        assert args[-3] == "unixfdsink" and kw["stdout"] == __import__("subprocess").DEVNULL
        assert rx.glActive and rx.glAvailable and gl.started and gl.started[0].startswith(str(tmp_path))
        for _ in range(20):
            gl.on_frame(__import__("time").monotonic())
            pump(30)
        assert wait_until(lambda: rx.state == "playing", 2), rx.state
        assert rx.fps > 5 and rx.image is None and rx.metrics()["frames_decoded"] == 20
    finally:
        rx.stop()
    assert gl.stopped == 1 and not rx.glActive and not os.path.exists(gl.started[0])


def test_gl_not_used_without_ready_item_or_for_jpeg(qapp, monkeypatch, tmp_path):
    rx, spawned = _receiver(monkeypatch, tmp_path, FakeGl(ready=False))
    rx.configure("h264", 5600)
    rx.stop()
    assert spawned[-1][0][-1] == "sync=false" and "fdsink" in spawned[-1][0] and not rx.glActive
    rx2, spawned2 = _receiver(monkeypatch, tmp_path, FakeGl())
    rx2.configure("jpeg", 5601)
    rx2.stop()
    assert "unixfdsink" not in spawned2[-1][0]


def test_gl_error_kills_decoder_for_restart(qapp, monkeypatch, tmp_path):
    gl = FakeGl()
    rx, _ = _receiver(monkeypatch, tmp_path, gl)
    rx.configure("h264", 5600)
    proc = rx._proc
    try:
        gl.on_error("gl video: test")
        assert wait_until(lambda: proc.poll() is not None, 3)
        assert wait_until(lambda: rx.state == "restarting", 3), rx.state
    finally:
        rx.stop()


def test_switches_to_gl_when_window_becomes_ready_and_back(qapp, monkeypatch, tmp_path):
    """Окно появилось на экране после старта приёмника → перезапуск на путь GL; дисплей пропал → прежний путь."""
    gl = FakeGl(ready=False)
    rx, spawned = _receiver(monkeypatch, tmp_path, gl)
    rx.configure("h264", 5600)
    assert not rx.glActive and "unixfdsink" not in spawned[-1][0]
    gl.is_ready = True
    assert wait_until(lambda: rx.glActive, 3) and spawned[-1][0][-3] == "unixfdsink"
    gl.is_ready = False
    assert wait_until(lambda: not rx.glActive, 3) and "unixfdsink" not in spawned[-1][0]
