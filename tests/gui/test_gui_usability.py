# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Удобство пульта (обзор 2026-11-02): причина «нет связи» поверх видео, полоса активной ошибки, всплывающие
сообщения backend.message, итог выгрузки журнала (в т.ч. неудача), экранная клавиатура, «Отмена» по умолчанию."""
import pytest
from PySide6.QtCore import QMetaObject, Q_ARG, Qt
from PySide6.QtTest import QTest

from sheregesh_console import backend as backend_mod
from sheregesh_console.qtwait import pump, wait_until
from test_gui_minimal import feed, find, find_opt, out_dir, tap, ui, wfind  # noqa: F401 — фикстуры ui, out_dir
from test_gui_tile_geometry import wide_font  # noqa: F401 — фикстура шрифта устройства
from test_gui_video_select import RECONNECTING

DOWN = {**RECONNECTING, "peerLink": "DISCONNECTED", "linkView": "DISCONNECTED", "peerState": "connecting"}


def overlay(u):
    assert wait_until(lambda: find(u, "videoMessage").property("visible"), 3)
    return find(u, "videoMessageTitle").property("text"), find(u, "videoMessageText").property("text")


@pytest.mark.test_id("TEST-UI-003")
@pytest.mark.parametrize("status, title, hint", [
    ({**DOWN, "linkEverConnected": False, "remoteOff": False}, "ОЖИДАНИЕ БЛОКА", "кабель Ethernet"),
    ({**RECONNECTING, "linkEverConnected": True, "remoteOff": False}, "НЕТ СВЯЗИ С БЛОКОМ", "переподключение"),
    ({**DOWN, "peerLink": "AUTH_FAILED", "linkView": "ERROR", "linkEverConnected": True, "remoteOff": False},
     "БЛОК НЕ ПРИЗНАН", "сопрягите"),
    ({**RECONNECTING, "linkEverConnected": True, "remoteOff": True}, "БЛОК ВЫКЛЮЧЕН", "питание"),
], ids=["first-start", "lost", "auth-failed", "switched-off"])
def test_no_link_overlay_names_cause_and_action(ui, status, title, hint):
    """Первый запуск, потеря, отказ сертификатов и выключение блока с пульта — разные тексты: обещание «само
    восстановится» только там, где это правда."""
    ui.backend.video.stop()
    feed(ui, status=status, rc=None)
    t, text = overlay(ui)
    assert t == title and hint in text, (t, text)
    if status["peerLink"] == "AUTH_FAILED":
        assert "сама не восстановится" in text


def test_remote_shutdown_flag_set_only_on_accepted_command(ui):
    """«Блок выключен оператором» — только после принятой блоком команды (снимается при следующей связи)."""
    b = ui.backend
    b._remote_done("remote-shutdown", {"ok": False, "result": "busy"})
    assert not b._remote_off
    b._remote_done("remote-shutdown", {"ok": True, "result": "ok"})
    assert b._remote_off


@pytest.mark.test_id("TEST-UI-003")
def test_active_error_banner_over_video_opens_diagnostics(ui):
    b = ui.backend
    b._events = [{"id": 7, "t": 1.0, "code": "TEMP-CRIT", "severity": "error", "text": "Критическая температура SoC",
                  "action": "Остановите работу и проверьте охлаждение.", "active": True, "resolved": -1},
                 {"id": 8, "t": 2.0, "code": "SVC-DOWN", "severity": "error", "text": "Служба остановлена",
                  "action": "Перезапустите службу.", "active": True, "resolved": -1},
                 {"id": 9, "t": 3.0, "code": "PWR-SOURCE", "severity": "info", "text": "Источник питания",
                  "action": "", "active": True, "resolved": -1}]
    b.eventsChanged.emit()
    pump(100)
    banner = find(ui, "faultBanner")
    assert banner.property("visible")
    assert find(ui, "faultBannerText").property("text") == "Критическая температура SoC  (ещё 1)"
    assert find(ui, "faultBannerAction").property("text") == "Остановите работу и проверьте охлаждение."
    tap(ui, "faultBanner")
    assert ui.window.property("section") == "diagnostics"
    b._events = [dict(b._events[0], active=False)]
    b.eventsChanged.emit()
    pump(100)
    assert not banner.property("visible")


def test_link_loss_event_not_duplicated_in_banner(ui):
    b = ui.backend
    ui.backend.video.stop()
    feed(ui, status={**RECONNECTING, "linkEverConnected": True}, rc=None)
    b._events = [{"id": 1, "t": 1.0, "code": "LINK-AUTH-FAIL", "severity": "error", "text": "Отказ аутентификации",
                  "action": "Проверьте сертификаты.", "active": True, "resolved": -1}]
    b.eventsChanged.emit()
    pump(100)
    assert not find(ui, "faultBanner").property("visible"), "о потере связи уже говорит центр экрана"


def test_backend_message_shown_as_toast(ui):
    ui.backend.message.emit("error", "Удалённый блок: не выполнено — нет связи с удалённым блоком")
    pump(50)
    toast = find(ui, "toast")
    assert toast.property("visible")
    assert find(ui, "toastText").property("text").startswith("Удалённый блок: не выполнено")
    assert len(ui.window.property("notices").toVariant()) == 1
    x, y = toast.property("x"), toast.property("y")
    QTest.mouseClick(ui.window, Qt.LeftButton, Qt.NoModifier,
                     toast.mapToScene(toast.boundingRect().center()).toPoint())
    pump(50)
    assert not toast.property("visible"), (x, y)


@pytest.mark.test_id("TEST-SW-005")
def test_export_failure_shows_reason_not_previous_file(ui, monkeypatch):
    tap(ui, "nav_diagnostics")
    tap(ui, "miniExportLogs")
    result = wfind(ui, "miniExportResult")
    assert wait_until(lambda: result.isVisible(), 5)
    ok_text = result.property("text")
    assert ok_text.startswith("Сохранено: ") and ui.backend.lastOk

    def broken(*a, **kw):
        raise OSError("нет места на носителе")
    monkeypatch.setattr(backend_mod.logops, "write_export", broken)
    tap(ui, "miniExportLogs")
    assert wait_until(lambda: result.isVisible() and result.property("text") != ok_text, 5)
    text = result.property("text")
    assert not text.startswith("Сохранено") and "нет места на носителе" in text, text
    assert not ui.backend.lastOk


def test_remote_failure_reason_is_translated(ui):
    b = ui.backend
    b._remote_done("remote-logs", {"ok": False, "result": "not-connected", "error": "no link to remote unit"})
    assert b.lastResult == "Удалённый блок: не выполнено — нет связи с удалённым блоком", b.lastResult
    b._remote_done("remote-logs", {"ok": False, "result": "error", "error": "journalctl failed"})
    assert b.lastResult == "Удалённый блок: не выполнено — ошибка на блоке (journalctl failed)", b.lastResult


def open_settings(u):
    QMetaObject.invokeMethod(u.window, "showDetails", Q_ARG("QVariant", "SettingsPage"))
    assert wait_until(lambda: find_opt(u, "peerField") is not None, 5)
    pump(200)


def test_keyboard_only_after_touch_and_types_into_field(ui):
    open_settings(ui)
    kb = find(ui, "onScreenKeyboard")
    assert not kb.property("visible"), "открытие настроек (программный фокус) клавиатуру не показывает"
    field = find(ui, "peerField")
    field.setProperty("text", "")
    tap(ui, "peerField")
    assert wait_until(lambda: kb.property("visible"), 3)
    for key in ("osk_1", "osk_9", "osk_2", "osk_.", "osk_1"):
        tap(ui, key)
    assert field.property("text") == "192.1"
    tap(ui, "osk_backspace")
    assert field.property("text") == "192."
    assert field.property("activeFocus"), "клавиши не забирают фокус"
    tap(ui, "osk_done")
    assert wait_until(lambda: not kb.property("visible"), 3)
