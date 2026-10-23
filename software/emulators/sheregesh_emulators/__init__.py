# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Безопасные эмуляторы оборудования (ADR-0008).

Только синтетические данные на детерминированных часах. Нет протоколов управления аппаратами,
радиопередатчиками или внешними исполнительными устройствами (docs/security/out-of-scope-components.md).
"""
from .devices import (EventGenerator, FanEmulator, IndicatorEmulator, PeerEmulator, PowerEmulator, SimClock,
                      ThermalEmulator)
from .scenario import Bench, Scenario, Step

__all__ = ["Bench", "EventGenerator", "FanEmulator", "IndicatorEmulator", "PeerEmulator", "PowerEmulator",
           "Scenario", "SimClock", "Step", "ThermalEmulator"]
