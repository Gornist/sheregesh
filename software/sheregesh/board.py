# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Датчики платы для монитора и телеметрии (ADR-0008, REQ-PWR-007, REQ-R-007): температура SoC — из ядра
(/sys/class/thermal), питание — только от модуля питания, которого на Pi 4 нет: «нет данных», а не модельное
значение эмулятора. Без датчиков (рабочая станция, CI) — None: вызывающий оставляет эмуляторы.
"""
from pathlib import Path

THERMAL = Path("/sys/class/thermal/thermal_zone0")


class SocTemperature:
    """TemperatureSource: {"soc": °C} из thermal_zone (cpu-thermal на Pi 4); None — датчик не прочитан."""

    def __init__(self, zone: Path = THERMAL):
        self.path = Path(zone) / "temp"

    def read(self):
        try:
            milli = int(self.path.read_text().strip())
        except (OSError, ValueError):
            return None
        return {"soc": round(milli / 1000.0, 1)}


class NoPower:
    """PowerProvider без модуля питания: телеметрии нет (монитор поднимает PWR-NODATA, пульт — «нет данных»)."""

    def read(self):
        return None


def sensors(zone: Path = THERMAL):
    """(температуры, питание) платы или None — датчика температуры нет (не плата)."""
    t = SocTemperature(zone)
    return (t, NoPower()) if t.read() is not None else None
