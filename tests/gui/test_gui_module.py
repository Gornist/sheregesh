# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Страница «Модуль» пульта (S6, ADR-0012, раздел 10; offscreen, L3).

status.module приходит так же, как из телеметрии блока (PeerStatus.telemetry["module"] после wire.sanitize_module);
сеанс, канал и режим — поля Backend._status. Команды — подменный клиент канала с module_command (подмена
Backend._module_peer, как _video_peer для видео): агент B добавляет метод в PeerClient. Проверяется: вкладка (F8,
прежние индексы 0…6 не изменились), objectName раздела 10.3, русские тексты всех состояний и support, плашка
лабораторного эмулятора, кнопки только с подтверждением и неактивны без связи, событие MODULE-FAULT и журнал,
геометрия на 1280×800 (Waveshare 10.1) и 1920×1200 со шрифтом устройства. Снимки:
SHEREGESH_S6_GUI_OUT=docs/acceptance/s6-jr/screenshots-offscreen — туда; иначе во временный каталог.

Это не проверка физического дисплея и не проверка блока: только экран пульта на подменных данных.
"""
import copy
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QPoint, QPointF, Qt
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from sheregesh_console import module as module_mod
from sheregesh_console.app import PAGE_NAMES, Ui, compile_translations, module_ui_state
from sheregesh_console.backend import Backend
from sheregesh_console.journal import MemoryJournal
from sheregesh_console.qtwait import pump, wait_until
from test_e2e_gui_screens import header_problems, outside_window, rect, save_small
from test_gui_tile_geometry import wide_font  # noqa: F401 — фикстура шрифта с метриками устройства

QQuickStyle.setStyle("Fusion")
SIZES = [(1280, 800), (1920, 1200)]

# status.module по ADR-0012, 6.2 (после sanitize_module)
READY = {
    "v": 1, "state": "READY", "reason": "init-ok", "since_s": 12.4, "seq": 17,
    "profile": {"id": "radiomaster-nomad", "name": "RadioMaster Nomad", "family": "elrs", "interface": "uart-crsf"},
    "profiles": [{"id": "radiomaster-nomad", "name": "RadioMaster Nomad"}],
    "backend": {"kind": "lab-emulator", "lab": True, "state": "running", "pid": 1234, "restarts": 0,
                "restart_limit": 3, "last_exit": None},
    "identity": {"vendor": "RadioMaster", "model": "Nomad", "firmware": "emu-1.0", "serial": "EMU-NOMAD-0001"},
    "error": None,
    "recovery": {"auto": True, "attempt": 0, "next_s": None, "exhausted": False},
    "settings": {"enabled": True, "profile": "radiomaster-nomad", "backend": "lab-emulator", "auto_init": True,
                 "auto_recover": True, "revision": "r000002", "source": "store", "notice": ""},
    "diag": {"init_ms": 1003, "pong_age_s": 0.4, "faults": 0, "recoveries": 0, "transitions": 17},
    "output": "none",
    "history": [{"seq": 17, "from": "INITIALIZING", "state": "READY", "reason": "init-ok", "code": "", "ago_s": 12.4},
                {"seq": 16, "from": "DETECTED", "state": "INITIALIZING", "reason": "auto-init", "code": "",
                 "ago_s": 13.4},
                {"seq": 15, "from": "DISCONNECTED", "state": "DETECTED", "reason": "present", "code": "",
                 "ago_s": 13.5}],
}


def tel(state, reason="", seq=None, error=None, **kw):
    t = copy.deepcopy(READY)
    t.update(state=state, reason=reason or t["reason"], seq=seq if seq is not None else t["seq"], error=error, **kw)
    if seq is not None or state != "READY":
        t["history"] = [{"seq": t["seq"], "from": "READY", "state": state, "reason": t["reason"],
                         "code": (error or {}).get("code", "") if state == "FAULT" else "", "ago_s": t["since_s"]}] \
            + t["history"]
    return t


IFACE = {"code": "MOD-IFACE-ERROR", "text": "interface error reported by module", "since_s": 3.1, "state": "READY",
         "active": True}
STATES = {      # имя снимка → (status.module, текст плашки, доп. проверки)
    "disconnected": (tel("DISCONNECTED", "absent", 20, since_s=2.0), "Модуль отсутствует"),
    "detected": (tel("DETECTED", "present", 21, since_s=0.3), "Модуль обнаружен"),
    "initializing": (tel("INITIALIZING", "auto-init", 22, since_s=0.8,
                         backend={**READY["backend"], "state": "running"}), "Инициализация"),
    "ready": (READY, "Готов"),
    "fault-iface": (tel("FAULT", "iface-fault", 23, error=IFACE, since_s=3.1,
                        recovery={"auto": True, "attempt": 1, "next_s": 1.0, "exhausted": False},
                        diag={**READY["diag"], "faults": 1}), "Ошибка"),
    "recovering": (tel("RECOVERING", "recover-attempt", 24, error={**IFACE, "active": False, "since_s": 5.0},
                       since_s=0.4, recovery={"auto": True, "attempt": 1, "next_s": None, "exhausted": False}),
                   "Восстановление"),
    "fault-emu-unavailable": (tel("FAULT", "restart-limit", 30, since_s=1.0,
                                  error={"code": "EMU-UNAVAILABLE", "text": "restart limit 3 in 60 s", "since_s": 1.0,
                                         "state": "RECOVERING", "active": True},
                                  backend={**READY["backend"], "state": "unavailable", "pid": 0, "restarts": 3,
                                           "last_exit": 70},
                                  recovery={"auto": True, "attempt": 3, "next_s": None, "exhausted": True}), "Ошибка"),
    "disabled": (tel("DISABLED", "settings-disabled", 31, since_s=4.0,
                     backend={**READY["backend"], "state": "stopped", "pid": 0},
                     settings={**READY["settings"], "enabled": False, "revision": "r000003"}), "Отключён"),
}
CONNECTED = {"peerLink": "CONNECTED", "linkView": "CONNECTED", "peerState": "connected", "peerDetail": "ok",
             "peerDevice": "remote-emu-01", "peerSoftware": "0.1.0.dev0", "peerProtocol": "1.3", "peerMode": "network",
             "linkSecure": True, "linkTls": "TLSv1.3", "linkHost": "192.168.1.171", "remoteAvailable": True}
LOST = {"peerLink": "DISCONNECTED", "linkView": "DISCONNECTED", "peerState": "lost", "peerDetail": "heartbeat",
        "remoteAvailable": False, "peerMode": "network"}
OBJECT_NAMES = ["moduleLabBanner", "moduleLabBannerText", "moduleSupportText", "moduleStateBadge", "moduleStateText",
                "moduleProfileText", "moduleEmulatorText", "moduleErrorText", "moduleChangedText", "moduleOutputText",
                "moduleDiagList", "moduleHistoryList", "moduleReinitButton", "moduleResetButton", "moduleEnableButton",
                "moduleConfirmDialog", "moduleLabPanel"]
CHECKED_TEXT = ["moduleLabBannerText", "moduleOutputText", "moduleStateText", "moduleStateDetail", "moduleSupportText",
                "moduleProfileText", "moduleEmulatorText", "moduleErrorText", "moduleChangedText", "moduleStaleText",
                "moduleCommandText"]


class Peer:
    """Клиент канала с module_command (контракт ADR-0012, 9.2): ответ reply или ok; gate задерживает ответ."""

    def __init__(self, reply=None):
        self.reply, self.calls, self.gate = reply, [], threading.Event()
        self.gate.set()

    def module_command(self, op, args=None, wait_s=5.0):
        self.calls.append((op, dict(args or {}), wait_s))
        self.gate.wait(10)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply or {"ok": True, "result": "ok", "error": "", "req": len(self.calls)}


class OldClient:
    """Клиент канала без module_command (до слияния с агентом B)."""


@pytest.fixture
def out_dir(tmp_path):
    d = Path(os.environ.get("SHEREGESH_S6_GUI_OUT") or tmp_path / "s6-gui")
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def ui(qapp, tmp_path, wide_font):
    compile_translations()
    b = Backend(tmp_path / "data", autostart=False, journal=MemoryJournal())
    b.peer = Peer()
    b._module_peer = lambda: b.peer
    u = Ui(b)
    u.window.resize(1280, 800)
    u.window.setProperty("currentPage", PAGE_NAMES.index("module"))
    pump(100)
    yield u
    u.close()


def feed(u, module, session="1.3", status=None, received=True):
    """Как Backend._refresh: поля канала в status, затем ModuleView.update по телеметрии блока."""
    b = u.backend
    b._status.update(status or CONNECTED)
    telemetry = ({"state": "normal", "module": module} if module is not None else {"state": "normal"}) \
        if received else None
    b._update_module(SimpleNamespace(peer=SimpleNamespace(telemetry=telemetry, session=session)))
    b.statusChanged.emit()
    pump(60)


def walk(item):
    """Визуальные потомки (делегаты Repeater — не дети по QObject)."""
    for ch in item.childItems():
        yield ch
        yield from walk(ch)


def find(u, name):          # noqa: F811 — объект по имени, в том числе делегат Repeater
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
    """Видимые элементы страницы «Модуль» в её пределах; текст не шире элемента, вне списков — не обрезан;
    плашка, состояние, плитки и панель сценариев не перекрываются; вкладка «Модуль» видна в заголовке."""
    page = rect(find(u, "modulePage")).adjusted(-1, -1, 1, 1)
    bad = []

    def walk(item, flick):
        for ch in item.childItems():
            if not ch.isVisible() or ch.width() <= 0 or ch.height() <= 0:
                continue
            f = flick or ch.inherits("QQuickFlickable")
            name = ch.objectName() or ch.metaObject().className()
            if not flick and not page.contains(rect(ch)):
                bad.append(f"{name} вне страницы")
            if ch.inherits("QQuickText") and ch.property("text"):
                if not f and ch.property("truncated"):           # в списках (диагностика, история) — прокрутка
                    bad.append(f"{name} «{ch.property('text')[:40]}» обрезан")
                elif ch.property("contentWidth") > ch.width() + 1:
                    bad.append(f"{name} «{ch.property('text')[:40]}» шире элемента")
            walk(ch, f)
    walk(find(u, "modulePage"), False)
    blocks = [n for n in ("moduleLabBanner", "moduleStateBadge", "moduleTile", "moduleDiagTile", "moduleLabPanel")
              if visible(u, n)]
    for i, a in enumerate(blocks):
        for b in blocks[i + 1:]:
            if rect(find(u, a)).adjusted(1, 1, -1, -1).intersects(rect(find(u, b))):
                bad.append(f"{a}∩{b}")
    tab, bar = rect(find(u, "tabModule")), rect(find(u, "tabBar"))
    if tab.right() > bar.right() + 1:
        bad.append("вкладка «Модуль» за краем панели вкладок")
    return bad


def check_screen(u, name, out_dir, report):
    for w, h in SIZES:
        u.window.resize(w, h)
        pump(200)
        problems = outside_window(u) + header_problems(u) + page_problems(u)
        shot = save_small(u, out_dir / f"{w}x{h}-{name}.png")
        report.append({"size": f"{w}x{h}", "state": name, "png": shot.name, "problems": problems,
                       "ui": module_ui_state(u)})
        assert problems == [], f"{w}×{h} {name}: {problems}"
    u.window.resize(1280, 800)
    pump(60)


@pytest.fixture(scope="module")
def report():
    rows = []
    yield rows
    d = Path(os.environ.get("SHEREGESH_S6_GUI_OUT") or "")
    if rows and os.environ.get("SHEREGESH_S6_GUI_OUT"):
        (d / "screens.json").write_text(json.dumps(
            {"note": "offscreen, QT_QUICK_BACKEND=software, шрифт устройства (DejaVu Sans/Verdana) — L3, "
                     "не проверка физического дисплея; данные модуля подменные (ADR-0012, 6.2)",
             "checks": ["элементы в пределах окна и страницы", "текст вне списков не обрезан",
                        "блоки страницы не перекрываются", "индикаторы заголовка не перекрываются",
                        "вкладка «Модуль» видна на панели вкладок"],
             "shots": rows}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


# ---------------- вкладка ----------------
def test_module_tab_is_last_and_previous_indices_unchanged(ui):
    assert PAGE_NAMES[:7] == ["dashboard", "video", "diagnostics", "power", "logs", "settings", "about"]
    assert PAGE_NAMES[7] == "module" and text(ui, "tabModule") == "Модуль"
    ui.window.requestActivate()
    ui.window.setProperty("currentPage", 0)
    QTest.keyClick(ui.window, Qt.Key_F8)
    assert ui.window.property("currentPage") == 7 and visible(ui, "modulePage")
    QTest.keyClick(ui.window, Qt.Key_F7)
    assert PAGE_NAMES[ui.window.property("currentPage")] == "about"
    QTest.keyClick(ui.window, Qt.Key_8, Qt.ControlModifier)
    assert PAGE_NAMES[ui.window.property("currentPage")] == "module"


def test_all_object_names_present(ui):
    feed(ui, READY)
    for n in OBJECT_NAMES:
        find(ui, n)
    for sc in module_mod.LAB_SCENARIOS:
        find(ui, f"moduleLabButton_{sc}")


# ---------------- состояния ----------------
@pytest.mark.parametrize("name", list(STATES))
def test_state_texts_banner_and_geometry(ui, name, out_dir, report):
    module, badge = STATES[name]
    feed(ui, module)
    mv = ui.backend.module
    assert mv.support == "ok" and mv.state == module["state"]
    assert text(ui, "moduleStateText") == badge
    assert visible(ui, "moduleLabBanner") and text(ui, "moduleLabBannerText") == "Лабораторный эмулятор, не физический JR-модуль"
    assert text(ui, "moduleOutputText") == "Выход: нет"                  # строка выхода S8 (ADR-0014, 3.3)
    assert text(ui, "moduleProfileText") == "RadioMaster Nomad (radiomaster-nomad)"
    assert text(ui, "moduleChangedText").startswith("Изменено: ")
    assert not visible(ui, "moduleSupportText")
    assert mv.snapshot()["display"]["stateText"] == badge          # локальный канал — тот же текст, что экран
    emu = text(ui, "moduleEmulatorText")
    assert emu.startswith("Эмулятор: ") and f"из {module['backend']['restart_limit']}" in emu
    if name == "fault-iface":
        assert text(ui, "moduleStateDetail") == "Ошибка интерфейса" and visible(ui, "moduleStateDetail")
        assert "Ошибка интерфейса" in text(ui, "moduleErrorText") and "(активна)" in text(ui, "moduleErrorText")
    elif name == "fault-emu-unavailable":
        assert text(ui, "moduleStateDetail") == "Эмулятор недоступен"
        assert "недоступен" in emu and "PID —" in emu
    elif name == "recovering":
        assert "Ошибка интерфейса" in text(ui, "moduleErrorText") and "(активна)" not in text(ui, "moduleErrorText")
    else:
        assert not visible(ui, "moduleStateDetail")
    if name == "disabled":
        assert text(ui, "moduleEnableButton") == "Включить модуль"
        assert not find(ui, "moduleReinitButton").property("enabled")
    else:
        assert text(ui, "moduleEnableButton") == "Отключить модуль"
        assert find(ui, "moduleReinitButton").property("enabled")
    if module.get("error") is None:
        assert text(ui, "moduleErrorText") == "Последняя ошибка: нет"
    check_screen(ui, name, out_dir, report)


def test_history_tab_shows_transitions_with_console_time(ui, out_dir, report):
    feed(ui, STATES["fault-iface"][0])
    hist = ui.backend.module.history
    assert hist[0]["state"] == "FAULT" and hist[0]["code"] == "MOD-IFACE-ERROR" and hist[0]["at"].count(":") == 2
    tab = find(ui, "moduleHistoryTab")
    QTest.mouseClick(ui.window, Qt.LeftButton, Qt.NoModifier, center(tab))
    pump(60)
    assert visible(ui, "moduleHistoryList") and find(ui, "moduleHistoryList").property("count") == len(hist)
    check_screen(ui, "fault-iface-history", out_dir, report)


@pytest.mark.parametrize("case", ["no-link", "old-remote", "absent", "local", "no-link-before-first-status"])
def test_support_states(ui, case, out_dir, report):
    if case != "no-link-before-first-status":           # до первой телеметрии данных нет совсем (запуск пульта)
        feed(ui, READY)
    if case == "no-link":
        feed(ui, READY, status=LOST)
    elif case == "old-remote":
        feed(ui, None, session="1.2")
    elif case == "absent":
        feed(ui, None, session="1.3")
    elif case == "local":
        feed(ui, READY, status={**CONNECTED, "peerMode": "emulated"})
    else:
        feed(ui, None, received=False)
    mv = ui.backend.module
    expected = {"no-link": ("no-link", "Нет связи с удалённым блоком", "Команды недоступны"),
                "old-remote": ("old-remote", "Не поддерживается блоком",
                               "Удалённый блок не поддерживает внешний модуль (протокол 1.2)"),
                "absent": ("absent", "Нет данных модуля", "Удалённый блок не сообщает состояние модуля"),
                "local": ("local", "Нет удалённого блока", "Внешний модуль доступен только с удалённым блоком"),
                "no-link-before-first-status": ("no-link", "Нет связи с удалённым блоком", "Команды недоступны")}[case]
    assert mv.support == expected[0] and mv.state == ""
    assert text(ui, "moduleStateText") == expected[1] and expected[2] in text(ui, "moduleSupportText")
    assert visible(ui, "moduleSupportText") and visible(ui, "moduleLabBanner")      # плашка — и без данных
    for n in ("moduleReinitButton", "moduleResetButton", "moduleEnableButton"):
        assert not find(ui, n).property("enabled"), n
    assert not visible(ui, "moduleStateDetail")
    if case == "no-link":                              # последние данные — приглушённо, с подписью времени
        assert mv.stale and visible(ui, "moduleStaleText")
        assert text(ui, "moduleStaleText").startswith("Данные на ") and "Готов" in text(ui, "moduleStaleText")
        assert visible(ui, "moduleLabPanel")
        assert not any(find(ui, f"moduleLabButton_{s}").property("enabled") for s in module_mod.LAB_SCENARIOS)
        assert text(ui, "moduleProfileText") == "RadioMaster Nomad (radiomaster-nomad)"
        assert visible(ui, "moduleCommandRow")          # кнопки видны, но неактивны
    else:
        assert not mv.stale and not visible(ui, "moduleStaleText") and not visible(ui, "moduleLabPanel")
        assert text(ui, "moduleProfileText") == "Профиль: не сообщён"
        if case != "no-link-before-first-status":
            assert not visible(ui, "moduleCommandRow")  # старый блок / нет данных / нет блока — кнопок нет
    assert ui.backend.module.snapshot()["display"]["stateText"] == expected[1]
    check_screen(ui, case, out_dir, report)


def test_link_return_shows_actual_state_not_stale(ui):
    feed(ui, READY)
    feed(ui, READY, status=LOST)
    assert ui.backend.module.stale
    feed(ui, STATES["recovering"][0])
    assert not ui.backend.module.stale and text(ui, "moduleStateText") == "Восстановление"
    assert not visible(ui, "moduleStaleText")


def test_output_unknown_is_shown_as_warning(ui):
    feed(ui, {**READY, "output": "unknown"})
    assert text(ui, "moduleOutputText") == "Выход: неизвестное значение от блока"


@pytest.mark.parametrize("code,ru", [("MOD-IFACE-ERROR", "Ошибка интерфейса"), ("MOD-NO-RESPONSE", "Модуль не отвечает"),
                                     ("MOD-INIT-TIMEOUT", "Инициализация не завершилась вовремя"),
                                     ("MOD-INIT-FAILED", "Ошибка инициализации"),
                                     ("MOD-PROFILE-MISMATCH", "Модуль не соответствует профилю"),
                                     ("MOD-RECOVER-TIMEOUT", "Восстановление не завершилось"),
                                     ("EMU-START-FAILED", "Эмулятор не запустился"),
                                     ("EMU-EXITED", "Эмулятор аварийно завершился"),
                                     ("EMU-NO-KEEPALIVE", "Эмулятор не отвечает"),
                                     ("EMU-UNAVAILABLE", "Эмулятор недоступен")])
def test_error_code_texts(ui, code, ru):
    feed(ui, tel("FAULT", "x", 40, error={**IFACE, "code": code}))
    assert text(ui, "moduleStateDetail") == ru and ru in text(ui, "moduleErrorText")
    assert ui.backend.module.snapshot()["display"]["errorText"] == ru
    assert page_problems(ui) == []


# ---------------- команды ----------------
def open_and_accept(u, button):
    QTest.mouseClick(u.window, Qt.LeftButton, Qt.NoModifier, center(find(u, button)))
    pump(80)
    dlg = find(u, "moduleConfirmDialog")
    assert dlg.property("opened") or dlg.property("visible")
    return dlg


def test_commands_require_confirmation_dialog(ui, out_dir, report):
    feed(ui, READY)
    mv, peer = ui.backend.module, ui.backend.peer
    assert mv.requestReinit(False) is False and mv.runLabScenario("detach", False) is False
    assert mv.setEnabled(False, False) is False and peer.calls == []
    peer.gate.clear()                                   # ответ блока задерживается: видно «отправляется»
    dlg = open_and_accept(ui, "moduleReinitButton")
    assert peer.calls == []                             # без подтверждения — ничего
    check_screen(ui, "confirm-reinit", out_dir, report)
    QTest.keyClick(ui.window, Qt.Key_Return)
    assert wait_until(lambda: peer.calls == [("reinit", {}, module_mod.WAIT_S)], 3)
    assert mv.command["state"] == "sending" and not find(ui, "moduleResetButton").property("enabled")
    assert "отправляется" in text(ui, "moduleCommandText")
    assert mv.requestReset(True) is False              # одна команда за раз
    peer.gate.set()
    assert wait_until(lambda: mv.command["state"] == "done", 3)
    assert text(ui, "moduleCommandText") == "Команда «переинициализация»: выполнена"
    assert not dlg.property("visible")


def test_cancel_sends_nothing(ui):
    feed(ui, READY)
    dlg = open_and_accept(ui, "moduleResetButton")
    QTest.keyClick(ui.window, Qt.Key_Escape)
    pump(60)
    assert not dlg.property("visible") and ui.backend.peer.calls == []


def test_lab_scenario_and_disable_via_dialog(ui, out_dir, report):
    feed(ui, READY)
    peer = ui.backend.peer
    open_and_accept(ui, "moduleLabButton_detach")
    q = text(ui, "moduleConfirmQuestion")
    assert "модуль вынут" in q and "выхода управления нет" in q
    check_screen(ui, "confirm-lab-detach", out_dir, report)
    QTest.keyClick(ui.window, Qt.Key_Return)
    assert wait_until(lambda: ("lab", {"scenario": "detach"}, module_mod.WAIT_S) in peer.calls, 3)
    assert wait_until(lambda: ui.backend.module.command["state"] == "done", 3)
    open_and_accept(ui, "moduleEnableButton")
    QTest.keyClick(ui.window, Qt.Key_Return)
    assert wait_until(lambda: ("configure", {"enabled": False}, module_mod.CONFIGURE_WAIT_S) in peer.calls, 3)


@pytest.mark.parametrize("reply,code,shown", [
    ({"ok": False, "result": "timeout", "error": "", "req": 1}, "timeout", "удалённый блок не сообщил итог вовремя"),
    ({"ok": False, "result": "not-saved", "error": "storage read-only", "req": 1}, "not-saved",
     "настройку не удалось сохранить на удалённом блоке (storage read-only)"),
    (RuntimeError("socket closed"), "error", "RuntimeError: socket closed"),
])
def test_command_errors_are_shown(ui, reply, code, shown, out_dir, report):
    feed(ui, READY)
    ui.backend.peer.reply = reply
    assert ui.backend.module.setEnabled(False, True)
    assert wait_until(lambda: ui.backend.module.command["state"] == "error", 3)
    assert ui.backend.module.command["result"] == code
    assert shown in text(ui, "moduleCommandText") and text(ui, "moduleCommandText").startswith("Команда «настройки модуля» не выполнена")
    if code == "not-saved":
        check_screen(ui, "command-error", out_dir, report)


def test_slots_refuse_without_link_or_support(ui):
    mv = ui.backend.module
    feed(ui, READY, status=LOST)
    assert mv.requestReinit(True) is False and mv.command["result"] == "no-link"
    feed(ui, None, session="1.2")
    assert mv.runLabScenario("detach", True) is False and mv.command["result"] == "unsupported"
    feed(ui, READY)
    ui.backend.peer = OldClient()
    assert mv.requestReset(True) is False and mv.command["result"] == "unsupported"
    assert mv.runLabScenario("no-such", True) is False and mv.command["result"] == "bad-request"


def test_lab_panel_only_with_lab_backend(ui):
    feed(ui, {**READY, "backend": {**READY["backend"], "kind": "hardware", "lab": False}})
    mv = ui.backend.module
    assert mv.labScenarios == [] and not visible(ui, "moduleLabPanel")
    assert not visible(ui, "moduleLabBanner")                      # не лабораторный бэкенд — плашки нет
    assert text(ui, "moduleEmulatorText") == "Канал к модулю: hardware"
    assert mv.runLabScenario("detach", True) is False and mv.command["result"] == "forbidden"
    assert ui.backend.peer.calls == []


def test_link_loss_during_command_gives_no_link(ui):
    feed(ui, READY)
    peer = ui.backend.peer
    peer.gate.clear()
    assert ui.backend.module.requestReinit(True)
    feed(ui, READY, status=LOST)
    assert ui.backend.module.command["state"] == "error" and ui.backend.module.command["result"] == "no-link"
    peer.gate.set()
    pump(200)                                           # поздний ответ прежнего запроса не меняет итог
    assert ui.backend.module.command["result"] == "no-link"


# ---------------- событие, журналы, ui-state ----------------
def test_module_fault_event_and_journals(ui):
    b = ui.backend
    feed(ui, READY)
    assert not b.bench.monitor.log.is_active("MODULE-FAULT")
    feed(ui, STATES["fault-iface"][0])
    assert b.bench.monitor.log.is_active("MODULE-FAULT")
    ev = [e for e in b.bench.monitor.log.events if e.code == "MODULE-FAULT"][-1]
    assert ev.params == {"code": "MOD-IFACE-ERROR"}
    ui_state = module_ui_state(ui)
    assert ui_state["faultEvent"] and ui_state["stateText"] == "Ошибка"
    assert "Ошибка интерфейса" in ui_state["errorText"] and ui_state["labBanner"]
    feed(ui, STATES["recovering"][0])
    assert not b.bench.monitor.log.is_active("MODULE-FAULT")
    feed(ui, STATES["fault-iface"][0])
    feed(ui, READY, status=LOST)                        # без связи событие снимается (ADR-0012, 12.2)
    assert not b.bench.monitor.log.is_active("MODULE-FAULT")
    lines = [(p, t) for p, t in b.journal.records if t.startswith("module: ")]
    assert [t.split(" (")[0] for _, t in lines] == ["module: READY", "module: FAULT", "module: RECOVERING",
                                                     "module: FAULT", "module: no-link"], lines
    assert lines[1][0] == 3 and "code=MOD-IFACE-ERROR" in lines[1][1] and "profile=radiomaster-nomad" in lines[1][1]
    assert any(r["source"] == "module" and r["level"] == "error" for r in b._logs)


def test_ui_state_without_module_page_shown(ui):
    """ui-state (ADR-0012, 10.4) не зависит от открытой вкладки: тексты и плашка — по условиям страницы."""
    feed(ui, READY)
    ui.window.setProperty("currentPage", 0)
    pump(60)
    s = module_ui_state(ui)
    assert s["support"] == "ok" and s["state"] == "READY" and s["seq"] == 17 and s["stateText"] == "Готов"
    assert s["labBanner"] and s["profileText"] == "RadioMaster Nomad (radiomaster-nomad)"
    assert s["emulatorText"].startswith("Эмулятор: работает") and s["errorText"] == "Последняя ошибка: нет"
    assert s["command"]["state"] == "idle" and s["faultEvent"] is False
    json.dumps(s, ensure_ascii=False)


def test_snapshot_format_for_control_channel(ui):
    feed(ui, READY)
    snap = ui.backend.module.snapshot()
    assert set(snap) >= {"support", "stale", "session", "telemetry", "display"}
    assert snap["session"] == "1.3" and snap["telemetry"]["profile"]["id"] == "radiomaster-nomad"
    assert snap["display"] == {"stateText": "Готов", "errorText": "", "changedAt": ui.backend.module.changedAt,
                               "outputText": "Выход: нет"}
    assert snap["backend"] == "lab-emulator" and snap["output"] == "none"


def test_english_texts(ui):
    form = dict(ui.backend.config)
    form.update(language="en")
    assert ui.backend.applyForm(form, True)
    pump(60)
    feed(ui, STATES["fault-iface"][0])
    assert text(ui, "moduleStateText") == "Fault" and text(ui, "moduleStateDetail") == "Interface error"
    assert text(ui, "moduleLabBannerText") == "Laboratory emulator, not a physical JR module"
    assert ui.backend.module.snapshot()["display"]["stateText"] == "Fault"
    ui.window.resize(1280, 800)
    pump(100)
    assert page_problems(ui) == []
