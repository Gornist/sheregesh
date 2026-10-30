# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Запросы пульта к блоку с итогом (этап 7.3): video-select (протокол 1.2) и module-cmd (1.3).

Для каждого вида хранится последний запрос: номер req (сквозной, растёт с каждым вызовом), сведения для показа и
состояние:
  queued       поставлен в очередь потока канала, ещё не отправлен
  sent         отправлен блоку, итога нет
  completed    блок сообщил итог с этим req (в status)
  timeout      итога нет за время ожидания вызывающего (запрос мог ещё исполниться)
  offline      соединения нет или оно потеряно до отправки — блок запрос не получал
  unknown      соединение потеряно после отправки — исполнил ли блок, неизвестно
  unsupported  согласованная версия (или блок) вид запроса не поддерживает
Объект не потокобезопасен: владелец (PeerClient) вызывает его под своим замком."""
QUEUED, SENT, COMPLETED, TIMEOUT, OFFLINE, UNKNOWN, UNSUPPORTED = (
    "queued", "sent", "completed", "timeout", "offline", "unknown", "unsupported")
LOST = (OFFLINE, UNKNOWN)


class Requests:
    def __init__(self):
        self._n = {}
        self._last = {}

    def new(self, kind: str) -> int:
        """Следующий номер req вида kind (номер выдаётся и при отказе до отправки: req в ответе вызывающему)."""
        self._n[kind] = self._n.get(kind, 0) + 1
        return self._n[kind]

    def open(self, kind: str, req: int, state: str, **meta) -> None:
        self._last[kind] = {"req": req, **meta, "state": state}

    def _move(self, kind, req, frm, to):
        cur = self._last.get(kind)
        if cur is not None and cur["req"] == req and cur["state"] in frm:
            cur["state"] = to

    def sent(self, kind: str, req: int) -> None:
        self._move(kind, req, (QUEUED,), SENT)

    def completed(self, kind: str, req: int) -> None:
        self._move(kind, req, (QUEUED, SENT, TIMEOUT), COMPLETED)

    def timed_out(self, kind: str, req: int) -> None:
        self._move(kind, req, (QUEUED, SENT), TIMEOUT)

    def lost(self) -> None:
        """Соединение потеряно: неотправленные — offline (блок их не получал), отправленные — unknown."""
        for cur in self._last.values():
            if cur["state"] == QUEUED:
                cur["state"] = OFFLINE
            elif cur["state"] == SENT:
                cur["state"] = UNKNOWN

    def state(self, kind: str, req: int) -> str | None:
        cur = self._last.get(kind)
        return cur["state"] if cur is not None and cur["req"] == req else None

    def snapshot(self, kind: str) -> dict | None:
        cur = self._last.get(kind)
        return dict(cur) if cur is not None else None
