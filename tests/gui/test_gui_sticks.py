# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Страница «Управление» пульта (S7, ADR-0013, 4.2; offscreen, L3).

Источник стиков и клиент канала подменные (test_console_sticks: FakeSource, FakeClient по контракту ADR-0013, 4.1);
канал и сеанс — как Backend._refresh передаёт их в SticksView.update_link. Проверяется: вкладка (F9, прежние индексы
0…7 не изменились), objectName, русские тексты состояний блока и support, плашка «Радиовыхода нет», переключатель
передачи только с подтверждением, выключение сразу, пропажа устройства, ui-state, геометрия на 1280×800
(Waveshare 10.1) и 1920×1200 со шрифтом устройства. Снимки: SHEREGESH_S7_GUI_OUT=docs/acceptance/s7-sticks/
screenshots-offscreen — туда; иначе во временный каталог.

Это не проверка физического дисплея, аппаратуры и блока: только экран пульта на подменных данных.
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QPoint, QPointF, Qt
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from sheregesh_console import sticks as sticks_mod
from sheregesh_console.app import PAGE_NAMES, Ui, compile_translations, sticks_ui_state
from sheregesh_console.backend import Backend
from sheregesh_console.journal import MemoryJournal
from sheregesh_console.qtwait import finish, pump
from test_e2e_gui_screens import header_problems, outside_window, rect, save_small
from test_gui_tile_geometry import wide_font  # noqa: F401 — фикстура шрифта с метриками устройства

QQuickStyle.setStyle("Fusion")
SIZES = [(1280, 800), (1920, 1200)]
CONNECTED = {"peerLink": "CONNECTED", "linkView": "CONNECTED", "peerState": "connected", "peerDetail": "ok",
             "peerDevice": "remote-emu-01", "peerSoftware": "0.1.0.dev0", "peerProtocol": "1.4", "peerMode": "network",
             "linkSecure": True, "linkTls": "TLSv1.3", "linkHost": "192.168.1.171", "remoteAvailable": True}
LOST = {"peerLink": "DISCONNECTED", "linkView": "DISCONNECTED", "peerState": "lost", "peerDetail": "heartbeat",
        "remoteAvailable": False, "peerMode": "network"}
AXES = [0, -12, 1000, 3, -1000, 640, -355, 1000]
BOXER_NAME = "OpenTX Radiomaster Boxer Joystick"          # RadioMaster Boxer (EdgeTX), ADR-0013, раздел 1


class FakeSource:
    """StickSource без устройства: состояние как у RadioMaster Boxer (8 осей, 24 кнопки); plug/unplug — горячее
    подключение (поколение устройства растёт при каждом подключении)."""

    def __init__(self, present=False):
        self.st = {**sticks_mod.empty_state(), "name": "", "axis_count": 8, "button_count": 24}
        if present:
            self.plug()

    def state(self):
        return dict(self.st, axes=list(self.st["axes"]))

    def plug(self):
        self.st.update(present=True, name=BOXER_NAME, axes=[0, -12, 1000, 3, -1000, 0, 0, 1000], buttons=5,
                       generation=self.st["generation"] + 1)

    def unplug(self):
        self.st.update(present=False, axes=[0] * 8, buttons=0, lost=self.st["lost"] + 1)

    def start(self):
        return self

    def stop(self):
        pass


class FakeClient:
    """PeerClient по контракту ADR-0013, 4.1: send_rc → queued | offline | unsupported; snapshot с rc_state."""

    def __init__(self):
        self.session, self.phase, self.connects = "1.4", "connected", 1
        self.frames, self.rc_state = [], None

    def send_rc(self, body):
        if self.phase != "connected":
            return "offline"
        if self.session != "1.4":
            return "unsupported"
        self.frames.append(body)
        return "queued"

    def snapshot(self):
        return {"phase": self.phase, "session": self.session if self.phase == "connected" else None,
                "counters": {"connects": self.connects}, "rc_state": self.rc_state,
                "rc_sent": {"n": len(self.frames), "at": None, "pending": False, "ring": []}}


