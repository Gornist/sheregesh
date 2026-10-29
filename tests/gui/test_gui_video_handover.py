# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Переход источника видео на экране пульта (S5 GUI-01, offscreen, L3): «Воспроизведение» — только по кадрам НОВОГО
источника, при старте сеанса — без лишнего чёрного экрана.

Сведения блока задаются подменой VideoOps.report (как в test_gui_video_source / test_gui_video_select), кадры —
подменный «декодер», который пишет RGBx 640×360, пока нет файла паузы: так моделируются кадры прежнего источника,
продолжающие приходить после смены (буфер, приёмник «playing» ещё NO_VIDEO_AFTER_S после последнего кадра), пауза
подключения RTSP и кадры нового источника. Снимки «Подключение» 1920×1200 и 1280×800:
SHEREGESH_GUI_HANDOVER_OUT=docs/acceptance/s5-video/gui — туда; иначе во временный каталог.

Это не проверка HDMI/cage/сенсора и не проверка переключения на настоящем блоке: только экран пульта.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest
from PySide6.QtQuickControls2 import QQuickStyle

from sheregesh_console import videosource as vs
from sheregesh_console.app import PAGE_NAMES, Ui, compile_translations
from sheregesh_console.backend import Backend
from sheregesh_console.journal import MemoryJournal
from sheregesh_console.qtwait import pump, wait_until
from test_e2e_gui_screens import find, header_problems, outside_window, save_small, tile_overflow, video_problems
from test_gui_tile_geometry import text_problems, wide_font  # noqa: F401 — фикстура шрифта с метриками устройства
from test_gui_video_select import SOURCES, Peer, tel
from test_gui_video_source import CONNECTED, lit_fraction

QQuickStyle.setStyle("Fusion")
SIZES = [(1920, 1200), (1280, 800)]

# «Декодер»: полосы 25 кадров/с, пока нет файла паузы (argv[1]).
PAUSABLE_DECODER = r"""
import os, sys, time
W, H = 640, 360
pause = sys.argv[1]
bars = [(235, 235, 235), (235, 235, 16), (16, 235, 235), (16, 235, 16), (235, 16, 235), (235, 16, 16), (16, 16, 235)]
frame = b"".join(bytes((*bars[x * len(bars) // W], 255)) for x in range(W)) * H
while True:
    if not os.path.exists(pause):
        try:
            os.write(1, frame)
        except OSError:
            sys.exit(0)
    time.sleep(0.04)
"""
RTSP = {"input": "rtsp", "device": "rtsp://192.168.10.5:8554/cam", "width": 1280, "height": 720, "fps": 25}


def rtsp_tel(state, **kw):
    """Телеметрия блока после выбора rtsp:1 (status.video.source — сведения нового источника)."""
    return {**tel(SOURCES, "rtsp:1"), "source": {**RTSP, "state": state, **kw}}


class Frames:
    """Файл паузы подменного декодера: кадры идут / кадров нет."""

    def __init__(self, path):
        self.path = path

    def stop(self):
        self.path.write_text("pause")

    def go(self):
        self.path.unlink(missing_ok=True)


@pytest.fixture
def out_dir(tmp_path):
    d = Path(os.environ.get("SHEREGESH_GUI_HANDOVER_OUT") or tmp_path / "gui-handover")
    d.mkdir(parents=True, exist_ok=True)
    return d


def make(tmp_path, telemetry_video, configure=True):
    compile_translations()
    pause = tmp_path / "pause"
    b = Backend(tmp_path / "data", autostart=False, video_command=[sys.executable, "-c", PAUSABLE_DECODER, str(pause)],
                journal=MemoryJournal())
    report = {"mode": "remote", "telemetry_video": telemetry_video,
              "session_info": {"codec": "h264", "width": 1280, "height": 720, "fps": 30}}
    b.videoops.report = lambda: report
    b.report = report
    b.peer = Peer(report)
    b.videoops.peer = lambda: b.peer
    b.frames_ctl = Frames(pause)
    u = Ui(b)
    b._status.update(CONNECTED)
    b.statusChanged.emit()
    b.videoops.update()
    if configure:
        b.video.configure("h264", 5600)
    u.window.setProperty("currentPage", PAGE_NAMES.index("video"))
    pump(100)
    return u


@pytest.fixture
def ui(qapp, tmp_path, wide_font):
    u = make(tmp_path, tel(SOURCES))                               # synthetic, streaming
    assert wait_until(lambda: u.backend.videoSource["live"], 5), u.backend.videoSource
    yield u
    u.backend.frames_ctl.go()
    u.close()


