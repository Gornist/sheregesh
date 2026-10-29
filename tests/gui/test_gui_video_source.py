# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Источник видео на экранах пульта: страница «Видео» и плитка связи (offscreen, L3).

Состояния источника на блоке (поток / нет сигнала / нет устройства / ошибка подключения RTSP, а также блок 1.1 без
этих полей) задаются подменой VideoOps.report — так же, как они придут из телеметрии и INFO видеосеанса
(sheregesh_console.videosource). Кадры даёт подменный «декодер» (процесс, пишущий RGBx 640×360), поэтому GStreamer
не нужен. Проверяется: подписи входа/состояния/режима/причины, чёрное поле без последнего кадра при «нет сигнала» и
возврат видео, геометрия на 1920×1200 и 1280×800 (текст не выходит за плитки и кадр). Снимки:
SHEREGESH_GUI_SOURCE_OUT=docs/acceptance/video-ingest/gui — туда; иначе во временный каталог.

Это не проверка HDMI/cage/сенсора и не проверка реального видеовхода блока: только отображение на пульте.
"""
import json
import os
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtQuickControls2 import QQuickStyle

from sheregesh import PROTOCOL_VERSION
from sheregesh_console.app import PAGE_NAMES, Ui, compile_translations
from sheregesh_console.backend import Backend
from sheregesh_console.qtwait import pump, wait_until
from test_e2e_gui_screens import find, header_problems, outside_window, rect, save_small, tile_overflow, video_problems
from test_gui_tile_geometry import text_problems, wide_font  # noqa: F401 — фикстура шрифта с метриками устройства

QQuickStyle.setStyle("Fusion")
SIZES = [(1920, 1200), (1280, 800)]

# «Декодер» для испытаний: цветные полосы с бегущей светлой полосой, 25 кадров/с, RGBx 640×360 в stdout.
FAKE_DECODER = [sys.executable, "-c", r"""
import os, sys, time
W, H = 640, 360
bars = [(235, 235, 235), (235, 235, 16), (16, 235, 235), (16, 235, 16), (235, 16, 235), (235, 16, 16), (16, 16, 235)]
row = b"".join(bytes((*bars[x * len(bars) // W], 255)) for x in range(W))
base = row * H
n = 0
while True:
    y = (n * 8) % H
    frame = base[:y * W * 4] + bytes((255, 255, 255, 255)) * (W * 12) + base[(y + 12) * W * 4:]
    try:
        os.write(1, frame[:W * H * 4])
    except OSError:
        sys.exit(0)
    n += 1
    time.sleep(0.04)
"""]

TEL = {"enabled": True, "active": True, "codec": "h264", "port": 5600, "transport": "tls"}
INFO_11 = {"codec": "h264", "width": 1280, "height": 720, "fps": 30}
STATES = {
    "synthetic-stream": {"telemetry_video": {**TEL, "source": {"input": "synthetic", "state": "streaming",
                                                               "device": "videotestsrc smpte", "width": 1280,
                                                               "height": 720, "fps": 30}},
                         "session_info": INFO_11},
    "legacy-1.1": {"telemetry_video": TEL, "session_info": INFO_11},
    "cvbs-no-signal": {"telemetry_video": {**TEL, "source": {"input": "cvbs", "state": "no-signal",
                                                             "device": "/dev/video0 (EasyCAP UTV007)",
                                                             "error": "no video sync on composite input",
                                                             "width": 720, "height": 576, "fps": 25}},
                       "session_info": {"codec": "h264", "width": 720, "height": 576, "fps": 25}},
    "hdmi-no-device": {"telemetry_video": {**TEL, "source": {"input": "hdmi", "state": "no-device",
                                                             "device": "tc358743 (CSI-2)",
                                                             "error": "/dev/video0 not found: HDMI bridge not detected"}},
                       "session_info": {}},
    "rtsp-connect-error": {"telemetry_video": {**TEL, "source": {
        "input": "rtsp", "state": "connect-error", "url": "rtsp://admin:s3cret@192.168.1.64:554/Streaming/Channels/101",
        "error": "Could not open resource for reading and writing: rtsp://admin:s3cret@192.168.1.64:554/"
                 "Streaming/Channels/101 (Connection refused)"}}, "session_info": {}},
}
CONNECTED = {"peerLink": "CONNECTED", "linkView": "CONNECTED", "peerState": "connected", "peerDetail": "ok",
             "peerDevice": "remote-emu-01", "peerSoftware": "0.1.0.dev0", "peerRole": "remote", "peerProtocol": PROTOCOL_VERSION,
             "linkSecure": True, "linkTls": "TLSv1.3", "linkHost": "192.168.1.171", "linkRttMs": 1.8,
             "peerHeartbeatAge": 0.2, "remoteAvailable": True, "remoteTempSoc": 47.3, "remotePowerPercent": 85,
             "remoteUptime": 1234, "peerMode": "network"}


@pytest.fixture
def out_dir(tmp_path):
    d = Path(os.environ.get("SHEREGESH_GUI_SOURCE_OUT") or tmp_path / "gui-source")
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def ui(qapp, tmp_path, wide_font):
    compile_translations()
    b = Backend(tmp_path / "data", autostart=False, video_command=FAKE_DECODER)
    report = {"mode": "remote", **STATES["synthetic-stream"]}
    b.videoops.report = lambda: report                 # сведения блока — как из телеметрии и INFO видеосеанса
    b.report = report
    u = Ui(b)
    b._status.update(CONNECTED)
    b.statusChanged.emit()
    b.video.configure("h264", 5600)                  # подменный декодер: кадры идут
    yield u
    u.close()


def set_source(u, state):
    u.backend.report.clear()
    u.backend.report.update({"mode": "remote", **STATES[state]})
    u.backend.videoops.update()
    pump(80)


def show(u, page):
    u.window.setProperty("currentPage", PAGE_NAMES.index(page))
    pump(60)


def lit_fraction(u, item):
    """Доля светлых пикселей кадра (полосы светлые; чёрное поле с подписью — единицы процентов)."""
    img = u.window.grabWindow()
    sub = img.copy(item.mapRectToScene(QRectF(0, 0, item.width(), item.height())).toRect())
    step = max(1, sub.width() // 80)
    total = lit = 0
    for x in range(0, sub.width(), step):
        for y in range(0, sub.height(), step):
            c = sub.pixelColor(x, y)
            total += 1
            lit += (c.red() + c.green() + c.blue()) > 60
    return lit / max(total, 1)


def text(u, name):
    return find(u, name).property("text")


@pytest.mark.test_id("TEST-UI-002")
def test_source_states_text_frame_and_recovery(ui):
    b = ui.backend
    assert wait_until(lambda: b.video.state == "playing", 5), b.video.metrics()
    ui.window.resize(1280, 800)
    show(ui, "video")
    view = find(ui, "videoView")

    # поток синтетического источника: кадры видны, вход и режим подписаны
    set_source(ui, "synthetic-stream")
    assert b.videoSource["display"] == "playing" and b.videoSource["live"]
    assert text(ui, "videoStateText") == "Воспроизведение"
    assert "Синтетический" in text(ui, "videoSourceText") and "поток" in text(ui, "videoSourceText")
    assert "1280×720" in text(ui, "videoInfo") and "показано" in text(ui, "videoInfo")
    pump(300)
    assert lit_fraction(ui, view) > 0.6

    # нет сигнала на CVBS: кадры от блока ещё идут, но поле чёрное, причина и вход названы
    set_source(ui, "cvbs-no-signal")
    pump(300)
    assert b.video.state == "playing"                     # приёмник кадры получает…
    assert b.videoSource["display"] == "no-signal" and not b.videoSource["live"]   # …но они не выдаются за видео
    assert lit_fraction(ui, view) < 0.15
    assert find(ui, "videoPlaceholder").property("visible") and text(ui, "videoPlaceholder") == "Нет сигнала"
    assert "CVBS" in text(ui, "videoReason") and "no video sync" in text(ui, "videoReason")
    assert "720×576" in text(ui, "videoInfo")

    # нет устройства (HDMI)
    set_source(ui, "hdmi-no-device")
    assert text(ui, "videoPlaceholder") == "Нет видеоустройства" and lit_fraction(ui, view) < 0.15
    assert "HDMI" in text(ui, "videoReason") and "not detected" in text(ui, "videoReason")

    # RTSP: ошибка подключения, учётные данные из адреса не показываются
    set_source(ui, "rtsp-connect-error")
    assert text(ui, "videoPlaceholder") == "Ошибка подключения"
    shown = text(ui, "videoReason") + text(ui, "videoSourceText")
    assert "RTSP" in shown and "Connection refused" in shown and "s3cret" not in shown and "admin" not in shown
    show(ui, "dashboard")
    assert "RTSP" in text(ui, "linkVideo") and "ошибка подключения" in text(ui, "linkVideo")

    # возврат сигнала: снова видео, и это новые кадры
    set_source(ui, "synthetic-stream")
    show(ui, "video")
    assert wait_until(lambda: view.showing_frame(), 3)
    pump(200)
    assert lit_fraction(ui, view) > 0.6 and not find(ui, "videoPlaceholder").property("visible")
    show(ui, "dashboard")
    assert "воспроизводится" in text(ui, "linkVideo")


@pytest.mark.test_id("TEST-UI-002")
def test_last_frame_is_not_reused_after_no_signal(ui):
    """Кадр, показанный до «нет сигнала», не возвращается как актуальный: после возврата сигнала — только новые."""
    b = ui.backend
    assert wait_until(lambda: b.video.state == "playing", 5)
    show(ui, "video")
    view = find(ui, "videoView")
    pump(200)
    b.video.configure(None, None)                         # декодер остановлен — новых кадров нет
    set_source(ui, "cvbs-no-signal")
    assert not view.showing_frame()
    b.report.update(STATES["synthetic-stream"])           # блок снова сообщает «поток», но кадров ещё нет
    b.videoops.update()
    pump(100)
    assert not view.showing_frame()
    b.video.configure("h264", 5600)
    assert wait_until(lambda: view.showing_frame(), 5)


@pytest.mark.test_id("TEST-UI-002")
def test_block_11_without_source_fields(ui):
    """Старый блок (1.1): полей источника нет — «синтетический», тип входа не сообщается, состояние по приёмнику."""
    b = ui.backend
    assert wait_until(lambda: b.video.state == "playing", 5)
    set_source(ui, "legacy-1.1")
    show(ui, "video")
    src = text(ui, "videoSourceText")
    assert "Синтетический" in src and "не сообщается" in src, src
    assert b.videoSource["live"] and "1280×720" in text(ui, "videoInfo")
    show(ui, "dashboard")
    assert text(ui, "linkVideo").startswith("Видео (Синтетический): воспроизводится")


@pytest.mark.test_id("TEST-UI-002")
def test_snapshots_and_geometry_all_states_both_resolutions(ui, out_dir, wide_font):
    b = ui.backend
    assert wait_until(lambda: b.video.state == "playing", 5)
    report = []
    try:
        for state in STATES:
            set_source(ui, state)
            for w, h in SIZES:
                ui.window.resize(w, h)
                pump(250)
                for page in ("video", "dashboard"):
                    show(ui, page)
                    pump(400 if page == "video" else 100)
                    problems = outside_window(ui) + header_problems(ui)
                    problems += video_problems(ui) if page == "video" else tile_overflow(ui) + text_problems(ui)
                    if page == "video":
                        frame = rect(find(ui, "videoView")).adjusted(-4, -4, 4, 4)
                        for n in ("videoSourceText", "videoInfo"):
                            t = find(ui, n)
                            if t.property("contentWidth") > t.width() + 1:
                                problems.append(f"{n} шире элемента")
                        if not frame.contains(rect(find(ui, "videoReason"))) and find(ui, "videoReason").isVisible():
                            problems.append("videoReason вне кадра")
                    shot = save_small(ui, out_dir / f"{w}x{h}-{state}-{page}.png")
                    report.append({"size": f"{w}x{h}", "state": state, "page": page, "png": shot.name,
                                   "display": b.videoSource["display"], "live": b.videoSource["live"],
                                   "source_text": text(ui, "videoSourceText") if page == "video" else None,
                                   "link_video": text(ui, "linkVideo") if page == "dashboard" else None,
                                   "problems": problems})
                    assert problems == [], f"{w}×{h} {state} {page}: {problems}"
    finally:
        (out_dir / "geometry.json").write_text(json.dumps(
            {"note": "offscreen, QT_QUICK_BACKEND=software (L3), шрифт с метриками устройства "
                     f"({wide_font or 'системный'}) — не проверка HDMI/cage/сенсора и не видеовход блока; "
                     "состояния источника заданы подменой сведений блока (контракт полей — sheregesh_console.videosource)",
             "checks": ["элементы в пределах окна", "текст плиток в пределах плитки", "подписи видео в пределах кадра",
                        "индикаторы заголовка не перекрываются", "надписи не шире своих элементов"],
             "shots": report}, ensure_ascii=False, indent=1), encoding="utf-8")