def rc_state(state, reason, rx_at, ch=None, btn=0, **kw):
    """rc-state блока после sanitize_rc_state + поля клиента (rx_at, rx_age_s, latency_ms), ADR-0013, 3.2."""
    return {"v": 1, "state": state, "reason": reason, "since_s": 12.3, "n": 1234, "age_ms": 18, "rate_hz": 49.8,
            "gaps": 0, "stale": 0, "bad": 0, "late_max_ms": 41, "failsafes": 1, "timeout_ms": 500,
            "ch": list(ch or [0] * 8), "btn": btn, "dev": {"name": BOXER_NAME, "ok": True}, "output": "none",
            "rx_at": rx_at, "rx_age_s": 0.0, "latency_ms": 18.4, **kw}


OBJECT_NAMES = ["sticksPage", "tabSticks", "sticksOutputBanner", "sticksOutputText", "sticksRemoteStateBadge",
                "sticksRemoteStateText", "sticksRemoteReasonText", "sticksEnableSwitch", "sticksNoticeText",
                "sticksChannelsTile", "sticksDeviceTile", "sticksDeviceText", "sticksDeviceDetail", "sticksButtonsTile",
                "sticksLinkStats", "sticksRateText", "sticksLatencyText", "sticksGapsText", "sticksFailsafesText",
                "sticksConfirmDialog"]
BLOCKS = ["sticksOutputBanner", "sticksRemoteStateBadge", "sticksChannelsTile", "sticksDeviceTile",
          "sticksButtonsTile"]


@pytest.fixture
def out_dir(tmp_path):
    d = Path(os.environ.get("SHEREGESH_S7_GUI_OUT") or tmp_path / "s7-gui")
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def ui(qapp, tmp_path, wide_font):
    compile_translations()
    src, client = FakeSource(present=False), FakeClient()
    b = Backend(tmp_path / "data", autostart=False, journal=MemoryJournal(), stick_source=src)
    b._sticks_peer = lambda: b.peer
    b.peer, b.src = client, src
    u = Ui(b)
    u.window.resize(1280, 800)
    u.window.setProperty("currentPage", PAGE_NAMES.index("sticks"))
    pump(100)
    yield u
    u.close()


def feed(u, status=None, session="1.4", rc=None, steps=0):
    """Как Backend._refresh: поля канала в status → SticksView.update_link; rc-state — в snapshot клиента."""
    b = u.backend
    st = status or CONNECTED
    b._status.update(st)
    b.peer.phase = "connected" if st["peerLink"] == "CONNECTED" else "connecting"
    b.peer.session = session
    if rc is not None:
        b.peer.rc_state = rc
    for _ in range(steps):                       # такты передачи 50 Гц
        b.sticks.sender.step(b.sticks.sender._clock())
    # status блока без status.module (как S7): плашка выхода «Управления» — по ModuleView (S8, ADR-0014, 3.3)
    b._update_module(SimpleNamespace(peer=SimpleNamespace(telemetry={"state": "normal"}, session=session)))
    b.sticks.update_link(st["peerLink"], session if st["peerLink"] == "CONNECTED" else None)
    b.statusChanged.emit()
    pump(60)


def walk(item):
    for ch in item.childItems():
        yield ch
        yield from walk(ch)


def find(u, name):
    o = u.window.findChild(QObject, name) or next((i for i in walk(u.window.contentItem()) if i.objectName() == name),
                                                  None)
    assert o is not None, name
    return o


def text(u, name):
    return find(u, name).property("text")


def visible(u, name):
    return find(u, name).isVisible()


def center(item):
    p = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
    return QPoint(int(p.x()), int(p.y()))


