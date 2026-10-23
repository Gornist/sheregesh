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
        Step(10, "events", "inject", kwargs={"code": "EMU-TEST", "detail": "1"}, label={"ru": "тестовое событие генератора", "en": "generator test event"}),
        Step(15, "events", "clear", args=("EMU-TEST",), label={"ru": "тестовое событие устранено", "en": "test event cleared"}),
        Step(20, "thermal", "ramp_ambient", args=(40.0, 1.0), label={"ru": "температура окружения растёт до 40 °C", "en": "ambient temperature rises to 40 °C"}),
        Step(20, "thermal", "set_load", args=(12.0,), label={"ru": "нагрузка 12 Вт", "en": "load 12 W"}),
        Step(20, "power", "switch_source", args=("battery",), label={"ru": "переход на батарею", "en": "switch to battery"}),
        Step(95, "peer", "disconnect", label={"ru": "связь с удалённым блоком разорвана", "en": "link to remote unit broken"}),
        Step(115, "peer", "reconnect", label={"ru": "связь восстановлена", "en": "link restored"}),
        Step(120, "thermal", "ramp_ambient", args=(25.0, 1.0), label={"ru": "температура окружения снижается", "en": "ambient temperature falls"}),
        Step(120, "thermal", "set_load", args=(4.0,), label={"ru": "нагрузка 4 Вт", "en": "load 4 W"}),
        Step(150, "power", "switch_source", args=("external",), label={"ru": "внешнее питание", "en": "external power"}),
    ],
    checkpoints={"start": 2.0, "heating": 45.0, "warning": 90.0, "peer-lost": 105.0, "recovered": 200.0},
)
