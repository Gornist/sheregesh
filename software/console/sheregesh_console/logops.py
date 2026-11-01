# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Журнал приложения пульта и его выгрузка на «USB» (REQ-SW-001/005).

AppLog — единственный владелец записей журнала: номер ревизии, отметка «есть новые записи» для QML, перенос событий
монитора. Используется только из потока интерфейса (как Backend). Выгрузка: снимок записей с фильтром секретов
берётся в потоке интерфейса (export_payload), запись файлов — write_export в фоне (configops.ConfigOps)."""
import collections
import hashlib
import os
import time

from sheregesh.redact import REDACT     # фильтр секретов (REQ-SW-005), общий с журналом удалённого блока

LOG_LIMIT = 2000
LEVEL_FILTERS = {0: lambda r: True, 1: lambda r: r["level"] != "info", 2: lambda r: r["level"] == "error"}


class AppLog:
    def __init__(self, limit=LOG_LIMIT):
        self._rows = collections.deque(maxlen=limit)
        self._seq = 0
        self._dirty = False
        self._last_event_id = 0

    def add(self, level, source, text):
        self._seq += 1
        self._rows.append({"id": self._seq, "time": time.strftime("%H:%M:%S"), "level": level,
                           "source": source, "text": text})
        self._dirty = True

    def mirror_events(self, events):
        """События монитора, ещё не перенесённые в журнал (по возрастанию id)."""
        for e in events:
            if e.id > self._last_event_id:
                self._last_event_id = e.id
                level = {"error": "error", "warning": "warning"}.get(e.severity, "info")
                params = " ".join(f"{k}={v}" for k, v in e.params.items())
                self.add(level, "monitor", f"{e.code} {params}".strip())

    def take_dirty(self) -> bool:
        """Были ли новые записи с прошлого вызова (журнал — в QML только при изменениях)."""
        dirty, self._dirty = self._dirty, False
        return dirty

    @property
    def revision(self):
        return self._seq

    def __len__(self):
        return len(self._rows)

    def rows(self, level_filter=0):
        """Записи от новых к старым; 0 — все, 1 — предупреждения и ошибки, 2 — ошибки."""
        keep = LEVEL_FILTERS.get(level_filter, LEVEL_FILTERS[0])
        return [r for r in reversed(self._rows) if keep(r)]

    def export_payload(self):
        """(имя файла, содержимое, число записей) — снимок сейчас, секреты заменены на [REDACTED]."""
        name = time.strftime("sheregesh-log-%Y%m%d-%H%M%S.txt")
        lines = [f"{r['time']} {r['level'].upper():7} {r['source']:8} {REDACT.sub('[REDACTED]', r['text'])}"
                 for r in self._rows]
        return name, ("\n".join(lines) + "\n").encode("utf-8"), len(lines)


def write_export(usb_dir, name: str, data: bytes) -> None:
    """Журнал на «USB»: временный файл → rename, рядом контрольная сумма (REQ-SW-001/005)."""
    usb_dir.mkdir(parents=True, exist_ok=True)
    tmp = usb_dir / f".tmp-{name}"
    tmp.write_bytes(data)
    os.replace(tmp, usb_dir / name)
    (usb_dir / f"{name}.sha256").write_text(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
