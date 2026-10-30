# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Сеанс управления блока: соединение пульта и его прикладной сеанс (ADR-0007, этап 2 устранения дефектов).

Один объект — одно соединение: поток записи, адрес пульта, согласованная своим hello версия, ожидание ответа на
heartbeat, итог video-select и последний отправленный rc-state. Агент держит ровно один текущий сеанс (или ни одного):
замена или обрыв соединения сбрасывает всё это одним присваиванием, и состояние прежнего соединения не может
остаться в новом. Прикладная команда исполняется, только если сеанс текущий и тип известен согласованной версии."""
from dataclasses import dataclass

from sheregesh.link.codec import TYPES


@dataclass(eq=False)
class ConsoleSession:
    writer: object
    addr: str | None = None              # адрес пульта (peername)
    version: str | None = None           # версия протокола после совместимого hello этого соединения; None — нет
    hb_wait: float | None = None         # monotonic первого heartbeat, на который пульт ещё не ответил
    video_select: dict | None = None     # итог последнего video-select этого сеанса (status.video.select)
    rc_state_sent: object = None         # control.transitions в последнем отправленном rc-state

    def established(self, mtype: str) -> bool:
        """Свой hello принят и согласованная версия знает тип сообщения."""
        return self.version is not None and mtype in TYPES.get(self.version, ())
