# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S3.3, укрепление (offscreen, L3): исчезновение устройства во время захвата, сторож зависшего захвата и группы
процессов, восстановление после ошибок open/ioctl/stdout, GUI без внешнего устройства, регрессия синтетического режима.

Устройства и процесс захвата подставные (tests/system/fake_v4l2.py, tests/system/fake_capture.py); настоящий GStreamer —
только в синтетической регрессии, с пропуском при отсутствии. Настоящая камера (L4) здесь НЕ проверяется.
"""
import errno
import os
import sys
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QElapsedTimer, QObject, QTimer
from PySide6.QtQuickControls2 import QQuickStyle

from fake_v4l2 import FakeV4L2
from sheregesh import video as gst
from sheregesh import videoinput as vi
from sheregesh_console import video as vmod
from sheregesh_console.app import PAGE_NAMES, Ui, compile_translations
from sheregesh_console.backend import Backend
from sheregesh_console.qtwait import finish, pump, wait_until
from sheregesh_console.video import VideoReceiver

QQuickStyle.setStyle("Fusion")
SYS = Path(__file__).resolve().parents[1] / "system"
KEY = ("", "1280x720@30", "auto")
ADAPTER = dict(driver="uvcvideo", name="USB Video: USB Video", usb=("534d", "2109", ""),
               formats=[("MJPG", "discrete", [((1280, 720), [30])])])


def capture_cmd(pidfile, frames=-1, after="exit", fps=30):
    return [sys.executable, str(SYS / "fake_capture.py"), str(pidfile), str(frames), after, str(fps)]


def pids(pidfile):
    return [int(x) for x in Path(pidfile).read_text().split()] if Path(pidfile).exists() else []


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Latency:
    """Наибольшая пауза событийного цикла GUI (зависание интерфейса = большая пауза)."""

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


@pytest.fixture
def v4l2(tmp_path):
    return FakeV4L2(tmp_path / "hw")


@pytest.fixture
def receivers(qapp):
    made = []

    def make(command):
        rx = VideoReceiver(command=command)
        made.append(rx)
        return rx
    yield make
    for rx in made:
        rx.shutdown()


def opener(v4l2, device="", mode="1280x720@30", fmt="auto"):
    return lambda: vi.open_uvc(device, mode, fmt, **v4l2.scan_kw())


def unplug(v4l2):
    for n in ("video0", "video1"):
        if n in v4l2.devices:
            v4l2.remove(n)


# ---------------- 2. исчезновение во время захвата, появление с другим /dev/videoN ----------------
def test_unplug_while_capture_hangs_then_replug_as_other_node(receivers, v4l2, tmp_path, monkeypatch):
    """v4l2src не всегда завершается при отключении — может просто перестать отдавать кадры. Сторож убивает захват,
    повторный поиск даёт «нет устройства», после подключения под другим номером — снова кадры с нового узла."""
    monkeypatch.setattr(vmod, "STALL_RESTART_S", 1.0)
    pidfile = tmp_path / "pids"
    rx = receivers(capture_cmd(pidfile, frames=15, after="hang"))
    rx.configure_local(KEY, opener(v4l2.pi4()))
    assert wait_until(lambda: rx.state == "playing", 5), rx.metrics()
    unplug(v4l2)
    assert wait_until(lambda: rx.state == "no-device", 10), rx.metrics()
    assert rx.restarts >= 1 and "not found" in rx.error
    assert wait_until(lambda: not any(alive(p) for p in pids(pidfile)), 3)    # зависший захват не брошен
    v4l2.add("video4", **ADAPTER, by_id="usb-MACROSILICON_USB_Video-video-index0")
    assert wait_until(lambda: rx.state == "playing", vmod.RESCAN_S + 5), rx.metrics()
    assert rx._source.node.name == "video4" and rx._source.node.dev.endswith("by-id/usb-MACROSILICON_USB_Video-video-index0")
    assert v4l2.aux_opened == []


def test_unplug_during_rescan_enum_fmt(receivers, v4l2, tmp_path):
    """Устройство исчезает посреди опроса при перезапуске (ENODEV на VIDIOC_ENUM_FMT): «нет устройства» с причиной,
    затем поиск продолжается и находит его снова."""
    pidfile = tmp_path / "pids"
    rx = receivers(capture_cmd(pidfile, frames=10))                          # захват завершается, как при отключении
    rx.configure_local(KEY, opener(v4l2.pi4()))
    assert wait_until(lambda: rx.state == "playing", 5)
    fired = []

    def vanish(name, req):
        if req == vi.VIDIOC_ENUM_FMT and not fired:
            fired.append(name)
            unplug(v4l2)
    v4l2.hooks.append(vanish)
    assert wait_until(lambda: rx.state == "no-device", 10), rx.metrics()
    assert fired == ["video0"] and "V4L2 query failed" in rx.error
    v4l2.add("video2", **ADAPTER)
    assert wait_until(lambda: rx.state == "playing" and rx._source.node.name == "video2", vmod.RESCAN_S + 5), rx.metrics()


# ---------------- 4. зависший захват, сторож, группы процессов ----------------
def test_capture_runs_in_own_process_group_and_watchdog_kills_whole_group(receivers, v4l2, tmp_path, monkeypatch):
    monkeypatch.setattr(vmod, "STALL_RESTART_S", 1.0)
    pidfile = tmp_path / "pids"
    rx = receivers(capture_cmd(pidfile, frames=15, after="hang-child"))
    rx.configure_local(KEY, opener(v4l2.pi4()))
    assert wait_until(lambda: rx.state == "playing", 5)
    assert wait_until(lambda: len(pids(pidfile)) >= 2, 5)                   # захват и его внук
    capture, child = pids(pidfile)[:2]
    pgid = rx._proc.pid
    assert os.getpgid(capture) == os.getpgid(child) == pgid != os.getpgid(0)   # своя группа, лидер — запущенный процесс
    assert wait_until(lambda: rx.restarts >= 1, 8), rx.metrics()
    assert wait_until(lambda: not group_alive(pgid) and not alive(capture) and not alive(child), 5)
    assert wait_until(lambda: rx.state == "playing", 8)                     # новый захват после сторожа


def test_capture_hung_from_start_is_restarted(receivers, v4l2, tmp_path, monkeypatch):
    """Кадров нет совсем (устройство открылось, поток не пошёл): waiting → no-video → перезапуск, процессы не копятся."""
    monkeypatch.setattr(vmod, "STALL_RESTART_S", 1.0)
    pidfile = tmp_path / "pids"
    rx = receivers(capture_cmd(pidfile, frames=0, after="hang"))
    rx.configure_local(KEY, opener(v4l2.pi4()))
    assert rx.state == "waiting"
    seen = set()
    lat = Latency()
    end = time.monotonic() + 15
    while rx.restarts < 2 and time.monotonic() < end:
        seen.add(rx.state)
        assert sum(alive(p) for p in pids(pidfile)) <= 2, pids(pidfile)    # убитый может миг побыть зомби
        pump(50)
    assert rx.restarts >= 2 and {"waiting", "no-video"} <= seen, (seen, rx.metrics())
    assert lat.stop() < 500
    assert wait_until(lambda: not any(alive(p) for p in pids(pidfile)[:-1]), 3)   # жив только текущий захват
    rx.shutdown()
    assert not any(alive(p) for p in pids(pidfile))


def test_shutdown_during_hang_leaves_no_orphans(receivers, v4l2, tmp_path):
    pidfile = tmp_path / "pids"
    rx = receivers(capture_cmd(pidfile, frames=5, after="hang-child"))
    rx.configure_local(KEY, opener(v4l2.pi4()))
    assert wait_until(lambda: len(pids(pidfile)) >= 2, 5)
    pgid = rx._proc.pid
    rx.shutdown()
    assert wait_until(lambda: not group_alive(pgid) and not any(alive(p) for p in pids(pidfile)), 5)


# ---------------- 5. восстановление после ошибок ----------------
@pytest.mark.parametrize("code", [errno.EACCES, errno.EBUSY, errno.ENODEV])
def test_open_error_shows_reason_and_recovers(receivers, v4l2, tmp_path, code):
    pidfile = tmp_path / "pids"
    rx = receivers(capture_cmd(pidfile))
    v4l2.pi4().open_errors["video0"] = code
    rx.configure_local(KEY, opener(v4l2))
    assert rx.state == "no-device" and os.strerror(code) in rx.error, rx.error    # причина, а не только «not found»
    pump(300)
    assert pids(pidfile) == []
    del v4l2.open_errors["video0"]
    assert wait_until(lambda: rx.state == "playing", vmod.RESCAN_S + 5), rx.metrics()


def test_scan_exception_is_contained(receivers, v4l2, tmp_path):
    calls = []

    def broken():
        calls.append(1)
        if len(calls) < 3:
            raise OSError(errno.EIO, "Input/output error")
        return vi.open_uvc(**v4l2.scan_kw())
    rx = receivers(capture_cmd(tmp_path / "pids"))
    v4l2.pi4()
    rx.configure_local(KEY, broken)
    assert rx.state == "no-device" and rx.error.startswith("device scan failed")
    assert wait_until(lambda: rx.state == "playing", 3 * vmod.RESCAN_S + 5), rx.metrics()
    assert len(calls) >= 3


def test_broken_stdout_with_living_capture_is_restarted(receivers, v4l2, tmp_path, monkeypatch):
    """Процесс захвата жив, но канал кадров оборван (stdout закрыт): сторож перезапускает захват."""
    monkeypatch.setattr(vmod, "STALL_RESTART_S", 1.0)
    pidfile = tmp_path / "pids"
    rx = receivers(capture_cmd(pidfile, frames=10, after="closeout"))
    rx.configure_local(KEY, opener(v4l2.pi4()))
    assert wait_until(lambda: rx.state == "playing", 5)
    assert wait_until(lambda: rx.restarts >= 1, 8), rx.metrics()
    first = pids(pidfile)[0]
    assert wait_until(lambda: not alive(first), 3)
    assert wait_until(lambda: rx.state == "playing", 8)


def test_capture_executable_missing_backs_off(receivers, v4l2, tmp_path):
    rx = receivers([str(tmp_path / "no-such-gst-launch")])
    rx.configure_local(KEY, opener(v4l2.pi4()))
    lat = Latency()
    assert wait_until(lambda: rx.restarts >= 2, 8), rx.metrics()
    assert rx.state in ("restarting", "waiting") and lat.stop() < 500


# ---------------- 6. GUI без внешнего устройства ----------------
def obj(u, name):
    o = u.window.findChild(QObject, name)
    assert o is not None, name
    return o


def show(u, name):
    u.window.setProperty("currentPage", PAGE_NAMES.index(name))
    pump(50)


def apply(b, **kw):
    form = dict(b.config)
    form.update(kw)
    assert finish(b, b.applyForm(form, True)), b.lastResult
    pump(50)


def test_gui_without_device_synthetic_default_uvc_no_device_responsive(qapp, v4l2, tmp_path):
    compile_translations()
    pidfile = tmp_path / "pids"
    u = Ui(Backend(tmp_path / "data", autostart=True, video_command=capture_cmd(pidfile),
                   video_scan=v4l2.pi4(with_uvc=False).scan_kw()))
    try:
        b = u.backend
        pump(300)
        assert b.video_config()["video_source"] == "synthetic" and b.status["videoSource"] == "synthetic"
        assert v4l2.opened == [] and not any(e["code"] == "VIDEO-LOST" for e in b.activeEvents)   # без uvc не ищет
        apply(b, video_source="uvc")
        show(u, "video")
        assert b.video.state == "no-device"
        assert obj(u, "videoStateText").property("text") == "Нет видеоустройства"
        assert wait_until(lambda: any(e["code"] == "VIDEO-LOST" for e in b.activeEvents), 3)
        lat = Latency()
        for name in ("dashboard", "settings", "video", "logs", "video", "dashboard"):  # переходы во время пересканов
            show(u, name)
            pump(400)
        assert lat.stop() < 300, "интерфейс подвисает при повторном поиске устройства"
        assert obj(u, "overallLabel").property("text")
        assert b.video.state == "no-device" and pids(pidfile) == [] and v4l2.opened == [] and v4l2.aux_opened == []
        apply(b, video_source="synthetic")
        assert wait_until(lambda: not any(e["code"] == "VIDEO-LOST" for e in b.activeEvents), 3)
        assert b.video.metrics()["mode"] == "off" and b.status["videoSource"] == "synthetic"
    finally:
        u.close()


def test_gui_unsupported_mode_raises_video_lost(qapp, v4l2, tmp_path):
    compile_translations()
    u = Ui(Backend(tmp_path / "data", autostart=True, video_command=capture_cmd(tmp_path / "pids"),
                   video_scan=v4l2.pi4().scan_kw()))
    try:
        b = u.backend
        apply(b, video_source="uvc", video_mode="1920x1080@60", video_format="yuyv")
        show(u, "video")
        assert b.video.state == "unsupported" and "MJPEG 1920×1080@60" in obj(u, "videoReason").property("text")
        assert wait_until(lambda: any(e["code"] == "VIDEO-LOST" for e in b.activeEvents), 3)
        assert pids(tmp_path / "pids") == []
    finally:
        u.close()


# ---------------- 7. регрессия синтетического режима ----------------
needs_gst = pytest.mark.skipif(gst.choose_codec() is None, reason="нет работающего кодека GStreamer")


@needs_gst
def test_synthetic_local_video_unchanged_after_uvc_round_trip(qapp, v4l2, tmp_path):
    """synthetic → uvc (нет устройства) → synthetic: генератор и приёмник снова те же, что при запуске; состояние
    возвращается к «Воспроизведение», лишних процессов GStreamer нет."""
    compile_translations()
    u = Ui(Backend(tmp_path / "data", autostart=True, local_video=True,
                   video_scan=v4l2.pi4(with_uvc=False).scan_kw()))
    try:
        b = u.backend
        assert wait_until(lambda: b.video.state == "playing", 10), b.video.metrics()
        norm = lambda args: [a for a in args if not a.startswith("port=")]      # noqa: E731 — порт выбирается заново
        before = (b.video.metrics()["mode"], b.video.codec, norm(b._local_sender.args))
        assert "videotestsrc" in before[2] and "v4l2src" not in before[2]
        sender = b._local_sender
        apply(b, video_source="uvc")
        assert b.video.state == "no-device" and b._local_sender is None and sender.poll() is not None
        apply(b, video_source="synthetic")
        assert wait_until(lambda: b.video.state == "playing", 10), b.video.metrics()
        assert (b.video.metrics()["mode"], b.video.codec, norm(b._local_sender.args)) == before
        assert "source" not in b.video.metrics() and b.video.source == {}
        assert v4l2.aux_opened == []
    finally:
        u.close()
