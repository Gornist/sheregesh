# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""TEST-SYS-009: действия, меняющие состояние устройства, явно отличаются от экранов только для чтения.

Каталог изменяющих действий пульта (методы backend и объектов QML с параметром confirmed):
  applyForm, restore                    — конфигурация (новая ревизия, восстановление из копии);
  selectVideoSource                     — источник видео на блоке;
  sticks.setEnabled(true)               — включение передачи управления;
  module: requestReinit, requestReset, setEnabled, setAutoInit, configure, setBackend, runLabScenario.
Исключение по безопасности: выключение передачи (sticks.setEnabled(false, …)) — сразу и без подтверждения.
Не изменяют устройство: backup и exportLogs (файл копии или журнала), validateForm, reload, refresh.

1. Статический обход всех QML: каждый вызов действия каталога с подтверждением (confirmed = true) — только в
   onAccepted отдельного диалога подтверждения (классический интерфейс и Minimal); на экранах только для чтения
   вызовов нет вовсе.
2. Без подтверждения каждый метод каталога отказывает и ничего не меняет.
3. Отмена диалога подтверждения не меняет активную ревизию конфигурации.
"""
import re

import pytest
from PySide6.QtCore import QObject
from PySide6.QtQuickControls2 import QQuickStyle

from sheregesh_console.app import PAGE_NAMES, QML, Ui, compile_translations
from sheregesh_console.backend import Backend
from sheregesh_console.qtwait import pump, wait_until

QQuickStyle.setStyle("Fusion")
ACTIONS = ("applyForm", "restore", "selectVideoSource", "setEnabled", "requestReinit", "requestReset", "setAutoInit",
           "configure", "setBackend", "runLabScenario")
READ_ONLY = ("AboutPage.qml", "DiagnosticsPage.qml", "LogsPage.qml", "DashboardPage.qml", "PowerPage.qml",
             "MiniDiagPanel.qml", "MiniSystemPanel.qml")
CALL = re.compile(r"\b(" + "|".join(ACTIONS) + r")\(")


def split_args(line, start):
    """Аргументы вызова с учётом вложенных скобок: applyForm(page.form(), true) → ["page.form()", "true"]."""
    depth, cur, out = 1, "", []
    for ch in line[start:]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        if ch == "," and depth == 1:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    return out + [cur.strip()]


def calls():
    for path in sorted(QML.glob("*.qml")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for n, line in enumerate(lines):
            for m in CALL.finditer(line):
                yield path.name, n, lines, m.group(1), split_args(line, m.end())


def confirm_context(lines, n):
    """Вызов внутри onAccepted диалога: обработчик на этой строке или выше в том же блоке (до 12 строк)."""
    for line in reversed(lines[max(0, n - 12):n + 1]):
        if "onAccepted" in line:
            return True
        if re.match(r"\s*(onClicked|onActivated|onTriggered|onPressed|function)\b", line):
            return False
    return False


@pytest.mark.test_id("TEST-SYS-009")
def test_confirmed_calls_only_from_confirmation_handlers_and_none_on_read_only_screens():
    seen, bad = 0, []
    for name, n, lines, action, args in calls():
        seen += 1
        if name in READ_ONLY:
            bad.append(f"{name}:{n + 1} {action} на экране только для чтения")
            continue
        confirmed = args[-1] if args else ""
        if confirmed == "true" and not confirm_context(lines, n):
            bad.append(f"{name}:{n + 1} {action}(…, true) вне обработчика подтверждения")
        if confirmed == "false" and not (action == "setEnabled" and args[0] == "false"):
            bad.append(f"{name}:{n + 1} {action} без подтверждения (допустимо только выключение)")
    assert seen >= 14, "обход не нашёл вызовов — шаблон устарел"
    assert bad == []


@pytest.mark.test_id("TEST-SYS-009")
def test_traversal_detects_unconfirmed_call():
    """Обход ловит нарушение: вызов с подтверждением прямо в onClicked обычной кнопки."""
    lines = ["Button {", '    objectName: "applyNow"', "    onClicked: backend.applyForm(page.form(), true)", "}"]
    assert split_args(lines[2], CALL.search(lines[2]).end()) == ["page.form()", "true"]
    assert not confirm_context(lines, 2)
    ok = ["Dialog {", "    onAccepted: backend.applyForm(page.form(), true)", "}"]
    assert confirm_context(ok, 1)


@pytest.fixture
def ui(qapp, tmp_path):
    compile_translations()
    u = Ui(Backend(tmp_path / "data", demo=True, autostart=False))
    yield u
    u.close()


@pytest.mark.test_id("TEST-SYS-009")
def test_every_catalog_method_refuses_without_confirmation(ui):
    b = ui.backend
    rev = b.revision
    form = dict(b.config)
    form["language"] = "en"
    assert b.applyForm(form, False) is False
    assert b.restore("any.zip", False) is False
    assert b.selectVideoSource("synthetic", False) is False
    assert b.sticks.setEnabled(True, False) is False and not b.sticks.enabled
    mv = b.module
    assert mv.requestReinit(False) is False and mv.requestReset(False) is False
    assert mv.setEnabled(True, False) is False and mv.setAutoInit(False, False) is False
    assert mv.configure({}, False) is False and mv.setBackend("lab-emulator", False) is False
    assert mv.runLabScenario("detach", False) is False
    pump(200)
    assert b.revision == rev and b.language == "ru"


@pytest.mark.test_id("TEST-SYS-009")
def test_cancelled_confirmation_keeps_active_revision(ui):
    b = ui.backend
    rev = b.revision
    ui.window.setProperty("currentPage", PAGE_NAMES.index("settings"))
    pump(100)
    dlg = ui.window.findChild(QObject, "confirmDialog")
    assert dlg is not None
    dlg.open()
    assert wait_until(lambda: dlg.property("opened"), 3)
    dlg.reject()
    pump(300)
    assert b.revision == rev and not b.configBusy