@pytest.fixture
def ui_start(qapp, tmp_path, wide_font):
    """Старт сеанса: блок сообщает starting (idle → starting), видеоканал ещё не подключён."""
    u = make(tmp_path, tel(SOURCES, state="starting"), configure=False)
    yield u
    u.close()


def set_report(u, **kw):
    u.backend.report.update(kw)
    u.backend.videoops.update()
    pump(60)


def text(u, name):
    return find(u, name).property("text")


def video_records(b):
    return [t for _p, t in b.journal.records if t.startswith("video:")]


def connecting(u):
    b = u.backend
    return b.videoSource["display"] == vs.CONNECTING and not b.videoSource["live"]


# ---------------- сценарии ----------------
@pytest.mark.test_id("TEST-UI-002")
def test_switch_synthetic_to_rtsp_while_old_frames_continue(ui):
    """Блок сменил synthetic → rtsp; кадры прежнего источника ещё идут, затем пауза подключения RTSP (приёмник всё это
    время «playing»): «Подключение», чёрное поле; «Воспроизведение» — с кадрами после перехода. Журнал: приёмник не
    прерывался — новых записей «video: …» нет (E2E-12: одна запись на переход приёмника)."""
    b = ui.backend
    view = find(ui, "videoView")
    pump(300)
    assert lit_fraction(ui, view) > 0.6
    before = video_records(b)
    set_report(ui, telemetry_video=rtsp_tel("starting"))
    assert b.video.state == "playing" and connecting(ui), b.videoSource
    assert text(ui, "videoPlaceholder") == "Подключение…" and text(ui, "videoStateText") == "Подключение…"
    assert text(ui, "videoReason") == ("Подключение к источнику RTSP: ожидаются его первые кадры. "
                                       "Кадры прежнего источника не показываются.")
    pump(int(vs.FLUSH_S * 1000) - 150)                             # кадры прежнего источника, бывшие в пути
    assert connecting(ui) and lit_fraction(ui, view) < 0.15 and not view.showing_frame()
    b.frames_ctl.stop()                                            # подключение RTSP: кадров нет
    pump(900)
    assert b.video.state == "playing" and connecting(ui), (b.video.state, b.videoSource)   # приёмник ещё «playing»
    set_report(ui, telemetry_video=rtsp_tel("streaming"))         # подтверждение блока опережает кадры на пульте
    assert connecting(ui) and lit_fraction(ui, view) < 0.15
    t0 = time.monotonic()
    b.frames_ctl.go()
    assert wait_until(lambda: b.videoSource["live"], 3), b.videoSource
    assert time.monotonic() - t0 < 1.5
    assert wait_until(view.showing_frame, 2)
    pump(150)
    assert text(ui, "videoStateText") == "Воспроизведение" and lit_fraction(ui, view) > 0.6
    assert video_records(b) == before, video_records(b)


@pytest.mark.test_id("TEST-UI-002")
def test_switch_through_gui_select_waits_for_new_frames(ui):
    """Выбор с пульта (Backend.selectVideoSource, как кнопка «Видео»): «Переключение…» до подтверждения блоком, затем
    «Подключение», пока нет кадров нового источника, — даже если блок уже сообщает streaming."""
    b = ui.backend
    b.peer.confirm = False
    b.frames_ctl.stop()
    assert b.selectVideoSource("rtsp:1", True)
    assert wait_until(lambda: b.peer.calls == ["rtsp:1"], 3)
    pump(60)
    assert b.videoSource["display"] == "switching" and not b.videoSource["live"]
    set_report(ui, telemetry_video=rtsp_tel("streaming"))         # блок подтвердил выбор и уже передаёт поток
    assert b.videoSelection["state"] == "idle" and b.video.state == "playing"
    assert connecting(ui) and text(ui, "videoPlaceholder") == "Подключение…"
    b.frames_ctl.go()
    assert wait_until(lambda: b.videoSource["live"], 3), b.videoSource
    pump(100)
    assert text(ui, "videoStateText") == "Воспроизведение"


