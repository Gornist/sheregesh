# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Детерминированный демонстрационный сценарий S1 (задача 5).

Контрольные точки (модельное время):
  start      — старт, штатное состояние
  heating    — растёт нагрузка и температура окружения, переход на батарею; вентилятор ускоряется
  warning    — порог предупреждения пройден: событие TEMP-HIGH, индикатор «warning»
  peer-lost  — пропала связь с удалённым блоком: событие PEER-LOST
  recovered  — нагрузка и связь восстановлены, внешнее питание; активных событий нет, индикатор «normal»
"""
from .scenario import Scenario, Step

DEMO = Scenario(
    name="demo-s1",
    duration=200.0,
    steps=[
        Step(10, "events", "inject", kwargs={"code": "EMU-TEST", "detail": "проверка журнала"}, label="тестовое событие генератора"),
        Step(15, "events", "clear", args=("EMU-TEST",), label="тестовое событие устранено"),
        Step(20, "thermal", "ramp_ambient", args=(40.0, 1.0), label="температура окружения растёт до 40 °C"),
        Step(20, "thermal", "set_load", args=(12.0,), label="нагрузка 12 Вт"),
        Step(20, "power", "switch_source", args=("battery",), label="переход на батарею"),
        Step(95, "peer", "disconnect", label="связь с удалённым блоком разорвана"),
        Step(115, "peer", "reconnect", label="связь восстановлена"),
        Step(120, "thermal", "ramp_ambient", args=(25.0, 1.0), label="температура окружения снижается"),
        Step(120, "thermal", "set_load", args=(4.0,), label="нагрузка 4 Вт"),
        Step(150, "power", "switch_source", args=("external",), label="внешнее питание"),
    ],
    checkpoints={"start": 2.0, "heating": 45.0, "warning": 90.0, "peer-lost": 105.0, "recovered": 200.0},
)
