# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Видео, новые страницы и GUI с отдельным процессом удалённого блока (offscreen, L3).

Видео — только синтетический генератор GStreamer на 127.0.0.1. Аппаратное декодирование Pi 4 (L4) не проверяется.
"""
import hashlib
import signal
import subprocess
import sys
import time

import pytest
from PySide6.QtCore import QElapsedTimer, QRectF, QTimer
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from procs import Agent, free_port
from sheregesh import video as gst
from sheregesh_console.qtwait import pump, wait_until
from sheregesh_console.app import PAGE_NAMES, Ui, compile_translations
from sheregesh_console.backend import Backend

QQuickStyle.setStyle("Fusion")
needs_gst = pytest.mark.skipif(gst.choose_codec() is None, reason="нет работающего кодека GStreamer")


@pytest.fixture
def make_ui(qapp, tmp_path):
    made = []

    def make(**kw):
        compile_translations()
        u = Ui(Backend(tmp_path / f"data{len(made)}", autostart=True, **kw))
        made.append(u)
        return u
    yield make
    for u in made:
        u.close()


def obj(u, name):
    from PySide6.QtCore import QObject
    o = u.window.findChild(QObject, name)
    assert o is not None, name
    return o


def show(u, name):
    u.window.setProperty("currentPage", PAGE_NAMES.index(name))
    pump(30)


def lit_fraction(u, item):
    """Доля «нецветных чёрных» пикселей в области элемента — признак отображаемого кадра."""
    img = u.window.grabWindow()
    r = item.mapRectToScene(QRectF(0, 0, item.width(), item.height())).toRect()
    sub = img.copy(r)
    step = max(1, sub.width() // 60)
    total = lit = 0
    for x in range(0, sub.width(), step):
        for y in range(0, sub.height(), step):
            total += 1
            c = sub.pixelColor(x, y)
            lit += (c.red() + c.green() + c.blue()) > 60
    return lit / max(total, 1)


class Latency:
    """Измеряет максимальную паузу событийного цикла GUI (зависание = большая пауза)."""

    def __init__(self):
        self.timer, self.clock, self.worst = QTimer(), QElapsedTimer(), 0
        self.timer.setInterval(20)
        self.timer.timeout.connect(self._tick)
        self.clock.start()
        self.timer.start()

    def _tick(self):
        self.worst = max(self.worst, self.clock.restart())

    def stop(self):
        self.timer.stop()
        return self.worst


# ---------------- видео ----------------
@needs_gst
@pytest.mark.test_id("TEST-SW-002")
def test_synthetic_video_displayed_resized_and_survives_tab_switch(make_ui, qtbot):
    u = make_ui(local_video=True)
    rx = u.backend.video
    show(u, "video")
    assert wait_until(lambda: rx.state == "playing", 8)
    view = obj(u, "videoView")
    pump(2500)
    assert rx.fps >= 15, f"частота отображения {rx.fps} к/с при генераторе 30 к/с"
    assert lit_fraction(u, view) > 0.5, "кадр не отображается"
    assert not obj(u, "videoPlaceholder").property("visible")
    frames0 = rx.frames
    for name in ("dashboard", "settings", "video", "logs", "video"):
        show(u, name)
    assert wait_until(lambda: rx.frames > frames0 + 10, 5)
    assert rx.state == "playing"
    for w, h in ((1280, 800), (1920, 1200)):
        u.window.resize(w, h)
        pump(150)
        assert abs(view.width() - (w - 2 * 16 - 4)) < 40, "видео должно следовать за размером окна"
        assert lit_fraction(u, view) > 0.3


@needs_gst
@pytest.mark.test_id("TEST-SW-002")
def test_no_video_then_recovery(make_ui, qtbot):
    codec = gst.choose_codec()
    port = free_port(kind=2)        # SOCK_DGRAM
    u = make_ui()
    rx = u.backend.video
    rx.configure(codec, port)
    show(u, "video")
    assert wait_until(lambda: rx.state == "no-video", 6)
    assert obj(u, "videoPlaceholder").property("visible")
    assert obj(u, "videoStateText").property("text") == "Нет видео"
    sender = subprocess.Popen(gst.sender_args(codec, port, 640, 360, 25), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        assert wait_until(lambda: rx.state == "playing", 8)
        sender.terminate()
        sender.wait(5)
        assert wait_until(lambda: rx.state == "no-video", 5)
    finally:
        if sender.poll() is None:
            sender.kill()


@pytest.mark.test_id("TEST-SW-002")
@pytest.mark.parametrize("command", [
    [sys.executable, "-c", "import time; time.sleep(60)"],                                       # декодер завис
    [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 1000); sys.stdout.flush()"],  # обрывок кадра
    ["false"],                                                                                    # декодер падает
], ids=["hang", "partial-frame", "crash"])
def test_video_failure_does_not_freeze_gui(make_ui, qtbot, command):
    u = make_ui(video_command=command)
    rx = u.backend.video
    rx.configure("jpeg", 5999)
    show(u, "video")
    lat = Latency()
    t_end = time.monotonic() + 4
    while time.monotonic() < t_end:
        pump(50)
    worst = lat.stop()
    assert worst < 300, f"событийный цикл GUI стоял {worst} мс"
    assert rx.state in ("no-video", "restarting", "waiting")
    if command == ["false"]:
        assert rx.restarts >= 1
    show(u, "dashboard")
    assert obj(u, "overallLabel").property("text")      # остальной интерфейс работает


def test_video_unavailable_without_gstreamer(make_ui, monkeypatch):
    monkeypatch.setattr(gst, "available", lambda: False)
    u = make_ui()
    u.backend.video.configure("jpeg", 5998)
    show(u, "video")
    assert u.backend.video.state == "unavailable"
    assert obj(u, "videoStateText").property("text") == "Видео недоступно"


# ---------------- страницы ----------------
def test_full_event_and_log_history_keeps_gui_responsive(make_ui):
    """Регрессия 60-минутного прогона: при заполненных журналах (500 событий, 2000 записей) такт GUI
    занимал до 7,5 с — QML фильтровал список-свойство, перечитывая его из Python на каждый элемент."""
    u = make_ui()
    b = u.backend
    for i in range(600):
        b.bench.monitor.log.raise_(b.bench.clock(), "LINK-BAD-MSG", reason="fields", count=i)
    for i in range(2100):
        b.log("info", "test", f"record {i}")
    worst = 0.0
    for page in ("diagnostics", "logs", "dashboard"):
        show(u, page)
        for k in range(3):
            b.bench.monitor.log.raise_(b.bench.clock(), "LINK-BAD-MSG", reason="json", count=k)
            b.log("warning", "test", f"new {k}")
            t = time.perf_counter()
            b.tick()
            pump(1)
            worst = max(worst, time.perf_counter() - t)
    assert b.eventCount == 500 and b.logCount == 2000
    assert worst < 0.5, f"такт GUI {worst * 1000:.0f} мс"
    show(u, "diagnostics")
    rows = obj(u, "eventsList").property("count")
    assert rows == 500


@pytest.mark.test_id("TEST-SW-005")
@pytest.mark.test_id("TEST-SW-001")
def test_logs_page_and_export_without_secrets(make_ui):
    u = make_ui()
    canary = "SHRG-CANARY-LOG-" + "4f1c2d9a7b"
    u.backend.log("error", "test", f"diagnostic dump token={canary}")
    u.backend.log("warning", "test", "-----BEGIN OPENSSH " + "PRIVATE KEY-----")
    show(u, "logs")
    pump(600)
    assert "записей" in obj(u, "logCount").property("text")
    name = u.backend.exportLogs()
    data = (u.backend.usb_dir / name).read_bytes()
    assert canary.encode() not in data and b"PRIVATE KEY" not in data and b"[REDACTED]" in data
    digest = (u.backend.usb_dir / f"{name}.sha256").read_text().split()[0]
    assert digest == hashlib.sha256(data).hexdigest()


@pytest.mark.test_id("TEST-SYS-007")
def test_about_page_shows_passport(make_ui):
    from sheregesh import __version__
    u = make_ui()
    show(u, "about")
    assert obj(u, "aboutSoftware").property("text") == __version__
    assert obj(u, "aboutDevice").property("text") == "Шерегеш-пульт"


@pytest.mark.test_id("TEST-PWR-007")
def test_power_page_states(make_ui):
    u = make_ui()
    show(u, "power")
    assert obj(u, "consolePowerValue").property("text").endswith("%")
    assert obj(u, "remotePowerValue").property("text") == "Нет данных"   # блок эмулируется в процессе
    u.backend.bench.power.set_available(False)
    pump(700)
    assert obj(u, "consolePowerValue").property("text") == "Нет данных"


# ---------------- GUI + отдельный процесс удалённого блока ----------------
@pytest.mark.test_id("TEST-SW-004")
@pytest.mark.test_id("TEST-SYS-002")
def test_gui_with_remote_process_crash_and_restart(make_ui, qtbot):
    video_port = free_port(kind=2)
    args = ["--video", "--video-port", str(video_port), "--video-width", "640", "--video-height", "360",
            "--video-fps", "15"] if gst.choose_codec() else []
    agent = Agent(*args, *(["--fault", "8:video-off:6"] if args else []))
    try:
        u = make_ui(peer=("127.0.0.1", agent.port))
        assert wait_until(lambda: u.backend.status["peerState"] == "connected", 8)
        assert wait_until(lambda: obj(u, "remoteTelemetry").property("visible"), 5)
        assert obj(u, "peerValue").property("text") == "Связь есть"
        if args:
            assert wait_until(lambda: u.backend.video.state == "playing", 10)
            # неисправность блока video-off (8–14 с): кадров нет → событие VIDEO-LOST, затем снятие
            assert wait_until(lambda: any(e["code"] == "VIDEO-LOST" for e in u.backend.activeEvents), 15)
            assert wait_until(lambda: not any(e["code"] == "VIDEO-LOST" for e in u.backend.activeEvents), 15)
        agent.signal(signal.SIGKILL)
        agent.proc.wait(5)
        assert wait_until(lambda: u.backend.status["peerState"] == "lost", 8)
        assert obj(u, "peerValue").property("text") == "Нет связи"
        assert wait_until(lambda: "не запущен" in obj(u, "peerDetail").property("text"), 8)
        if args:
            assert wait_until(lambda: u.backend.video.state == "no-video", 6)
        agent2 = Agent(*args, port=agent.port)
        try:
            assert wait_until(lambda: u.backend.status["peerState"] == "connected", 12)
            if args:
                assert wait_until(lambda: u.backend.video.state == "playing", 10)
        finally:
            agent2.stop()
    finally:
        agent.stop()
