# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Записи пульта в системный журнал (E2E-12, REQ-SW-001/005): переходы состояния канала и видео.

Служба sheregesh-console пишет stderr в journald; префикс «<N>» (уровень syslog, sd-daemon(3)) journald
разбирает сам (SyslogLevelPrefix=yes по умолчанию). Запись — в фоновом потоке: зависший приёмник журнала
не должен останавливать событийный цикл GUI (E2E-08). Секретов в записях нет: только состояния, адреса,
идентификаторы устройств и причины отказа.
"""
import queue
import sys
import threading

EMERG, ALERT, CRIT, ERR, WARNING, NOTICE, INFO, DEBUG = range(8)


class Journal:
    def __init__(self, stream=None):
        self._stream = stream
        self._q = queue.SimpleQueue()
        self._thread = None

    def emit(self, priority, text):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="journal", daemon=True)
            self._thread.start()
        self._q.put(f"<{int(priority)}>{text}\n")

    def _run(self):
        while True:
            line = self._q.get()
            stream = self._stream or sys.stderr
            try:
                stream.write(line)
                stream.flush()
            except (OSError, ValueError):
                pass


class MemoryJournal:
    """Для испытаний: те же вызовы, записи в списке (priority, text)."""

    def __init__(self):
        self.records = []

    def emit(self, priority, text):
        self.records.append((priority, text))


def video_class(state):
    """Видео для журнала: playing | no-stream | off; waiting — переходное, класс не меняет (None)."""
    if state == "playing":
        return "playing"
    if state == "off":
        return "off"
    if state == "waiting":
        return None
    return "no-stream"          # no-video, restarting, no-device, unsupported, unavailable