def page_problems(u):
    """Видимые элементы страницы в её пределах; текст не шире элемента и не обрезан; блоки не перекрываются;
    вкладка «Управление» видна в заголовке."""
    page = rect(find(u, "sticksPage")).adjusted(-1, -1, 1, 1)
    bad = []

    def walk_(item):
        for ch in item.childItems():
            if not ch.isVisible() or ch.width() <= 0 or ch.height() <= 0:
                continue
            name = ch.objectName() or ch.metaObject().className()
            if not page.contains(rect(ch)):
                bad.append(f"{name} вне страницы")
            if ch.inherits("QQuickText") and ch.property("text"):
                if ch.property("truncated"):
                    bad.append(f"{name} «{ch.property('text')[:40]}» обрезан")
                elif ch.property("contentWidth") > ch.width() + 1:
                    bad.append(f"{name} «{ch.property('text')[:40]}» шире элемента")
            walk_(ch)
    walk_(find(u, "sticksPage"))
    blocks = [n for n in BLOCKS if visible(u, n)]
    for i, a in enumerate(blocks):
        for b in blocks[i + 1:]:
            if rect(find(u, a)).adjusted(1, 1, -1, -1).intersects(rect(find(u, b))):
                bad.append(f"{a}∩{b}")
    sw = rect(find(u, "sticksEnableSwitch"))
    if rect(find(u, "sticksRemoteReasonText")).adjusted(1, 1, -1, -1).intersects(sw):
        bad.append("sticksRemoteReasonText∩sticksEnableSwitch")
    tab, bar = rect(find(u, "tabSticks")), rect(find(u, "tabBar"))
    if tab.right() > bar.right() + 1:
        bad.append("вкладка «Управление» за краем панели вкладок")
    return bad


def check_screen(u, name, out_dir, report):
    for w, h in SIZES:
        u.window.resize(w, h)
        pump(200)
        problems = outside_window(u) + header_problems(u) + page_problems(u)
        shot = save_small(u, out_dir / f"{w}x{h}-{name}.png")
        report.append({"size": f"{w}x{h}", "state": name, "png": shot.name, "problems": problems,
                       "ui": sticks_ui_state(u)})
        assert problems == [], f"{w}×{h} {name}: {problems}"
    u.window.resize(1280, 800)
    pump(60)