@pytest.mark.test_id("TEST-UI-002")
def test_rtsp_wrong_codec_starting_then_error(ui):
    """RTSP с неподдерживаемым кодеком: блок starting → error. Кадры прежнего источника не выдаются за новый ни при
    «Подключение», ни при ошибке; причина — текстом."""
    b = ui.backend
    view = find(ui, "videoView")
    set_report(ui, telemetry_video=rtsp_tel("starting"))
    assert connecting(ui)
    pump(300)
    assert lit_fraction(ui, view) < 0.15
    set_report(ui, telemetry_video=rtsp_tel("error", error="pipeline: no decoder for H.265 stream"))
    assert b.videoSource["display"] == "error" and not b.videoSource["live"]
    pump(300)
    assert text(ui, "videoPlaceholder") == "Ошибка видеоисточника"
    assert "H.265" in text(ui, "videoReason") and lit_fraction(ui, view) < 0.15 and not view.showing_frame()
    set_report(ui, telemetry_video=rtsp_tel("starting", restarts=1))   # повторная попытка блока
    assert connecting(ui)
    set_report(ui, telemetry_video=tel(SOURCES, "synthetic"))       # оператор вернул synthetic
    assert wait_until(lambda: b.videoSource["live"], 3), b.videoSource


@pytest.mark.test_id("TEST-UI-002")
def test_session_start_without_extra_black_screen(ui_start):
    """Старт сеанса: блок сообщает starting и подтверждает streaming позже фактических кадров — кадр показывается,
    как только кадры реально идут (без ожидания streaming и без FLUSH_S); журнал — одна запись «stream started»."""
    u = ui_start
    b = u.backend
    assert connecting(u) and text(u, "videoPlaceholder") == "Подключение…"
    b.video.configure("h264", 5600)
    assert wait_until(lambda: b.video.frames > 0, 5)
    t0 = time.monotonic()
    assert wait_until(lambda: b.videoSource["live"], 2), b.videoSource
    assert time.monotonic() - t0 < 0.4, time.monotonic() - t0
    assert b.report["telemetry_video"]["source"]["state"] == "starting"   # блок ещё не подтвердил
    assert wait_until(find(u, "videoView").showing_frame, 2)
    set_report(u, telemetry_video=tel(SOURCES))                     # блок: streaming — без нового перехода
    assert b.videoSource["live"]
    pump(200)
    assert text(u, "videoStateText") == "Воспроизведение"
    assert video_records(b) == ["video: stream started (codec=h264 source=synthetic)"], video_records(b)


# ---------------- снимки и геометрия ----------------
@pytest.mark.test_id("TEST-UI-002")
@pytest.mark.parametrize("shot", ["connecting-switch", "connecting-start"])
def test_connecting_snapshots_and_geometry(qapp, tmp_path, wide_font, out_dir, shot):
    if shot == "connecting-switch":
        u = make(tmp_path, tel(SOURCES))
        assert wait_until(lambda: u.backend.videoSource["live"], 5)
        u.backend.frames_ctl.stop()
        set_report(u, telemetry_video=rtsp_tel("starting"))
    else:
        u = make(tmp_path, tel(SOURCES, state="starting"), configure=False)
    b = u.backend
    report = []
    try:
        for w, h in SIZES:
            u.window.resize(w, h)
            pump(250)
            for page in ("video", "dashboard"):
                u.window.setProperty("currentPage", PAGE_NAMES.index(page))
                pump(300 if page == "video" else 100)
                assert connecting(u), b.videoSource
                problems = outside_window(u) + header_problems(u)
                if page == "video":
                    problems += video_problems(u)
                    assert text(u, "videoPlaceholder") == "Подключение…"
                else:
                    problems += tile_overflow(u) + text_problems(u)
                    assert "подключение к источнику…" in text(u, "linkVideo")
                png = save_small(u, out_dir / f"{w}x{h}-{shot}-{page}.png")
                report.append({"size": f"{w}x{h}", "page": page, "png": png.name, "display": b.videoSource["display"],
                               "live": b.videoSource["live"], "receiver": b.video.state,
                               "placeholder": text(u, "videoPlaceholder") if page == "video" else None,
                               "reason": text(u, "videoReason") if page == "video" else None,
                               "link_video": text(u, "linkVideo") if page == "dashboard" else None,
                               "problems": problems})
                assert problems == [], f"{w}×{h} {shot} {page}: {problems}"
    finally:
        b.frames_ctl.go()
        (out_dir / f"geometry-{shot}.json").write_text(json.dumps(
            {"note": "offscreen, QT_QUICK_BACKEND=software (L3), шрифт с метриками устройства "
                     f"({wide_font or 'системный'}) — не проверка HDMI/cage/сенсора и не переход на настоящем блоке; "
                     "сведения блока заданы подменой (status.video.source), кадры — подменный декодер",
             "checks": ["элементы в пределах окна", "текст плиток в пределах плитки", "подписи видео в пределах кадра",
                        "индикаторы заголовка не перекрываются"],
             "shots": report}, ensure_ascii=False, indent=1), encoding="utf-8")
        u.close()
