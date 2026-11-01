# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""TEST-SYS-009: действия, меняющие состояние устройства, явно отличаются от экранов только для чтения.

Каталог изменяющих действий пульта (методы backend и объектов QML с параметром confirmed):
  applyForm, restore                    — конфигурация (новая ревизия, восстановление из копии);
  selectVideoSource                     — источник видео на блоке;
  sticks.setEnabled(true)               — включение передачи управления;
  module: requestReinit, requestReset, setEnabled, setAutoInit, configure, setBackend, runLabScenario;
  systemInfo.armDevImage                — разовый режим разработчика (REQ-UPD-005);
  shutdownRemote                        — штатное выключение удалённого блока (REQ-UI-006, протокол 1.6);
  armRemoteDevImage                     — разовый режим разработчика удалённого блока (REQ-UPD-005, протокол 1.6).
Исключение по безопасности: выключение передачи (sticks.setEnabled(false, …)) — сразу и без подтверждения.
Не изменяют устройство: backup, exportLogs и exportRemoteLogs (файл копии или журнала), validateForm, reload, refresh.

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
           "configure", "setBackend", "runLabScenario", "armDevImage", "shutdownRemote", "armRemoteDevImage")
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
    ran = []
    ui.system_info._run = lambda argv: ran.append(argv)
    assert ui.system_info.armDevImage(False) is False
    calls_ = []
    b._module_peer = lambda: type("P", (), {"remote_command": lambda self, op, w: calls_.append(op) or {"ok": True}})()
    assert b.shutdownRemote(False) is False and b.armRemoteDevImage(False) is False
    pump(200)
    assert b.revision == rev and b.language == "ru" and ran == [] and calls_ == []


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


@pytest.mark.test_id("TEST-UPD-005")
def test_dev_mode_dialog_arms_only_after_confirmation(ui):
    """Режим разработчика: кнопка открывает диалог (по умолчанию — «Отмена»); отмена ничего не запускает, «ОК» —
    одна команда dev-arm агента OTA, затем сведения перечитываются."""
    ran = []
    si = ui.system_info

    def run(argv):
        ran.append(argv[1])
        return {"result": "OK", "reason": "разрешена установка одного собственного образа"} if argv[1] == "dev-arm" \
            else {"dev_mode": {"armed": 1, "expires": 901, "by": "sheregesh-console.service"}}
    si._run = run
    si._info = {**si._info, "available": True}
    si.changed.emit()
    ui.window.setProperty("currentIndex", PAGE_NAMES.index("settings"))
    pump(300)
    items = {o.objectName(): o for o in ui.window.findChildren(QObject) if o.objectName()}
    dlg = items["devDialog"]
    dlg.open()
    pump(200)
    dlg.reject()
    pump(200)
    assert ran == []
    dlg.open()
    pump(200)
    dlg.accept()
    assert wait_until(lambda: "dev-arm" in ran and si.info.get("devMode") is True, 3), ran
    assert ran.count("dev-arm") == 1


class FakeRemote:
    """Клиент канала с remote_command (протокол 1.6): журнал блока и выключение."""

    def __init__(self):
        self.calls = []

    def remote_command(self, op, wait_s):
        self.calls.append(op)
        if op == "log-export":
            return {"ok": True, "result": "ok", "error": "", "req": 1, "name": "sheregesh-remote-log-20261101-120000.txt",
                    "data": b"remote line\n"}
        if op == "dev-arm":
            return {"ok": True, "result": "ok", "error": "разрешена установка одного собственного образа", "req": 3}
        return {"ok": True, "result": "ok", "error": "", "req": 2}


@pytest.mark.test_id("TEST-UI-006")
def test_remote_shutdown_dialog_sends_only_after_confirmation(ui):
    """Выключение блока: диалог в «Настройках» (по умолчанию — «Отмена»); отмена — без команды, «ОК» — одна команда
    shutdown, итог и запись в журнале пульта."""
    b, fake = ui.backend, FakeRemote()
    b._module_peer = lambda: fake
    ui.window.setProperty("currentIndex", PAGE_NAMES.index("settings"))
    pump(300)
    items = {o.objectName(): o for o in ui.window.findChildren(QObject) if o.objectName()}
    assert "remoteShutdownButton" in items
    dlg = items["remoteShutdownDialog"]
    dlg.open()
    pump(200)
    dlg.reject()
    pump(200)
    assert fake.calls == []
    dlg.open()
    pump(200)
    dlg.accept()
    assert wait_until(lambda: fake.calls == ["shutdown"] and not b.configBusy, 3), fake.calls
    assert "выключается" in b.lastRemoteResult
    assert any("shutdown requested by operator" in r["text"] for r in b.logRows(0))


@pytest.mark.test_id("TEST-SW-001")
def test_remote_log_export_writes_file_with_checksum(ui):
    b, fake = ui.backend, FakeRemote()
    b._module_peer = lambda: fake
    assert b.exportRemoteLogs() is True
    assert wait_until(lambda: not b.configBusy, 3)
    name = "sheregesh-remote-log-20261101-120000.txt"
    assert (b.usb_dir / name).read_bytes() == b"remote line\n"
    assert (b.usb_dir / f"{name}.sha256").read_text().endswith(f"  {name}\n")
    assert b.lastExport == name and name in b.lastRemoteResult


@pytest.mark.test_id("TEST-UPD-005")
def test_remote_dev_mode_dialog_arms_only_after_confirmation(ui):
    b, fake = ui.backend, FakeRemote()
    b._module_peer = lambda: fake
    ui.window.setProperty("currentIndex", PAGE_NAMES.index("settings"))
    pump(300)
    items = {o.objectName(): o for o in ui.window.findChildren(QObject) if o.objectName()}
    assert "remoteDevModeButton" in items
    dlg = items["remoteDevDialog"]
    dlg.open()
    pump(200)
    dlg.reject()
    pump(200)
    assert fake.calls == []
    dlg.open()
    pump(200)
    dlg.accept()
    assert wait_until(lambda: fake.calls == ["dev-arm"] and not b.configBusy, 3), fake.calls
    assert "собственного образа" in b.lastRemoteResult

