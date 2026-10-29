# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Блокирующие операции пульта не останавливают поток интерфейса (configops.py): медленное хранилище и запись на
«USB» — событийный цикл Qt работает, интерфейс видит busy и итог, конфликтующая операция отклоняется, about не
запускает команды при чтении свойства, закрытие дожидается фоновой операции."""
import subprocess
import threading
import time

import pytest
from PySide6.QtCore import QTimer

from sheregesh_console import configops
from sheregesh_console.app import Ui, compile_translations
from sheregesh_console.backend import Backend
from sheregesh_console.qtwait import pump, wait_until


@pytest.fixture
def ui(qapp, tmp_path):
    compile_translations()
    backend = Backend(tmp_path / "data", demo=True, autostart=False)
    u = Ui(backend)
    qapp.processEvents()
    yield u
    u.close()


class Ticks:
    """Таймер 10 мс в потоке интерфейса: сколько раз он сработал, пока шла операция."""

    def __init__(self):
        self.n = 0
        self.t = QTimer()
        self.t.setInterval(10)
        self.t.timeout.connect(self._tick)
        self.t.start()

    def _tick(self):
        self.n += 1


def slow(gate, fn):
    def wrapper(*a, **kw):
        assert threading.current_thread() is not threading.main_thread()
        gate.wait(10)
        return fn(*a, **kw)
    return wrapper


def test_slow_apply_keeps_event_loop_and_reports_result(ui):
    b = ui.backend
    gate = threading.Event()
    b.store.apply = slow(gate, b.store.apply)
    form = dict(b.config)
    form["language"] = "en"
    before = b.revision
    ticks = Ticks()
    try:
        t0 = time.monotonic()
        assert b.applyForm(form, True) is True          # запрос принят, итог — позже
        assert time.monotonic() - t0 < 0.5
        assert b.configBusy is True and ui.find("applyButton").property("enabled") is False
        assert ui.find("backupButton").property("enabled") is False
        pump(200)
        assert ticks.n >= 5                              # цикл событий шёл, пока хранилище «писало»
        assert b.applyForm(form, True) is False and "ещё" in b.lastResult   # конфликтующее изменение отклонено
        assert b.revision == before
    finally:
        gate.set()
    assert wait_until(lambda: not b.configBusy, 5)
    assert b.revision != before and b.language == "en" and "Applied" in b.lastResult


def test_slow_backup_restore_and_export_logs(ui):
    b = ui.backend
    gate = threading.Event()
    b.store.backup = slow(gate, b.store.backup)
    ticks = Ticks()
    t0 = time.monotonic()
    assert b.backup() is True and time.monotonic() - t0 < 0.5 and b.configBusy
    pump(100)
    gate.set()
    assert wait_until(lambda: not b.configBusy, 5) and ticks.n >= 5
    name = b.backups()[0]
    assert name in b.lastResult

    gate.clear()
    b.store.restore = slow(gate, b.store.restore)
    assert b.restore(name, False) is False and not b.configBusy           # без подтверждения — ничего
    assert b.restore(name, True) is True and b.configBusy
    pump(50)
    gate.set()
    assert wait_until(lambda: not b.configBusy, 5)
    assert "Восстановлено" in b.lastResult or "Restored" in b.lastResult

    gate.clear()
    real = configops.write_log_export
    configops.write_log_export = slow(gate, real)
    try:
        assert b.exportLogs() is True and b.configBusy and b.lastExport == ""
        pump(50)
        gate.set()
        assert wait_until(lambda: not b.configBusy, 5)
    finally:
        configops.write_log_export = real
    assert b.lastExport.startswith("sheregesh-log-") and (b.usb_dir / b.lastExport).is_file()
    assert (b.usb_dir / f"{b.lastExport}.sha256").is_file()


def test_failed_operation_is_shown_and_releases_busy(ui):
    b = ui.backend

    def broken(*a, **kw):
        raise OSError("USB: нет места")
    b.store.backup = broken
    assert b.backup() is True
    assert wait_until(lambda: not b.configBusy, 5)
    assert "нет места" in b.lastResult


def test_about_does_not_run_commands_on_gui_thread(ui, monkeypatch):
    calls = []
    real = subprocess.run

    def spy(*a, **kw):
        calls.append(threading.current_thread() is threading.main_thread())
        return real(*a, **kw)
    monkeypatch.setattr(subprocess, "run", spy)
    b = ui.backend
    for _ in range(3):
        a = b.about
        assert a["software"] and "build" in a
    assert True not in calls
    assert wait_until(lambda: b.about["build"] not in ("", "…"), 10)


def test_background_completion_keeps_unsaved_settings_edits(ui):
    """Готовность сведений о сборке и ход фоновой операции не перечитывают форму настроек: несохранённая правка в
    SettingsPage остаётся (иначе завершение пробы посреди ввода сбрасывало значение)."""
    b = ui.backend
    ui.window.setProperty("currentPage", 3)
    spin = ui.find("timeoutSpin")
    spin.setProperty("value", 330)
    b._about_ready({"build": "x", "gstreamer": "y", "videoCodec": "z"})
    assert b.about["build"] == "x"
    gate = threading.Event()
    b.store.backup = slow(gate, b.store.backup)
    assert b.backup() is True
    pump(50)
    assert spin.property("value") == 330
    gate.set()
    assert wait_until(lambda: not b.configBusy, 5)


def test_stop_waits_for_running_operation(ui):
    b = ui.backend
    gate = threading.Event()
    done = threading.Event()
    real = b.store.backup

    def op(*a, **kw):
        gate.wait(10)
        try:
            return real(*a, **kw)
        finally:
            done.set()
    b.store.backup = op
    assert b.backup() is True
    threading.Timer(0.2, gate.set).start()
    b.stop()
    assert done.is_set() and not b._ops._thread.is_alive()   # stop() дождался записи
    assert b.backup() is False                                  # после stop() новые операции не начинаются
