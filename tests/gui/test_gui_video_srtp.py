# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Режим «служба» VideoReceiver (ADR-0016, транспорт SRTP): кадры отдаёт служба sheregesh-video-rx в unixfdsink.

Проверяется на рабочей станции и в CI без дисплея и без gst: приёмник GL подменный (FakeGl), конвейер RGBx — подменный
spawn, который запускает обычный процесс Python и пишет в его stdout кадры 640×360 RGBx. Что проверяется:
выбор пути (GL или RGBx), ожидание сокета службы, учёт кадров и метрик канала, остановка без удаления чужого сокета,
переключение GL↔RGBx при готовности окна, перезапуск по сбою приёмника GL, сброс режима службы другими configure-вызовами.
Реальные qml6glsink, unixfdsrc и dmabuf проверяются только на плате.
"""
import os
import subprocess
import sys
import threading
import time

import pytest

from sheregesh import video as gst
from sheregesh_console import video as V
from sheregesh_console.qtwait import pump, wait_until

LINK = {"rtt_ms": 4.2, "lost": 0}          # метрики канала, которые отдаёт служба (videorx.VideoRx.stats)


def _stats():
    return dict(LINK)


# Подменный декодер RGBx: пишет кадры нужного размера раз в 0.05 с (как gst-launch с fdsink fd=1).
WRITER = ("import sys, time\n"
          "frame = b'\\x10' * (640 * 360 * 4)\n"
          "while True:\n"
          "    sys.stdout.buffer.write(frame)\n"
          "    sys.stdout.buffer.flush()\n"
          "    time.sleep(0.05)\n")


class FakeGl:
    """Подменный приёмник GL (на рабочей станции и в CI нет qml6glsink): учитывает запуск и остановку."""

    def __init__(self, ready=True):
        self.is_ready, self.started, self.stopped = ready, [], 0

    def ready(self):
        return self.is_ready

    def start(self, path, on_frame, on_error):
        self.started.append(path)
        self.on_frame, self.on_error = on_frame, on_error

    def stop(self):
        self.stopped += 1


class FakeClient:
    """Подменный канал удалённого блока: работает, пока его не закроют (для проверки режима TLS после сброса)."""

    info = {}

    def __init__(self):
        self._closed = threading.Event()

    def run(self, fd):
        self._closed.wait()
        return "closed"

    def stats(self):
        return {}

    def frame_decoded(self, now):
        pass

    def close(self):
        self._closed.set()


@pytest.fixture(autouse=True)
def hw_decoder(monkeypatch):
    """Аппаратный декодер платы (на рабочей станции и в CI его нет): конвейеры строятся как на Pi 4."""
    monkeypatch.setattr(gst, "h264_elements", lambda: (["v4l2h264enc"], "v4l2h264dec"))


RECEIVERS = []


@pytest.fixture(autouse=True)
def shutdown_receivers():
    """Таймер и потоки приёмника — до конца теста: иначе после теста он перезапускает конвейер уже без подмен."""
    yield
    while RECEIVERS:
        RECEIVERS.pop().shutdown()


def _receiver(monkeypatch, tmp_path, gl, cmd=None):
    """Приёмник с подменами gst. cmd — команда процесса, который spawn запускает вместо декодера (по умолчанию — сон)."""
    spawned = []
    proc_cmd = cmd or [sys.executable, "-c", "import time; time.sleep(60)"]

    def fake_spawn(args, **kw):
        spawned.append((args, kw))
        return subprocess.Popen(proc_cmd, start_new_session=True, **kw)

    monkeypatch.setattr(gst, "available", lambda: True)
    monkeypatch.setattr(gst, "gl_capable", lambda codec: codec == "h264")
    monkeypatch.setattr(gst, "spawn", fake_spawn)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    rx = V.VideoReceiver()
    rx.gl = gl
    RECEIVERS.append(rx)
    return rx, spawned


def _sock(tmp_path, name="srtp-rx.sock"):
    """Сокет кадров службы: обычный пустой файл — для проверки, что приёмник его находит и не трогает."""
    path = tmp_path / name
    path.touch()
    return str(path)


def test_service_waits_for_socket_then_starts_gl(qapp, monkeypatch, tmp_path):
    """Сокета службы ещё нет — процесс не запускается, состояние «ожидание» без ошибки; появился — путь GL."""
    gl = FakeGl()
    rx, spawned = _receiver(monkeypatch, tmp_path, gl)
    sock = str(tmp_path / "srtp-rx.sock")
    rx.configure_service("h264", sock, _stats)
    try:
        assert not os.path.exists(sock)
        assert rx.state == "waiting" and rx.error == "" and rx._svc_pending is True
        assert gl.started == [] and spawned == []
        open(sock, "w").close()
        rx._next_start = 0.0                    # в боевом коде — не раньше чем через SERVICE_RETRY_S
        rx._poll()
        assert gl.started == [sock] and spawned == []
        assert rx.glActive is True and rx._svc_pending is False
    finally:
        rx.stop()


def test_service_gl_path_counts_frames_and_keeps_service_socket(qapp, monkeypatch, tmp_path):
    """Путь GL: процесса нет, кадры учитываются, метрики режима службы; stop() не удаляет чужой сокет."""
    gl = FakeGl()
    rx, spawned = _receiver(monkeypatch, tmp_path, gl)
    sock = _sock(tmp_path)
    rx.configure_service("h264", sock, _stats)
    try:
        assert spawned == [] and rx._proc is None
        assert rx.glActive is True and gl.started[-1] == sock
        m = rx.metrics()
        assert m["mode"] == "remote-srtp" and m["transport"] == "srtp" and m["link"] == LINK
        for _ in range(20):
            gl.on_frame(time.monotonic())
            pump(30)
        assert wait_until(lambda: rx.state == "playing", 2), rx.state
        m = rx.metrics()
        assert m["frames_decoded"] == 20 and m["decoded_fps_5s"] > 0
    finally:
        rx.stop()
    assert gl.stopped == 1 and rx.glActive is False
    assert os.path.exists(sock)


def test_service_rgbx_path_spawns_gst_and_reads_frames(qapp, monkeypatch, tmp_path):
    """Путь RGBx (окна нет): один запуск gst-launch unixfdsrc → RGBx → stdout, кадры дочернего процесса."""
    gl = FakeGl(ready=False)
    rx, spawned = _receiver(monkeypatch, tmp_path, gl, cmd=[sys.executable, "-c", WRITER])
    sock = _sock(tmp_path)
    rx.configure_service("h264", sock, _stats)
    try:
        assert len(spawned) == 1
        args, kw = spawned[0]
        assert args == V.service_rgbx_args(sock, V.FRAME_W, V.FRAME_H)
        assert kw["stdout"] == subprocess.PIPE
        assert rx.glActive is False and gl.started == []
        assert wait_until(lambda: rx.state == "playing" and rx.metrics()["frames_decoded"] > 3, 5), rx.state
        assert rx.image is not None
        assert rx.metrics()["mode"] == "remote-srtp"
    finally:
        rx.stop()
    assert os.path.exists(sock)


def test_service_switches_between_gl_and_rgbx(qapp, monkeypatch, tmp_path):
    """Окно появилось — перезапуск на путь GL (процесс RGBx остановлен); окно пропало — снова RGBx."""
    gl = FakeGl(ready=False)
    rx, spawned = _receiver(monkeypatch, tmp_path, gl, cmd=[sys.executable, "-c", WRITER])
    sock = _sock(tmp_path)
    rx.configure_service("h264", sock, _stats)
    try:
        assert len(spawned) == 1 and rx.glActive is False

        gl.is_ready = True
        rx._gl_check_at = 0.0
        rx._poll()
        assert rx.glActive is True and gl.started and gl.started[-1] == sock
        assert rx._proc is None

        gl.is_ready = False
        rx._gl_check_at = 0.0
        rx._poll()
        assert rx.glActive is False and len(spawned) == 2
        assert rx._proc is not None
    finally:
        rx.stop()


def test_service_gl_end_of_stream_restarts_receiver(qapp, monkeypatch, tmp_path):
    """Сбой приёмника GL: декодера нет, поэтому _poll переводит в «перезапуск» и считает перезапуск."""
    gl = FakeGl()
    rx, spawned = _receiver(monkeypatch, tmp_path, gl)
    rx.configure_service("h264", _sock(tmp_path), _stats)
    try:
        gl.on_error("gl video: end of stream")
        assert rx._svc_dead is True
        rx._poll()
        assert wait_until(lambda: rx.state == "restarting", 1), rx.state
        assert rx.metrics()["restarts"] == 1
        assert rx._svc_dead is False
    finally:
        rx.stop()


def test_service_decoder_socket_error_is_not_reported(qapp, monkeypatch, tmp_path):
    """«Сокета декодера ещё нет» — ожидание службы, а не ошибка: текст ошибки остаётся пустым."""
    gl = FakeGl()
    rx, spawned = _receiver(monkeypatch, tmp_path, gl)
    rx.configure_service("h264", _sock(tmp_path), _stats)
    try:
        gl.on_error("decoder socket x did not appear")
        assert rx.error == ""
        rx._poll()
        assert rx.error == ""
    finally:
        rx.stop()


def test_configure_none_or_remote_resets_service_mode(qapp, monkeypatch, tmp_path):
    """configure(None, None) и configure_remote снимают режим службы; повторный configure_service с теми же
    аргументами ничего не перезапускает."""
    gl = FakeGl(ready=False)
    rx, spawned = _receiver(monkeypatch, tmp_path, gl)
    sock = _sock(tmp_path)
    rx.configure_service("h264", sock, _stats)
    assert rx.metrics()["mode"] == "remote-srtp"
    rx.configure(None, None)
    try:
        assert rx.metrics()["mode"] != "remote-srtp"
        assert rx.state == "off"
    finally:
        rx.stop()

    rx2, spawned2 = _receiver(monkeypatch, tmp_path, FakeGl(ready=False))
    rx2.configure_service("h264", sock, _stats)
    rx2.configure_remote("h264", ("videolink", 5600), FakeClient)
    try:
        assert rx2.metrics()["mode"] == "remote-tls"
        assert rx2.metrics()["mode"] != "remote-srtp"
    finally:
        rx2.stop()


def test_configure_service_same_args_does_not_restart(qapp, monkeypatch, tmp_path):
    """Повторный configure_service с теми же аргументами (сеанс уже идёт) не перезапускает приёмник."""
    gl = FakeGl()
    rx, spawned = _receiver(monkeypatch, tmp_path, gl)
    sock = _sock(tmp_path)
    rx.configure_service("h264", sock, _stats)
    try:
        assert len(gl.started) == 1
        rx.configure_service("h264", sock, _stats)
        assert len(gl.started) == 1 and spawned == []
        assert rx.glActive is True
    finally:
        rx.stop()


def test_service_rgbx_args_take_dmabuf(monkeypatch):
    """Кадры службы — dmabuf (DMA_DRM): без программного videoscale; v4l2convert (ISP), иначе GL без окна."""
    monkeypatch.setattr(gst, "has_element", lambda name: name == "v4l2convert")
    isp = V.service_rgbx_args("/run/x.sock", 640, 360)
    assert "v4l2convert" in isp and "output-io-mode=dmabuf-import" in isp and "videoscale" not in isp
    assert "video/x-raw,format=RGBx,width=640,height=360" in isp and isp[-3:] == ["fdsink", "fd=1", "sync=false"]
    monkeypatch.setattr(gst, "has_element", lambda name: False)
    gl = V.service_rgbx_args("/run/x.sock", 640, 360)
    assert gl[:3] == ["env", "GST_GL_WINDOW=surfaceless", "GST_GL_PLATFORM=egl"] and "glupload" in gl
    assert "videoscale" not in gl and "video/x-raw,format=RGBx,width=640,height=360" in gl

