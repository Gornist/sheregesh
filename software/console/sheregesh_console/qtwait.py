# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ожидание с работающим событийным циклом Qt, которое отпускает GIL (для тестов и инструментов).

QTest.qWait и qtbot.waitUntil держат GIL: фоновые потоки Python (чтение видео, канал связи) при этом
стоят, и испытание видит искажённую картину. QEventLoop.exec() отпускает GIL, как app.exec() в приложении.
"""
import time

from PySide6.QtCore import QEventLoop, QTimer


def pump(ms):
    loop = QEventLoop()
    QTimer.singleShot(int(ms), loop.quit)
    loop.exec()


def wait_until(pred, timeout_s, step_ms=50):
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if pred():
            return True
        pump(step_ms)
    return bool(pred())


def finish(backend, started, timeout_s=10.0):
    """Фоновая операция пульта (applyForm, backup, restore, exportLogs): started — что вернул вызов; дождаться её
    конца с работающим циклом событий → успешен ли итог (backend.lastOk)."""
    return bool(started) and wait_until(lambda: not backend.configBusy, timeout_s) and backend.lastOk