@pytest.fixture(scope="module")
def report():
    rows = []
    yield rows
    if rows and os.environ.get("SHEREGESH_S7_GUI_OUT"):
        (Path(os.environ["SHEREGESH_S7_GUI_OUT"]) / "screens.json").write_text(json.dumps(
            {"note": "offscreen, QT_QUICK_BACKEND=software, шрифт устройства (DejaVu Sans/Verdana) — L3, "
                     "не проверка физического дисплея; аппаратура и блок подменные (ADR-0013, 3.2, 4.1)",
             "checks": ["элементы в пределах окна и страницы", "текст не обрезан и не шире элемента",
                        "блоки страницы не перекрываются", "индикаторы заголовка не перекрываются",
                        "вкладка «Управление» видна на панели вкладок"],
             "shots": rows}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


# ---------------- вкладка ----------------
def test_sticks_tab_is_last_and_previous_indices_unchanged(ui):
    assert PAGE_NAMES[:8] == ["dashboard", "video", "diagnostics", "power", "logs", "settings", "about", "module"]
    assert PAGE_NAMES[8] == "sticks" and text(ui, "tabSticks") == "Управление"
    ui.window.requestActivate()
    ui.window.setProperty("currentPage", 0)
    QTest.keyClick(ui.window, Qt.Key_F9)
    assert ui.window.property("currentPage") == 8 and visible(ui, "sticksPage")
    QTest.keyClick(ui.window, Qt.Key_F8)
    assert PAGE_NAMES[ui.window.property("currentPage")] == "module"
    QTest.keyClick(ui.window, Qt.Key_9, Qt.ControlModifier)
    assert PAGE_NAMES[ui.window.property("currentPage")] == "sticks"


def test_all_object_names_present(ui):
    feed(ui)
    for n in OBJECT_NAMES:
        find(ui, n)
    for i in range(8):
        find(ui, f"sticksChannel_{i}")
        find(ui, f"sticksAxisValue_{i}")
    for i in range(24):
        find(ui, f"sticksButton_{i}")


# ---------------- состояния (снимки) ----------------
def test_no_device(ui, out_dir, report):
    feed(ui, rc=rc_state("IDLE", "start", 1.0, n=0, rate_hz=0.0, failsafes=0, age_ms=None, latency_ms=None))
    sv = ui.backend.sticks
    assert sv.support == "no-device" and not sv.present and not sv.enabled
    assert text(ui, "sticksDeviceText") == "Аппаратура не подключена"
    assert text(ui, "sticksDeviceDetail") == "Подключите аппаратуру по USB в режиме USB Joystick"
    assert text(ui, "sticksRemoteStateText") == "Нет команд"
    assert text(ui, "sticksRemoteReasonText") == "Команд не было с запуска блока"
    # блок без status.module (как в S7) — выхода нет; плашка по фактическому выходу — test_gui_crsf (S8)
    assert text(ui, "sticksOutputText") == "Радиовыхода нет: команды доходят до блока, модулю не передаются"
    assert not find(ui, "sticksEnableSwitch").property("enabled")
    assert text(ui, "sticksAxisValue_0") == "—" and text(ui, "sticksLatencyText") == "Задержка: —"
    assert sv.setEnabled(True, True) is False and sv.result == "no-device"
    assert ui.backend.peer.frames == []
    check_screen(ui, "01-no-device", out_dir, report)


def test_device_present_transmission_off(ui, out_dir, report):
    ui.backend.src.plug()
    feed(ui, rc=rc_state("IDLE", "start", 1.0, n=0, rate_hz=0.0, failsafes=0, age_ms=None, latency_ms=None), steps=10)
    sv = ui.backend.sticks
    assert sv.support == "ok" and sv.present and not sv.enabled and ui.backend.peer.frames == []
    assert text(ui, "sticksDeviceText") == BOXER_NAME and text(ui, "sticksDeviceDetail") == "Осей: 8 · кнопок: 24"
    sw = find(ui, "sticksEnableSwitch")
    assert sw.property("enabled") and not sw.property("checked") and sw.property("text") == "Передача"
    assert text(ui, "sticksAxisValue_1") == "−12" and text(ui, "sticksRemoteAxisValue_1") == "0"
    assert text(ui, "sticksRemoteStateText") == "Нет команд" and not visible(ui, "sticksNoticeText")
    check_screen(ui, "02-device-transmission-off", out_dir, report)


def test_transmission_on_active(ui, out_dir, report):
    b = ui.backend
    b.src.plug()
    b.src.st.update(axes=list(AXES), buttons=0b1000_0000_0101_0000_0000_0101)
    feed(ui)
    assert b.sticks.setEnabled(True, True)
    feed(ui, rc=rc_state("ACTIVE", "fresh", 2.0, ch=AXES, btn=0b1000_0000_0101_0000_0000_0101, failsafes=0),
         steps=5)
    sv = b.sticks
    assert sv.enabled and b.peer.frames and b.peer.frames[-1]["ch"] == AXES and b.peer.frames[-1]["en"] is True
    assert sv.remoteState == "ACTIVE" and text(ui, "sticksRemoteStateText") == "Команды идут"
    assert text(ui, "sticksRemoteReasonText") == "Свежие команды с пульта"
    assert find(ui, "sticksEnableSwitch").property("checked")
    assert text(ui, "sticksRateText") == "Частота: 49,8 Гц" and text(ui, "sticksLatencyText") == "Задержка: 18 мс"
    assert text(ui, "sticksGapsText") == "Пропуски: 0" and text(ui, "sticksFailsafesText") == "Срабатываний FAILSAFE: 0"
    assert text(ui, "sticksAxisValue_4") == "−1000" and text(ui, "sticksRemoteAxisValue_5") == "640"
    assert find(ui, "sticksButton_0").property("pressed") and find(ui, "sticksButton_23").property("remote")
    assert not find(ui, "sticksButton_1").property("pressed")
    check_screen(ui, "03-transmission-on-active", out_dir, report)


def test_failsafe_timeout(ui, out_dir, report):
    b = ui.backend
    b.src.plug()
    feed(ui)
    assert b.sticks.setEnabled(True, True)
    feed(ui, rc=rc_state("FAILSAFE", "timeout", 3.0, rate_hz=0.0, failsafes=2, gaps=3, age_ms=640), steps=1)
    assert text(ui, "sticksRemoteStateText") == "FAILSAFE: нет свежих команд"
    assert text(ui, "sticksRemoteReasonText") == "Нет команд дольше 500 мс: каналы в центре, кнопки отпущены"
    assert text(ui, "sticksFailsafesText") == "Срабатываний FAILSAFE: 2" and text(ui, "sticksGapsText") == "Пропуски: 3"
    assert all(text(ui, f"sticksRemoteAxisValue_{i}") == "0" for i in range(8))
    check_screen(ui, "04-failsafe-timeout", out_dir, report)


def test_device_lost_failsafe_and_notice(ui, out_dir, report):
    b = ui.backend
    b.src.plug()
    feed(ui)
    assert b.sticks.setEnabled(True, True)
    feed(ui, steps=3)
    b.src.unplug()
    feed(ui, rc=rc_state("FAILSAFE", "device-lost", 4.0, rate_hz=40.0, dev={"name": BOXER_NAME, "ok": False}),
         steps=5)
    sv = b.sticks
    frames = b.peer.frames
    assert frames[-1]["dev"]["ok"] is False and frames[-1]["en"] is True and len(frames) == 4
    assert not sv.enabled and sv.notice == "device-lost"
    assert not find(ui, "sticksEnableSwitch").property("checked")
    assert text(ui, "sticksNoticeText").startswith("Аппаратура была отключена: передача выключена")
    assert text(ui, "sticksRemoteReasonText") == "Аппаратура отключена: каналы в центре, кнопки отпущены"
    assert any(t.startswith("sticks: transmission disabled (device-lost)") for _, t in b.journal.records)
    check_screen(ui, "05-device-lost", out_dir, report)
    b.src.plug()                                         # вернули — передача остаётся выключенной (ST-08)
    feed(ui, steps=10)
    assert not sv.enabled and len(b.peer.frames) == 4


def test_no_link(ui, out_dir, report):
    b = ui.backend
    b.src.plug()
    feed(ui, rc=rc_state("ACTIVE", "fresh", 5.0, ch=AXES))
    feed(ui, status=LOST, steps=5)
    sv = b.sticks
    assert sv.support == "no-link" and sv.remoteState == "" and b.peer.frames == []
    assert text(ui, "sticksRemoteStateText") == "Нет связи с удалённым блоком"
    assert text(ui, "sticksRemoteReasonText") == "Передача недоступна, пока связь не восстановится."
    assert not find(ui, "sticksEnableSwitch").property("enabled")
    assert text(ui, "sticksRemoteAxisValue_2") == "—" and text(ui, "sticksRateText") == "Частота: —"
    assert sv.setEnabled(True, True) is False and sv.result == "no-link"
    check_screen(ui, "06-no-link", out_dir, report)


def test_old_remote(ui, out_dir, report):
    b = ui.backend
    b.src.plug()
    feed(ui, session="1.3", steps=5)
    sv = b.sticks
    assert sv.support == "old-remote" and b.peer.frames == []
    assert text(ui, "sticksRemoteStateText") == "Не поддерживается блоком"
    assert text(ui, "sticksRemoteReasonText") == "Удалённый блок не принимает команды стиков (протокол 1.3)."
    assert sv.setEnabled(True, True) is False and sv.result == "old-remote"
    assert text(ui, "sticksNoticeText") == "Передача не включена: не поддерживается удалённым блоком"
    check_screen(ui, "07-old-remote", out_dir, report)


# ---------------- переключатель и диалог ----------------
def test_switch_requires_confirmation_dialog(ui, out_dir, report):
    b = ui.backend
    b.src.plug()
    feed(ui, rc=rc_state("IDLE", "start", 1.0, n=0))
    QTest.mouseClick(ui.window, Qt.LeftButton, Qt.NoModifier, center(find(ui, "sticksEnableSwitch")))
    pump(80)
    dlg = find(ui, "sticksConfirmDialog")
    assert dlg.property("opened") or dlg.property("visible")
    assert not b.sticks.enabled and not find(ui, "sticksEnableSwitch").property("checked")
    q = text(ui, "sticksConfirmQuestion")
    assert f"«{BOXER_NAME}»" in q and "50 раз в секунду" in q and "500 мс" in q and "Радиовыхода нет" in q
    assert dlg.property("title") == "Включить передачу команд?"
    feed(ui, steps=5)
    assert b.peer.frames == []                           # пока диалог открыт — ничего
    check_screen(ui, "08-confirm-enable", out_dir, report)
    QTest.keyClick(ui.window, Qt.Key_Backtab)          # фокус по умолчанию — «Отмена»; к «Включить передачу»
    QTest.keyClick(ui.window, Qt.Key_Return)
    pump(60)
    assert b.sticks.enabled and find(ui, "sticksEnableSwitch").property("checked") and not dlg.property("visible")
    feed(ui, steps=2)
    assert len(b.peer.frames) == 2 and all(f["en"] for f in b.peer.frames)
    QTest.mouseClick(ui.window, Qt.LeftButton, Qt.NoModifier, center(find(ui, "sticksEnableSwitch")))
    pump(80)
    assert not b.sticks.enabled and not dlg.property("visible")    # выключение — сразу, без диалога
    feed(ui, steps=5)
    assert len(b.peer.frames) == 3 and b.peer.frames[-1]["en"] is False


def test_cancel_enables_nothing(ui):
    b = ui.backend
    b.src.plug()
    feed(ui)
    QTest.mouseClick(ui.window, Qt.LeftButton, Qt.NoModifier, center(find(ui, "sticksEnableSwitch")))
    pump(80)
    QTest.keyClick(ui.window, Qt.Key_Escape)
    pump(60)
    feed(ui, steps=5)
    assert not find(ui, "sticksConfirmDialog").property("visible")
    assert not b.sticks.enabled and b.peer.frames == []


def test_switch_inactive_without_device_does_not_open_dialog(ui):
    feed(ui)
    QTest.mouseClick(ui.window, Qt.LeftButton, Qt.NoModifier, center(find(ui, "sticksEnableSwitch")))
    pump(80)
    assert not find(ui, "sticksConfirmDialog").property("visible") and not ui.backend.sticks.enabled


# ---------------- ui-state, журнал, английский ----------------
def test_ui_state_without_page_shown(ui):
    b = ui.backend
    b.src.plug()
    feed(ui, rc=rc_state("ACTIVE", "fresh", 2.0, ch=AXES, btn=5))
    ui.window.setProperty("currentPage", 0)
    pump(60)
    s = sticks_ui_state(ui)
    assert s["support"] == "ok" and s["present"] and s["enabled"] is False and s["switchChecked"] is False
    assert s["remoteState"] == "ACTIVE" and s["stateText"] == "Команды идут" and s["remoteAxes"] == AXES
    assert s["noOutputBanner"] and s["noOutputText"].startswith("Радиовыхода нет")
    assert s["deviceText"] == BOXER_NAME and s["axisCount"] == 8 and s["buttonCount"] == 24
    assert s["rateText"] == "Частота: 49,8 Гц" and s["remoteOutput"] == "none"
    assert set(s["latency"]) >= {"samples", "median_ms", "p99_ms"}
    json.dumps(s, ensure_ascii=False)


def test_snapshot_display_matches_screen(ui):
    b = ui.backend
    b.src.plug()
    for rc, ru in [(rc_state("IDLE", "disabled", 1.0), "Нет команд"),
                   (rc_state("ACTIVE", "fresh", 2.0), "Команды идут"),
                   (rc_state("FAILSAFE", "link-lost", 3.0), "FAILSAFE: нет свежих команд")]:
        feed(ui, rc=rc)
        assert text(ui, "sticksRemoteStateText") == ru and b.sticks.snapshot()["display"]["stateText"] == ru
    feed(ui, rc=rc_state("IDLE", "disabled", 4.0))
    assert text(ui, "sticksRemoteReasonText") == "Передача выключена оператором"


def test_english_texts(ui):
    form = dict(ui.backend.config)
    form.update(language="en")
    assert finish(ui.backend, ui.backend.applyForm(form, True))
    pump(60)
    ui.backend.src.plug()
    feed(ui, rc=rc_state("FAILSAFE", "timeout", 1.0))
    assert text(ui, "sticksRemoteStateText") == "FAILSAFE: no fresh commands"
    assert text(ui, "sticksOutputText") == "No radio output: commands reach the remote unit but are not passed to the module"
    assert text(ui, "tabSticks") == "Control" and ui.backend.sticks.snapshot()["display"]["stateText"] == "FAILSAFE: no fresh commands"
    pump(100)
    assert page_problems(ui) == []


def test_page_names_index_for_capture():
    assert PAGE_NAMES.index("sticks") == 8 and sticks_mod.CONTEXT == "SticksPage"
