# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Выбор источника видео блока с пульта (S4): список на странице «Видео», подтверждение, «Переключение…», ошибки,
старый блок без списка, клиент без select_video_source, «Переподключение» (offscreen, L3).

Список источников приходит в телеметрии status.video.sources (подмена Backend._video_report, как в
test_gui_video_source); выбор — вызов select_video_source(id) у клиента канала (подмена Backend._video_peer: метод
добавляет агент B в sheregesh.link.client.PeerClient). Кадры — подменный декодер. Снимки 1920×1200 и 1280×800:
SHEREGESH_GUI_SELECT_OUT=docs/acceptance/s4-video/gui — туда; иначе во временный каталог.

Это не проверка HDMI/cage/сенсора и не проверка переключения на настоящем блоке: только экран пульта.
"""
import copy
import json
import os
import threading
from pathlib import Path

import pytest
from PySide6.QtCore import QMetaObject, QPoint, QPointF, Qt
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from sheregesh_console import backend as backend_mod
from sheregesh_console import videosource as vs
from sheregesh_console.app import PAGE_NAMES, Ui, compile_translations
from sheregesh_console.backend import Backend, select_error
from sheregesh_console.qtwait import pump, wait_until
from test_e2e_gui_screens import find, header_problems, outside_window, rect, save_small, tile_overflow, video_problems
from test_gui_tile_geometry import text_problems, wide_font  # noqa: F401 — фикстура шрифта с метриками устройства
from test_gui_video_source import CONNECTED, FAKE_DECODER, TEL, lit_fraction

QQuickStyle.setStyle("Fusion")
SIZES = [(1920, 1200), (1280, 800)]

RTSP_LONG = ("Камера ворот северная, въезд грузового транспорта (rtsp://admin:s3cret@192.168.1.64:554/"
             "Streaming/Channels/101)")
SOURCES = [
    {"id": "synthetic", "input": "synthetic", "name": "Испытательная таблица", "available": True, "active": True},
    {"id": "v4l2:/dev/v4l/by-path/platform-fe801000.csi-video-index0", "input": "hdmi",
     "name": "HDMI (TC358743, CSI-2)", "available": True, "active": False},
    {"id": "v4l2:/dev/v4l/by-id/usb-eMPIA_EasyCAP_UTV007-video-index0", "input": "cvbs",
     "name": "CVBS EasyCAP UTV007", "available": False, "active": False},
    {"id": "rtsp:1", "input": "rtsp", "name": RTSP_LONG, "available": True, "active": False},
    {"id": "v4l2:/dev/v4l/by-id/usb-MACROSILICON_USB_Video-video-index0", "input": "usb",
     "name": "USB-захват MS2130 (HDMI→UVC), лабораторный стенд, порт 3", "available": True, "active": False},
]
MANY = SOURCES + [{"id": f"rtsp:{n}", "input": "rtsp", "name": f"rtsp://192.168.10.{n}:554/stream1 — камера {n}",
                   "available": n % 3 != 0, "active": False} for n in range(2, 14)]       # 17 → на экране 16


def tel(sources, active_id=None, state="streaming"):
    src = copy.deepcopy(sources)
    if active_id is not None:
        for s in src:
            s["active"] = s["id"] == active_id
    act = next((s for s in src if s["active"]), src[0] if src else {"input": "synthetic"})
    return {**TEL, "sources": src, "source": {"input": act["input"], "state": state, "name": act.get("name", ""),
                                               "id": act.get("id", ""), "width": 1280, "height": 720, "fps": 30}}


RECONNECTING = {"peerLink": "RECONNECTING", "linkView": "CONNECTING", "peerState": "disconnected",
                "peerDetail": "heartbeat", "remoteAvailable": False, "linkRttMs": -1, "peerHeartbeatAge": 4.2}


class Peer:
    """Клиент канала с select_video_source: ok — блок принимает и отмечает источник активным в телеметрии;
    gate — ответ задерживается до gate.set(); reply — ответ блока (dict/исключение) без смены активного."""

    def __init__(self, report, reply=None, confirm=True):
        self.report, self.reply, self.confirm = report, reply, confirm
        self.calls, self.gate = [], threading.Event()
        self.gate.set()

    def select_video_source(self, source_id):
        self.calls.append(source_id)
        self.gate.wait(10)
        if isinstance(self.reply, BaseException):
            raise self.reply
        if self.reply is not None:
            return self.reply
        if self.confirm:
            self.report["telemetry_video"] = tel(self.report["telemetry_video"]["sources"], source_id)
        return {"ok": True}


class OldClient:
    """Клиент канала без select_video_source (до слияния с агентом B)."""


@pytest.fixture
def out_dir(tmp_path):
    d = Path(os.environ.get("SHEREGESH_GUI_SELECT_OUT") or tmp_path / "gui-select")
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def ui(qapp, tmp_path, wide_font):
    compile_translations()
    b = Backend(tmp_path / "data", autostart=False, video_command=FAKE_DECODER)
    report = {"mode": "remote", "telemetry_video": tel(SOURCES), "session_info": {"codec": "h264", "width": 1280,
                                                                                    "height": 720, "fps": 30}}
    b._video_report = lambda: report
    b.report = report
    b.peer = Peer(report)
    b._video_peer = lambda: b.peer
    u = Ui(b)
    b._status.update(CONNECTED)
    b.statusChanged.emit()
    b.video.configure("h264", 5600)
    b._update_video_source()
    u.window.setProperty("currentPage", PAGE_NAMES.index("video"))
    pump(100)
    yield u
    u.close()


def set_report(u, **kw):
    u.backend.report.update(kw)
    u.backend._update_video_source()
    pump(80)


def walk(item):
    """Визуальные потомки (делегаты ListView и содержимое кнопок — не дети по QObject)."""
    for ch in item.childItems():
        yield ch
        yield from walk(ch)


def items(u):
    out = [o for o in walk(find(u, "videoSources")) if o.objectName().startswith("videoSourceItem")]
    return sorted(out, key=lambda o: int(o.objectName().removeprefix("videoSourceItem")))


def item(u, source_id):
    return next(o for o in items(u) if o.property("sourceId") == source_id)


def label(it, name):
    return next(o for o in walk(it) if o.objectName() == name).property("text")


def tap(u, it):
    p = it.mapToScene(QPointF(it.width() / 2, it.height() / 2))
    QTest.mouseClick(u.window, Qt.LeftButton, Qt.NoModifier, QPoint(int(p.x()), int(p.y())))
    pump(80)


def text(u, name):
    return find(u, name).property("text")


def dialog_open(u):
    d = find(u, "videoSelectDialog")
    return bool(d.property("opened") or d.property("visible"))


# ---------------- без Qt: список и ответы ----------------
def test_sources_sanitized():
    raw = MANY + [{"id": "synthetic", "name": "дубль"}, {"id": "", "name": "пусто"}, "мусор", {"name": "без id"}]
    got = vs.sources({"sources": [dict(SOURCES[0])] + raw})
    assert len(got) == vs.MAX_SOURCES == 16
    assert len({s["id"] for s in got}) == 16 and sum(s["active"] for s in got) == 1
    rtsp = next(s for s in got if s["id"] == "rtsp:1")
    assert "s3cret" not in rtsp["name"] and "admin" not in rtsp["name"] and len(rtsp["name"]) <= 64
    assert set(got[0]) == {"id", "input", "name", "available", "active"}
    assert next(s for s in got if s["input"] == "cvbs")["available"] is False
    assert vs.sources({**TEL}) == [] and vs.sources(None) == [] and vs.sources({"sources": "x"}) == []
    assert vs.sources({"sources": [{"id": "rtsp:7", "kind": "IP"}]})[0] == {
        "id": "rtsp:7", "input": "rtsp", "name": "rtsp:7", "available": True, "active": False}


@pytest.mark.parametrize("result,code", [
    (None, ""), (True, ""), ({"ok": True}, ""), ("ok", ""),
    (False, "failed"), ({"ok": False, "error": "unknown-source"}, "unknown"),
    ({"ok": False, "error": "unavailable", "detail": "no device"}, "unavailable"),
    (KeyError("unknown source id rtsp:9"), "unknown"), (RuntimeError("not connected"), "no-link"),
    (TimeoutError("timeout"), "timeout"), ({"error": "busy"}, "busy"), (OSError("boom"), "failed"),
    ({"ok": False, "error": "not-supported"}, "unsupported"),
])
def test_select_error_mapping(result, code):
    assert select_error(result)[0] == code


@pytest.mark.parametrize("rx,display,live", [("playing", "playing", True), ("no-video", "reconnecting", False),
                                             ("off", "reconnecting", False), ("waiting", "reconnecting", False),
                                             ("unavailable", "unavailable", False)])
def test_reconnecting_follows_receiver(rx, display, live):
    """Канал управления восстанавливается: «нет видео/переподключение» — не раньше приёмника. Иначе экран
    опережал журнал, и разрыв короче NO_VIDEO_AFTER_S не записывался (E2E-12, CI ubuntu)."""
    d = vs.describe("remote", tel(SOURCES), {}, rx_state=rx, link="RECONNECTING")
    assert d["state"] == "reconnecting" and vs.display(d, rx) == {"display": display, "live": live}


def test_select_error_detail_without_credentials():
    assert "s3cret" not in select_error(RuntimeError("cannot open rtsp://admin:s3cret@10.0.0.5/x"))[1]


# ---------------- экран ----------------
@pytest.mark.test_id("TEST-UI-002")
def test_list_shows_name_input_availability_and_active(ui):
    its = items(ui)
    assert [o.property("sourceId") for o in its] == [s["id"] for s in SOURCES]
    syn, hdmi, cvbs, rtsp, usb = its
    assert label(syn, "videoSourceName") == "Испытательная таблица"
    assert label(syn, "videoSourceState") == "Синтетический · активный"
    assert label(hdmi, "videoSourceState") == "HDMI · доступен"
    assert label(cvbs, "videoSourceState") == "CVBS · недоступен"
    assert label(usb, "videoSourceState") == "USB (UVC) · доступен"
    assert "s3cret" not in label(rtsp, "videoSourceName") and label(rtsp, "videoSourceState") == "RTSP · доступен"
    # активный и недоступный не выбираются; остальные — да
    assert [o.property("enabled") for o in its] == [False, True, False, True, True]
    assert not find(ui, "videoSelectStatus").property("visible")
    tap(ui, cvbs)
    tap(ui, syn)
    assert not dialog_open(ui) and ui.backend.peer.calls == []


@pytest.mark.test_id("TEST-UI-002")
def test_select_requires_confirmation_and_calls_peer_with_id(ui):
    b = ui.backend
    hdmi_id = SOURCES[1]["id"]
    tap(ui, item(ui, hdmi_id))
    assert dialog_open(ui) and b.peer.calls == []                  # без подтверждения — ничего не отправлено
    assert "HDMI (TC358743, CSI-2)" in text(ui, "videoSelectQuestion") and "(HDMI)" in text(ui, "videoSelectQuestion")
    QMetaObject.invokeMethod(find(ui, "videoSelectDialog"), "reject")
    pump(80)
    assert b.peer.calls == [] and b.videoSelection["state"] == "idle"
    assert b.selectVideoSource(hdmi_id, False) is False and b.peer.calls == []   # слот без подтверждения не шлёт

    b.peer.gate.clear()                                            # блок отвечает не сразу
    tap(ui, item(ui, hdmi_id))
    QTest.keyClick(ui.window, Qt.Key_Return)                       # «ОК» — кнопка по умолчанию
    assert wait_until(lambda: b.peer.calls == [hdmi_id], 3), b.peer.calls
    pump(80)
    assert b.videoSelection["state"] == "switching"
    assert text(ui, "videoSelectStatus") == "Переключение на «HDMI (TC358743, CSI-2)»…"
    assert label(item(ui, hdmi_id), "videoSourceState") == "HDMI · переключение…"
    assert not any(o.property("enabled") for o in items(ui))     # во время переключения другие не выбираются
    assert b.selectVideoSource(SOURCES[3]["id"], True) is False and len(b.peer.calls) == 1
    b.peer.gate.set()
    assert wait_until(lambda: b.videoSelection["state"] == "idle", 3), b.videoSelection
    pump(80)
    assert label(item(ui, hdmi_id), "videoSourceState") == "HDMI · активный"
    assert label(item(ui, "synthetic"), "videoSourceState") == "Синтетический · доступен"
    assert not find(ui, "videoSelectStatus").property("visible")
    ui.window.setProperty("currentPage", PAGE_NAMES.index("dashboard"))
    pump(60)
    assert text(ui, "linkVideo").startswith("Видео (HDMI · HDMI (TC358743, CSI-2)): воспроизводится")


@pytest.mark.test_id("TEST-UI-002")
def test_switching_placeholder_while_new_source_starts(ui):
    """Блок принял запрос, источник запускается: в кадре «Переключение…», прежний кадр не выдаётся за новый."""
    b = ui.backend
    assert wait_until(lambda: b.video.state == "playing", 5)
    b.peer.confirm = False                                         # блок принял, но ещё не отметил активным
    assert b.selectVideoSource("rtsp:1", True)
    assert wait_until(lambda: b.peer.calls == ["rtsp:1"], 3)
    set_report(ui, telemetry_video={**tel(SOURCES), "source": {"input": "rtsp", "state": "starting"}})
    assert b.videoSelection["state"] == "switching" and not b.videoSource["live"]
    assert text(ui, "videoPlaceholder") == "Переключение…" and text(ui, "videoStateText") == "Переключение…"
    pump(300)
    assert lit_fraction(ui, find(ui, "videoView")) < 0.15
    set_report(ui, telemetry_video=tel(SOURCES, "rtsp:1"))
    assert b.videoSelection["state"] == "idle" and b.videoSource["live"]
    assert text(ui, "videoStateText") == "Воспроизведение"


@pytest.mark.test_id("TEST-UI-002")
@pytest.mark.parametrize("case,expect", [
    ("unknown", "Удалённый блок не знает источник «rtsp:99»; возможно, список источников изменился."),
    ("unavailable-local", "Источник «CVBS EasyCAP UTV007» на удалённом блоке недоступен; выберите другой источник."),
    ("unavailable-unit", "Источник «HDMI (TC358743, CSI-2)» на удалённом блоке недоступен; выберите другой источник. "
                         "/dev/video0 busy"),
    ("exception", "Переключение на «HDMI (TC358743, CSI-2)» не выполнено. link write failed"),
    ("timeout", "Удалённый блок не подтвердил переключение на «HDMI (TC358743, CSI-2)»."),
])
def test_select_errors_are_shown_as_text(ui, monkeypatch, case, expect):
    b = ui.backend
    hdmi = SOURCES[1]["id"]
    if case == "unknown":
        assert b.selectVideoSource("rtsp:99", True) is False and b.peer.calls == []
    elif case == "unavailable-local":
        assert b.selectVideoSource(SOURCES[2]["id"], True) is False and b.peer.calls == []
    else:
        b.peer.reply = {"unavailable-unit": {"ok": False, "error": "unavailable", "detail": "/dev/video0 busy"},
                        "exception": OSError("link write failed"), "timeout": None}[case]
        b.peer.confirm = case != "timeout"
        monkeypatch.setattr(backend_mod, "SELECT_TIMEOUT_S", 0.5)
        assert b.selectVideoSource(hdmi, True)
    assert wait_until(lambda: b.videoSelection["state"] == "error", 3), b.videoSelection
    pump(80)
    assert text(ui, "videoSelectStatus") == expect
    assert find(ui, "videoSelectStatus").property("visible")
    # после ошибки выбор снова возможен; удачный выбор убирает текст ошибки
    b.peer.reply, b.peer.confirm = None, True
    assert b.selectVideoSource(SOURCES[4]["id"], True)
    assert wait_until(lambda: b.videoSelection["state"] == "idle", 3)
    pump(80)
    assert not find(ui, "videoSelectStatus").property("visible")


@pytest.mark.test_id("TEST-UI-002")
def test_client_without_select_method(ui):
    """До слияния с агентом B у клиента канала нет select_video_source: список виден, выбор недоступен, не падает."""
    b = ui.backend
    b.peer = OldClient()
    b._update_video_source()
    pump(80)
    assert len(items(ui)) == len(SOURCES) and not any(o.property("enabled") for o in items(ui))
    assert text(ui, "videoSelectStatus") == ("Выбор источника недоступен: клиент канала связи этого пульта "
                                             "его не поддерживает.")
    assert b.selectVideoSource(SOURCES[1]["id"], True) is False
    assert b.videoSelection["error"] == "unsupported"
    assert text(ui, "videoSelectStatus") == "Выбор источника не поддерживается каналом связи с удалённым блоком."


@pytest.mark.test_id("TEST-UI-002")
def test_old_unit_without_sources(ui):
    """Блок без status.video.sources (1.1 и ранние): списка нет — только синтетический, выбор недоступен."""
    b = ui.backend
    set_report(ui, telemetry_video=dict(TEL))
    assert b.videoSources == [] and b.videoSelection["blocker"] == "no-list"
    assert not find(ui, "videoSources").property("visible")
    assert text(ui, "videoSelectStatus") == ("Удалённый блок не сообщает список источников: только синтетический "
                                             "источник, выбор недоступен.")
    assert "Синтетический" in text(ui, "videoSourceText")
    assert b.selectVideoSource("synthetic", True) is False and b.peer.calls == []


@pytest.mark.test_id("TEST-UI-002")
@pytest.mark.parametrize("state,placeholder,reason", [
    ("no-signal", "Нет сигнала", "Последний кадр не показывается"),
    ("no-device", "Нет видеоустройства", "Нет устройства захвата"),
    ("connect-error", "Ошибка подключения", "Не удаётся подключиться к потоку RTSP"),
    ("reconnecting", "Нет видео", "Переподключение к удалённому блоку"),     # REQ-SW-002: заголовок «нет видео»
])
def test_fault_states_with_list_hide_last_frame(ui, state, placeholder, reason):
    """Приёмник кадры получает, но источник в отказе — поле чёрное, причина названа. Связь восстанавливается —
    «нет видео» с причиной «Переподключение», как только кадров нет и по приёмнику."""
    b = ui.backend
    assert wait_until(lambda: b.video.state == "playing", 5)
    view = find(ui, "videoView")
    pump(300)
    assert lit_fraction(ui, view) > 0.6
    if state == "reconnecting":
        set_report(ui, link="RECONNECTING")
        pump(300)
        assert b.videoSource["live"] and lit_fraction(ui, view) > 0.6   # кадры ещё идут — они актуальны (E2E-12)
        b.video.configure(None, None)                            # кадров нет: приёмник выключен (TLS) / «нет видео»
        b._update_video_source()
    else:
        set_report(ui, telemetry_video=tel(SOURCES, "rtsp:1", state=state))
    pump(300)
    assert (b.video.state == "playing") == (state != "reconnecting") and not b.videoSource["live"]
    assert lit_fraction(ui, view) < 0.15 and not view.showing_frame()
    assert text(ui, "videoPlaceholder") == placeholder and reason in text(ui, "videoReason")
    if state == "reconnecting":                                   # сведения блока устарели: списка нет, выбор закрыт
        assert not find(ui, "videoSources").property("visible") and b.videoSelection["blocker"] == "no-link"
        ui.window.setProperty("currentPage", PAGE_NAMES.index("dashboard"))
        pump(60)
        assert text(ui, "linkVideo") == "Видео: переподключение"
        b.report.pop("link")
        b.video.configure("h264", 5600)
    else:
        assert label(item(ui, "rtsp:1"), "videoSourceState") == "RTSP · активный"
    set_report(ui, telemetry_video=tel(SOURCES))
    ui.window.setProperty("currentPage", PAGE_NAMES.index("video"))
    assert wait_until(lambda: view.showing_frame(), 3)


@pytest.mark.test_id("TEST-UI-002")
def test_link_lost_while_switching(ui):
    b = ui.backend
    b.peer.confirm = False
    assert b.selectVideoSource(SOURCES[1]["id"], True)
    assert wait_until(lambda: b.peer.calls, 3)
    set_report(ui, link="RECONNECTING")
    assert b.videoSelection["state"] == "error" and b.videoSelection["error"] == "no-link"
    assert text(ui, "videoSelectStatus") == "Нет связи с удалённым блоком; переключение на «HDMI (TC358743, CSI-2)» не подтверждено."


# ---------------- геометрия и снимки ----------------
def chip_problems(u):
    bad = []
    lst = rect(find(u, "videoSources"))
    frame = rect(find(u, "videoView"))
    if lst.intersects(frame.adjusted(2, 2, -2, -2)):
        bad.append("videoSources∩videoView")
    for it in items(u):
        r = rect(it)
        if not r.intersects(lst):
            continue                                               # за пределами прокрутки — не видна
        for o in walk(it):
            if o.objectName() in ("videoSourceName", "videoSourceState"):
                if o.property("contentWidth") > o.property("width") + 1:
                    bad.append(f"{it.property('sourceId')}/{o.objectName()} шире элемента")
                if not r.adjusted(-1, -1, 1, 1).contains(rect(o)):
                    bad.append(f"{it.property('sourceId')}/{o.objectName()} вне элемента")
    st = find(u, "videoSelectStatus")
    if st.property("visible") and st.property("contentWidth") > st.width() + 1:
        bad.append("videoSelectStatus шире элемента")
    return bad


SHOTS = {
    "list": lambda u: None,
    "list-16": lambda u: set_report(u, telemetry_video=tel(MANY, "rtsp:1")),
    "confirm-dialog": lambda u: tap(u, item(u, "rtsp:1")),
    "switching": lambda u: (setattr(u.backend.peer, "confirm", False), u.backend.selectVideoSource("rtsp:1", True),
                            wait_until(lambda: u.backend.peer.calls, 3),
                            set_report(u, telemetry_video={**tel(SOURCES), "source": {"input": "rtsp",
                                                                                      "state": "starting"}})),
    "error-unavailable": lambda u: u.backend.selectVideoSource(SOURCES[2]["id"], True),
    "rtsp-active-connect-error": lambda u: set_report(u, telemetry_video=tel(SOURCES, "rtsp:1",
                                                                              state="connect-error")),
    "reconnecting": lambda u: (u.backend._status.update(RECONNECTING), u.backend.statusChanged.emit(),
                               u.backend.video.configure(None, None), set_report(u, link="RECONNECTING")),
    "old-unit-no-list": lambda u: set_report(u, telemetry_video=dict(TEL)),
    "client-without-select": lambda u: (setattr(u.backend, "peer", OldClient()), u.backend._update_video_source()),
}


@pytest.mark.test_id("TEST-UI-002")
@pytest.mark.parametrize("shot", list(SHOTS))
def test_snapshots_and_geometry_with_source_list(ui, out_dir, wide_font, shot):
    b = ui.backend
    assert wait_until(lambda: b.video.state == "playing", 5)
    SHOTS[shot](ui)
    pump(150)
    report = []
    try:
        for w, h in SIZES:
            ui.window.resize(w, h)
            pump(250)
            for page in ("video", "dashboard"):
                ui.window.setProperty("currentPage", PAGE_NAMES.index(page))
                pump(400 if page == "video" else 100)
                problems = outside_window(ui) + header_problems(ui)
                if page == "video":
                    problems += video_problems(ui) + (chip_problems(ui) if b.videoSources else [])
                    for n in ("videoSourceText", "videoInfo"):
                        t = find(ui, n)
                        if t.property("contentWidth") > t.width() + 1:
                            problems.append(f"{n} шире элемента")
                else:
                    problems += tile_overflow(ui) + text_problems(ui)
                if shot == "confirm-dialog":
                    assert dialog_open(ui) and b.peer.calls == []
                    if page == "dashboard":
                        continue                                   # диалог — на странице «Видео»
                png = save_small(ui, out_dir / f"{w}x{h}-{shot}-{page}.png")
                report.append({"size": f"{w}x{h}", "page": page, "png": png.name,
                               "display": b.videoSource["display"], "live": b.videoSource["live"],
                               "selection": {k: b.videoSelection[k] for k in ("state", "id", "error", "blocker")},
                               "sources": len(b.videoSources),
                               "status": text(ui, "videoSelectStatus") if page == "video" else None,
                               "link_video": text(ui, "linkVideo") if page == "dashboard" else None,
                               "problems": problems})
                assert problems == [], f"{w}×{h} {shot} {page}: {problems}"
    finally:
        (out_dir / f"geometry-{shot}.json").write_text(json.dumps(
            {"note": "offscreen, QT_QUICK_BACKEND=software (L3), шрифт с метриками устройства "
                     f"({wide_font or 'системный'}) — не проверка HDMI/cage/сенсора и не переключение на настоящем "
                     "блоке; список источников и ответы блока заданы подменой (status.video.sources, "
                     "select_video_source)",
             "checks": ["элементы в пределах окна", "текст плиток в пределах плитки", "подписи видео в пределах кадра",
                        "надписи источников не шире и не вне своей кнопки", "список не перекрывает кадр",
                        "индикаторы заголовка не перекрываются"],
             "shots": report}, ensure_ascii=False, indent=1), encoding="utf-8")
