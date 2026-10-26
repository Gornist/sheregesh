# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Безопасные эмуляторы оборудования (ADR-0008).

Синтетические данные на детерминированных часах.
Эмулятор внешнего модуля JR (jrmodule, ADR-0012) допускается решением D6 (управление наземными платформами), но
моделирует только состояние модуля — присутствие, инициализацию, ответы на опрос, ошибки — и радиосигналов,
каналов и любого выхода управления не формирует; импортируется отдельно (sheregesh_emulators.jrmodule).
Эмулятор передающего модуля ELRS (elrs_tx, ADR-0014) — ведомая сторона линии CRSF на псевдотерминале: принимает кадры
хоста, отвечает device info, синхронизацией и link statistics; радиосигналов не формирует; импортируется отдельно.
"""
from .devices import (EventGenerator, FanEmulator, IndicatorEmulator, PeerEmulator, PowerEmulator, SimClock,
                      ThermalEmulator)
from .scenario import Bench, Scenario, Step

__all__ = ["Bench", "EventGenerator", "FanEmulator", "IndicatorEmulator", "PeerEmulator", "PowerEmulator",
           "Scenario", "SimClock", "Step", "ThermalEmulator"]
