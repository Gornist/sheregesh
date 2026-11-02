# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Интерфейс Minimal (MainMinimal.qml): матрица GUI-01…GUI-12 (offscreen, L3, подменные данные).

Кадры даёт подменный декодер (test_gui_video_source.FAKE_DECODER, 25 кадров/с), состояние источника — подмена
VideoOps.report, аппаратура и клиент канала — FakeSource/FakeClient (test_gui_sticks), модуль — телеметрия
status.module (test_gui_module). Проверяется геометрия 1280×800 и 1920×1200, строка состояния, нижнее меню, пять
разделов и их закрытие, выбор источника, «нет кадров» и «нет связи с блоком» как разные состояния, восстановление,
USB-ввод, внешний модуль (SIM), и что навигация по меню не роняет показ кадров и не меняет поле видео.

Это не проверка физического дисплея, сенсора, платы и блока: только окно пульта на подменных данных. Снимки:
SHEREGESH_MINIMAL_OUT=DIR — туда; иначе во временный каталог.
"""
import os
import time
from pathlib import Path

import pytest

from sheregesh import PROTOCOL_VERSION
from PySide6.QtCore import QObject, QPointF, Qt
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from sheregesh_console.app import Ui, compile_translations
from sheregesh_console.backend import Backend
from sheregesh_console.journal import MemoryJournal
from sheregesh_console.qtwait import pump, wait_until
from test_gui_module import READY, tel
from test_gui_sticks import CONNECTED, LOST, FakeClient, FakeSource, rc_state
from test_gui_tile_geometry import wide_font  # noqa: F401 — фикстура шрифта с метриками устройства
from test_gui_video_select import RECONNECTING, SOURCES as SELECT_SOURCES, Peer as SelectPeer, tel as select_tel
from test_gui_video_source import FAKE_DECODER, STATES, lit_fraction

QQuickStyle.setStyle("Fusion")
SECTIONS = ["video", "control", "module", "diagnostics", "system"]


@pytest.fixture
def out_dir(tmp_path):
    d = Path(os.environ.get("SHEREGESH_MINIMAL_OUT") or tmp_path / "minimal")
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def ui(qapp, tmp_path, wide_font):
    compile_translations()
    src = FakeSource(present=True)
    b = Backend(tmp_path / "data", autostart=False, journal=MemoryJournal(), stick_source=src,
                video_command=FAKE_DECODER)
    b._sticks_peer = lambda: b.peer
    b.peer, b.src = FakeClient(), src
    report = {"mode": "remote", **STATES["synthetic-stream"]}
    b.videoops.report = lambda: report
    b.report = report
    u = Ui(b, layout="minimal")
    u.window.resize(1280, 800)
    feed(u)
    b.video.configure("h264", 5600)
    assert wait_until(lambda: b.video.state == "playing", 5), b.video.metrics()
    yield u
    u.close()


def feed(u, status=CONNECTED, module=READY, rc="IDLE", source="synthetic-stream"):
    """Как Backend._refresh: канал, видео блока, модуль (status.module), rc-state стиков; строка состояния — через 0,3 с."""
    b = u.backend
    b._status.update(status)
    connected = status["peerLink"] == "CONNECTED"
    b.peer.phase = "connected" if connected else "connecting"
    if rc is not None:
        b.peer.rc_state = rc_state(rc, "start" if rc == "IDLE" else "fresh", time.monotonic())
    b.report.clear()
    b.report.update({"mode": "remote", "link": status["peerLink"], **STATES[source]})   # как VideoOps.report
    b.videoops.update()
    tele = {"state": "normal", "module": module} if module is not None else {"state": "normal"}
    from types import SimpleNamespace
    b._update_module(SimpleNamespace(peer=SimpleNamespace(telemetry=tele if connected else None, session="1.4")))
    b.sticks.update_link(status["peerLink"], "1.4" if connected else None)
    b.statusChanged.emit()
    pump(350)


def walk(item):
    for ch in item.childItems():
        yield ch
        yield from walk(ch)


def find_opt(u, name):
    return u.window.findChild(QObject, name) or next((i for i in walk(u.window.contentItem()) if i.objectName() == name),
                                                     None)


def find(u, name):
    o = u.window.findChild(QObject, name) or next((i for i in walk(u.window.contentItem()) if i.objectName() == name),
                                                  None)
    assert o is not None, name
    return o


def wfind(u, name):
    assert wait_until(lambda: find_opt(u, name) is not None, 3), name        # содержимое панели — асинхронно
    return find(u, name)


def geom(item):
    p = item.mapToScene(QPointF(0, 0))
    return p.x(), p.y(), item.width(), item.height()


def tap(u, name):
    assert wait_until(lambda: find_opt(u, name) is not None, 3), name        # панели создаются асинхронно
    item = find(u, name)
    x, y, w, h = geom(item)
    QTest.mouseClick(u.window, Qt.LeftButton, Qt.NoModifier, QPointF(x + w / 2, y + h / 2).toPoint())
    pump(120)


def status(u, key):
    return find(u, "status" + key.capitalize()).property("value")


def shot(u, out_dir, name):
    pump(150)
    u.grab(out_dir / f"{name}.png")


# ---------------- GUI-01…GUI-05: каркас ----------------
@pytest.mark.test_id("TEST-UI-002")
def test_gui01_minimal_starts_with_video_field(ui, out_dir):
    assert ui.window.objectName() == "mainWindow"
    for name in ("statusBar", "navBar", "videoField", "videoView"):
        find(ui, name)
    assert ui.window.property("section") == "" and not find(ui, "sidePanel").property("visible")
    assert find(ui, "videoView").showing_frame()
    shot(ui, out_dir, "gui01-1280x800-video")


@pytest.mark.test_id("TEST-UI-002")
@pytest.mark.parametrize("size,bar,nav", [((1280, 800), 28, 44), ((1920, 1200), 42, 66)])
def test_gui02_status_bar_one_line_and_geometry(ui, out_dir, size, bar, nav):
    ui.window.resize(*size)
    pump(200)
    w, h = size
    sb, field, nb = find(ui, "statusBar"), find(ui, "videoField"), find(ui, "navBar")
    assert geom(sb) == (0, 0, w, bar) and geom(nb) == (0, h - nav, w, nav)
    assert geom(field) == (0, bar, w, h - bar - nav)
    assert field.height() / h >= 0.90, field.height() / h          # 728/800 = 91 %; кадр 16:9 — 720 строк
    # пять полей в одну строку, без переноса и без наезда на метку LAB справа
    lab_x = geom(find(ui, "statusLab"))[0]
    for key in ("control", "link", "video", "input", "module"):
        item = find(ui, "status" + key.capitalize())
        x, y, iw, ih = geom(item)
        assert status(ui, key), key
        assert ih == bar and x + iw <= lab_x, (key, x, iw, lab_x)
    assert status(ui, "link") == "на связи" and "IDLE" in status(ui, "control")
    shot(ui, out_dir, f"gui02-{w}x{h}-status")


@pytest.mark.test_id("TEST-UI-002")
def test_gui03_bottom_nav_five_equal_touch_targets(ui):
    buttons = [find(ui, "nav_" + s) for s in SECTIONS]
    widths = {round(b.width()) for b in buttons}
    assert len(widths) == 1 and all(b.height() >= 44 for b in buttons)
    assert [b.property("modelData")["label"] for b in buttons] == ["Видео", "Управление", "Модуль", "Диагностика",
                                                                   "Система"]


@pytest.mark.test_id("TEST-UI-002")
def test_gui04_all_five_sections_open_over_video(ui, out_dir):
    field0 = geom(find(ui, "videoField"))
    for s in SECTIONS:
        tap(ui, "nav_" + s)
        assert ui.window.property("section") == s
        panel = find(ui, "sidePanel")
        assert panel.property("visible")
        assert wait_until(lambda: find(ui, "panelLoader_" + s).property("item") is not None, 3), s
        x, y, w, h = geom(panel)
        assert w <= 0.32 * 1280 + 1 and x > 1280 / 2, "панель справа, центр кадра виден"
        assert geom(find(ui, "videoField")) == field0, "поле видео не меняется"
        assert find(ui, "nav_" + s).property("current")
        shot(ui, out_dir, f"gui04-1280x800-{s}")


@pytest.mark.test_id("TEST-UI-002")
def test_gui05_panel_closes_by_cross_tap_on_video_escape_and_same_button(ui):
    tap(ui, "nav_control")
    tap(ui, "panelClose")
    assert ui.window.property("section") == ""
    tap(ui, "nav_module")
    QTest.mouseClick(ui.window, Qt.LeftButton, Qt.NoModifier, QPointF(200, 400).toPoint())   # тап по видео
    pump(80)
    assert ui.window.property("section") == ""
    tap(ui, "nav_system")
    ui.window.requestActivate()
    QTest.keyClick(ui.window, Qt.Key_Escape)
    pump(80)
    assert ui.window.property("section") == ""
    tap(ui, "nav_video")
    tap(ui, "nav_video")
    assert ui.window.property("section") == ""
    QTest.keyClick(ui.window, Qt.Key_F4)                              # без сенсора: F1…F5
    pump(80)
    assert ui.window.property("section") == "diagnostics"


# ---------------- GUI-06…GUI-09: видео и связь ----------------
@pytest.mark.test_id("TEST-UI-002")
def test_gui06_video_source_switch_needs_confirmation(ui):
    b = ui.backend
    b.report["telemetry_video"] = select_tel(SELECT_SOURCES)
    peer = SelectPeer(b.report)
    b.videoops.peer = lambda: peer
    b.videoops.update()
    b.statusChanged.emit()
    pump(200)
    tap(ui, "nav_video")
    assert not wfind(ui, "miniSource0").property("enabled"), "активный источник не выбирается повторно"
    tap(ui, "miniSource1")
    question = wfind(ui, "miniSourceQuestion")            # текст диалога подтверждения (REQ-SYS-009)
    assert wait_until(lambda: question.isVisible(), 3) and "«" in question.property("text")
    assert peer.calls == []
    QTest.keyClick(ui.window, Qt.Key_Escape)               # без сенсора: Esc — отмена, ничего не переключается
    assert wait_until(lambda: not question.isVisible(), 3)
    pump(200)
    assert peer.calls == []
    tap(ui, "miniSource1")
    assert wait_until(lambda: wfind(ui, "miniSourceQuestion").isVisible(), 3)
    QTest.keyClick(ui.window, Qt.Key_Return)               # Enter — подтверждение
    assert wait_until(lambda: peer.calls == [SELECT_SOURCES[1]["id"]], 5), peer.calls
    b.videoops.update()
    assert wait_until(lambda: b.videoSource.get("input") == "hdmi", 5), b.videoSource
    pump(350)
    assert status(ui, "video").startswith("HDMI")


@pytest.mark.test_id("TEST-UI-002")
def test_gui07_no_frames_is_not_link_loss(ui, out_dir):
    feed(ui, source="cvbs-no-signal")
    assert wait_until(lambda: find(ui, "videoMessage").property("visible"), 3)
    title = find(ui, "videoMessageTitle").property("text")
    assert title == "Нет сигнала", title
    assert status(ui, "link") == "на связи" and "нет сигнала" in status(ui, "video")
    assert lit_fraction(ui, find(ui, "videoField")) < 0.2, "прежний кадр не показывается"
    shot(ui, out_dir, "gui07-no-signal")


@pytest.mark.test_id("TEST-UI-002")
def test_gui08_ethernet_loss_shown_as_no_link(ui, out_dir):
    # кабель выдернут: канал управления переподключается и кадры не идут (пока кадры идут, видео актуально и без
    # канала управления — videosource.display)
    ui.backend.video.stop()
    feed(ui, status=RECONNECTING, rc=None)
    assert wait_until(lambda: find(ui, "videoMessage").property("visible"), 3)
    assert find(ui, "videoMessageTitle").property("text") == "НЕТ СВЯЗИ С БЛОКОМ"
    assert status(ui, "link") == "нет связи · переподключение" and status(ui, "video") == "нет видео"
    assert status(ui, "control").startswith("нет связи")
    shot(ui, out_dir, "gui08-link-lost")


@pytest.mark.test_id("TEST-SYS-008")
def test_gui08b_incompatible_unit_shows_versions_not_link_loss(ui):
    """Несовместимая версия блока — не «нет связи»: заголовок и версии протокола обеих сторон (стенд 2026-10-31:
    Minimal показывал «НЕТ СВЯЗИ С БЛОКОМ» при peerState incompatible)."""
    ui.backend.video.stop()
    feed(ui, status={**RECONNECTING, "peerLink": "INCOMPATIBLE", "linkView": "ERROR", "peerState": "incompatible",
                     "peerDetail": "major", "peerProtocol": "2.0"}, rc=None)
    assert wait_until(lambda: find(ui, "videoMessage").property("visible"), 3)
    assert find(ui, "videoMessageTitle").property("text") == "НЕСОВМЕСТИМАЯ ВЕРСИЯ БЛОКА"
    text = find(ui, "videoMessageText").property("text")
    assert f"пульт {PROTOCOL_VERSION}" in text and "блок 2.0" in text and "(major)" in text, text


@pytest.mark.test_id("TEST-UI-002")
def test_gui09_recovery_hides_message_and_shows_frames(ui):
    ui.backend.video.stop()
    feed(ui, status=RECONNECTING, rc=None)
    assert wait_until(lambda: find(ui, "videoMessage").property("visible"), 3)
    feed(ui, source="cvbs-no-signal")
    ui.backend.video.configure("h264", 5600)
    feed(ui)
    assert wait_until(lambda: not find(ui, "videoMessage").property("visible")
                      and find(ui, "videoView").showing_frame(), 5)
    assert lit_fraction(ui, find(ui, "videoField")) > 0.5
    assert status(ui, "link") == "на связи"


# ---------------- GUI-10, GUI-11: устройства ----------------
@pytest.mark.test_id("TEST-UI-002")
def test_gui10_usb_input_state(ui, out_dir):
    assert status(ui, "input") == "Boxer"
    ui.backend.src.unplug()
    ui.backend.sticks.refresh()
    pump(350)
    assert status(ui, "input") == "нет устройства"
    tap(ui, "nav_control")
    assert wfind(ui, "miniControlDevice").property("value") == "не подключено"
    shot(ui, out_dir, "gui10-usb-unplugged")
    ui.backend.src.plug()
    ui.backend.sticks.refresh()
    pump(350)
    assert status(ui, "input") == "Boxer"


@pytest.mark.test_id("TEST-UI-002")
def test_gui11_external_module_state_and_sim_mark(ui, out_dir):
    assert status(ui, "module") == "Nomad SIM · готов"
    tap(ui, "nav_module")
    assert wfind(ui, "miniModuleSim").property("visible")
    assert wfind(ui, "miniModuleState").property("value") == "Готов"
    feed(ui, module=tel("FAULT", "iface-fault", 23, error={"code": "MOD-IFACE-ERROR", "text": "interface error",
                                                              "since_s": 1.0, "state": "READY", "active": True}))
    assert status(ui, "module") == "Nomad SIM · неисправность"
    shot(ui, out_dir, "gui11-module-fault")
    feed(ui, status=LOST, rc=None)
    assert status(ui, "module") == "нет данных"


# ---------------- GUI-12: навигация не мешает видео ----------------
@pytest.mark.test_id("TEST-UI-002")
def test_gui12_navigation_does_not_degrade_video(ui):
    rx, field = ui.backend.videoReceiver, find(ui, "videoField")
    field0 = geom(field)

    def shown(seconds, navigate):
        n0, end, i = rx.shown_seq(), time.monotonic() + seconds, 0
        while time.monotonic() < end:
            if navigate:
                ui.window.setProperty("section", (SECTIONS + [""])[i % 6])
                i += 1
            pump(150)
            assert geom(field) == field0
        return (rx.shown_seq() - n0) / seconds
    idle = shown(3.0, False)
    nav = shown(3.0, True)
    assert idle > 15 and nav >= 0.85 * idle, (idle, nav)


@pytest.mark.test_id("TEST-SW-001")
def test_gui_diagnostics_lists_mandatory_services_of_both_devices(ui):
    """Состояние каждой обязательной службы видно в диагностике пульта (для блока — через status)."""
    rows = [{"side": "console", "name": "video-rx", "state": "active", "restarts": 0},
            {"side": "remote", "name": "video-tx", "state": "failed", "restarts": 2}]
    ui.backend._status["services"] = rows
    ui.backend.statusChanged.emit()
    tap(ui, "nav_diagnostics")
    assert wait_until(lambda: find_opt(ui, "miniSvc_video-tx") is not None, 3)
    rx, tx = find(ui, "miniSvc_video-rx"), find(ui, "miniSvc_video-tx")
    assert rx.property("label") == "Служба пульта: video-rx" and rx.property("value") == "active · перезапусков 0"
    assert tx.property("label") == "Служба блока: video-tx" and tx.property("value") == "failed · перезапусков 2"


@pytest.mark.test_id("TEST-SYS-009")
def test_sticks_transmission_enabled_only_through_confirmation_dialog(ui):
    """Включение передачи — отдельный диалог: Esc — передача не включается, Enter — включается; выключение — сразу."""
    sv = ui.backend.sticks
    tap(ui, "nav_control")
    assert wait_until(lambda: wfind(ui, "miniSticksToggle").property("enabled"), 3), sv.support
    tap(ui, "miniSticksToggle")
    question = wfind(ui, "miniSticksQuestion")
    assert wait_until(lambda: question.isVisible(), 3) and not sv.enabled
    QTest.keyClick(ui.window, Qt.Key_Escape)
    assert wait_until(lambda: not question.isVisible(), 3)
    pump(200)
    assert not sv.enabled
    tap(ui, "miniSticksToggle")
    question = wfind(ui, "miniSticksQuestion")
    assert wait_until(lambda: question.isVisible(), 3)
    QTest.keyClick(ui.window, Qt.Key_Return)               # по умолчанию в фокусе «Отмена»: Enter — отказ
    assert wait_until(lambda: not question.isVisible(), 3)
    pump(200)
    assert not sv.enabled
    tap(ui, "miniSticksToggle")
    assert wait_until(lambda: wfind(ui, "miniSticksQuestion").isVisible(), 3)
    QTest.keyClick(ui.window, Qt.Key_Backtab)              # к «Включить передачу», Enter — подтверждение
    QTest.keyClick(ui.window, Qt.Key_Return)
    assert wait_until(lambda: sv.enabled, 3)
    tap(ui, "miniSticksToggle")                            # выключение — без диалога
    assert wait_until(lambda: not sv.enabled, 3)
