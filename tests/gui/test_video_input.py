# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S3.3 Universal Video Input в GUI пульта (offscreen, L3): локальный видеовход через тот же VideoReceiver.

Устройства и GStreamer подставные: sysfs/dev/ioctl — tests/system/fake_v4l2.py, процесс захвата —
tests/system/fake_capture.py (кадры RGBx, как gst-launch-1.0 v4l2src … ! fdsink). Настоящая камера (L4) здесь
НЕ проверяется — см. docs/acceptance/s3.3-video-input/REPORT.md.
"""
import os
import sys
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QObject
from PySide6.QtQuickControls2 import QQuickStyle

from fake_v4l2 import FakeV4L2
from sheregesh import videoinput as vi
from sheregesh_console import video as vmod
from sheregesh_console.app import PAGE_NAMES, Ui, compile_translations
from sheregesh_console.backend import Backend
from sheregesh_console.qtwait import pump, wait_until
from sheregesh_console.video import VideoReceiver

QQuickStyle.setStyle("Fusion")
SYS = Path(__file__).resolve().parents[1] / "system"


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


@pytest.mark.test_id("TEST-SW-002")
def test_local_input_plays_and_reports_source(receivers, v4l2, tmp_path):
    rx = receivers(capture_cmd(tmp_path / "pids"))
    rx.configure_local(("", "1280x720@30", "auto"), opener(v4l2.pi4()))
    assert wait_until(lambda: rx.state == "playing", 5), rx.metrics()
    t0, f0 = time.monotonic(), rx.frames
    pump(2000)
    rate = (rx.frames - f0) / (time.monotonic() - t0)
    m = rx.metrics()
    assert m["mode"] == "local-input" and m["error"] == "" and rate > 20, (rate, m)    # кадры не теряются
    src = rx.source
    assert (src["kind"], src["name"], src["format"], src["resolution"], src["fps"]) == \
        ("uvc", "USB Video: USB Video", "mjpeg", "1280x720", 30)
    assert m["source"]["id"] == "uvc:534d:2109" and "YUYV 640×480@60" in src["formats"]
    assert v4l2.opened == ["video0", "video1"]                    # служебные узлы Pi не открывались


def test_absent_device_does_not_block_and_hotplug_is_picked_up(receivers, v4l2, tmp_path):
    rx = receivers(capture_cmd(tmp_path / "pids"))
    v4l2.pi4(with_uvc=False)
    rx.configure_local(("", "1280x720@30", "auto"), opener(v4l2))
    assert rx.state == "no-device" and "not found" in rx.error and pids(tmp_path / "pids") == []
    pump(500)
    assert rx.state == "no-device"
    v4l2.add("video0", "uvcvideo", "USB Video: USB Video", 0, ("534d", "2109", ""),
             formats=[("MJPG", "discrete", [((1280, 720), [30])])])
    assert wait_until(lambda: rx.state == "playing", vmod.RESCAN_S + 5), rx.metrics()


def test_unsupported_mode_is_reported_without_capture(receivers, v4l2, tmp_path):
    rx = receivers(capture_cmd(tmp_path / "pids"))
    rx.configure_local(("", "1920x1080@60", "yuyv"), opener(v4l2.pi4(), mode="1920x1080@60", fmt="yuyv"))
    assert rx.state == "unsupported" and "not supported" in rx.error and "MJPEG 1920×1080@60" in rx.error
    pump(300)
    assert pids(tmp_path / "pids") == []                          # устройство не захватывалось


def test_disconnect_then_recovery(receivers, v4l2, tmp_path):
    """Отключение USB: процесс захвата завершается (как v4l2src), устройство исчезает; после подключения — снова кадры."""
    rx = receivers(capture_cmd(tmp_path / "pids", frames=30))
    rx.configure_local(("", "1280x720@30", "auto"), opener(v4l2.pi4()))
    assert wait_until(lambda: rx.state == "playing", 5)
    v4l2.remove("video0")
    v4l2.remove("video1")
    assert wait_until(lambda: rx.state in ("restarting", "no-device"), 5), rx.metrics()
    assert wait_until(lambda: rx.state == "no-device", 8), rx.metrics()
    assert rx.restarts >= 1 and "not found" in rx.error
    v4l2.add("video3", "uvcvideo", "USB Video: USB Video", 0, ("534d", "2109", ""),
             formats=[("MJPG", "discrete", [((1280, 720), [30])])])
    assert wait_until(lambda: rx.state == "playing", vmod.RESCAN_S + 5), rx.metrics()
    assert rx.metrics()["source"]["id"] == "uvc:534d:2109"
    assert not any(alive(p) for p in pids(tmp_path / "pids")[:-1])   # завершившиеся процессы не остаются


def test_stalled_device_restarts_capture(receivers, v4l2, tmp_path, monkeypatch):
    monkeypatch.setattr(vmod, "STALL_RESTART_S", 1.0)
    rx = receivers(capture_cmd(tmp_path / "pids", frames=20, after="hang"))
    rx.configure_local(("", "1280x720@30", "auto"), opener(v4l2.pi4()))
    assert wait_until(lambda: rx.state == "playing", 5)
    assert wait_until(lambda: rx.restarts >= 1, 8), rx.metrics()
    first = pids(tmp_path / "pids")[0]
    assert wait_until(lambda: not alive(first), 3)                # зависший захват убит, а не брошен
    assert wait_until(lambda: rx.state == "playing", 8)


def test_capture_crash_is_restarted_without_freezing(receivers, v4l2, tmp_path):
    rx = receivers(["false"])
    rx.configure_local(("", "1280x720@30", "auto"), opener(v4l2.pi4()))
    assert wait_until(lambda: rx.restarts >= 2, 8), rx.metrics()
    assert rx.state in ("restarting", "waiting") and "capture exited" in rx.error


def test_source_switching_leaves_no_processes(receivers, v4l2, tmp_path):
    pidfile = tmp_path / "pids"
    rx = receivers(capture_cmd(pidfile))
    key, op = ("", "1280x720@30", "auto"), opener(v4l2.pi4())
    rx.configure_local(key, op)
    assert wait_until(lambda: rx.state == "playing", 5)
    rx.configure_local(key, op)                                   # та же конфигурация — без перезапуска
    assert len(pids(pidfile)) == 1
    rx.configure(None, None)                                      # переключение на «выключено»
    assert rx.state == "off" and rx.source == {} and not any(alive(p) for p in pids(pidfile))
    rx.configure_local(key, op)
    assert wait_until(lambda: rx.state == "playing", 5)
    rx.configure_local(("", "640x480@60", "auto"), opener(v4l2, mode="640x480@60"))   # другой режим — перезапуск
    assert wait_until(lambda: rx.state == "playing" and rx.source["format"] == "yuyv", 5)
    rx.shutdown()
    assert len(pids(pidfile)) == 3 and not any(alive(p) for p in pids(pidfile))


# ---------------- пульт: конфигурация и экран «Видео» ----------------
def make_backend(data, v4l2, pidfile, **kw):
    return Backend(data, autostart=True, video_command=capture_cmd(pidfile), video_scan=v4l2.scan_kw(), **kw)


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
    assert b.applyForm(form, True), b.lastResult
    pump(50)


@pytest.mark.test_id("TEST-SW-002")
def test_uvc_source_selected_in_settings_survives_application_update(qapp, v4l2, tmp_path):
    """Конфигурация пишется в каталог данных (на устройстве /var/lib/sheregesh — DEV deploy и обновление его
    сохраняют): новый процесс приложения на том же каталоге снова открывает тот же видеовход."""
    compile_translations()
    data, pidfile = tmp_path / "data", tmp_path / "pids"
    u = Ui(make_backend(data, v4l2.pi4(), pidfile))
    try:
        b = u.backend
        assert b.video_config()["video_source"] == "synthetic" and b.video.metrics()["mode"] == "off"
        assert obj(u, "videoSourceBox").property("currentIndex") == 0
        apply(b, video_source="uvc", video_mode="1280x720@60")
        assert wait_until(lambda: b.video.state == "playing", 5), b.video.metrics()
        show(u, "video")
        info = obj(u, "videoInfo").property("text")
        assert "USB Video: USB Video" in info and "MJPEG 1280×720" in info and "60" in info, info
        assert obj(u, "videoStateText").property("text") == "Воспроизведение"
        assert obj(u, "videoSourceBox").property("currentIndex") == 1
        rev = b.revision
    finally:
        u.close()
    assert not any(alive(p) for p in pids(pidfile))
    b2 = make_backend(data, v4l2, pidfile)                        # «обновлённое» приложение, тот же каталог данных
    try:
        assert b2.revision == rev and b2.video_config()["video_mode"] == "1280x720@60"
        assert wait_until(lambda: b2.video.state == "playing", 5), b2.video.metrics()
        assert b2.video.metrics()["source"]["fps"] == 60
    finally:
        b2.stop()


def test_disabled_and_synthetic_sources(qapp, v4l2, tmp_path):
    compile_translations()
    u = Ui(make_backend(tmp_path / "data", v4l2.pi4(), tmp_path / "pids"))
    try:
        b = u.backend
        apply(b, video_source="uvc")
        assert wait_until(lambda: b.video.state == "playing", 5)
        apply(b, video_source="disabled")
        assert b.video.state == "off" and not any(alive(p) for p in pids(tmp_path / "pids"))
        show(u, "video")
        assert obj(u, "videoReason").property("text") == "Видеоисточник выключен в настройках."
        assert not any(e["code"] == "VIDEO-LOST" for e in b.activeEvents)
        apply(b, video_source="synthetic")                        # прежний режим: поток удалённого блока
        assert b.video.metrics()["mode"] == "off" and b.status["videoSource"] == "synthetic"
    finally:
        u.close()


def test_missing_device_is_shown_with_reason_and_event(qapp, v4l2, tmp_path):
    compile_translations()
    u = Ui(make_backend(tmp_path / "data", v4l2.pi4(with_uvc=False), tmp_path / "pids"))
    try:
        b = u.backend
        apply(b, video_source="uvc")
        show(u, "video")
        assert b.video.state == "no-device"
        assert obj(u, "videoStateText").property("text") == "Нет видеоустройства"
        assert "not found" in obj(u, "videoReason").property("text")
        assert wait_until(lambda: any(e["code"] == "VIDEO-LOST" for e in b.activeEvents), 3)
        show(u, "dashboard")
        assert obj(u, "overallLabel").property("text")           # остальной интерфейс работает
    finally:
        u.close()


def test_configuration_without_video_fields_keeps_previous_behaviour(qapp, tmp_path):
    """Ревизии до S3.3 (без полей video_*) корректны: синтетический режим, форма настроек без изменений."""
    compile_translations()
    u = Ui(Backend(tmp_path / "data", autostart=False))
    try:
        b = u.backend
        assert "video_source" not in b.store.active().data["system.yaml"]
        assert b.config["video_source"] == "synthetic" and b.status["videoSource"] == "synthetic"
        show(u, "settings")
        assert obj(u, "dirtyLabel").property("text") == "изменений нет"
    finally:
        u.close()
