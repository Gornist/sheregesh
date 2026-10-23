# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Аппаратные абстракции ADR-0008. Реальные реализации появятся после выбора оборудования;
в S1 существуют только эмуляторы (software/emulators). Интерфейсы только читают состояние и
управляют собственным охлаждением и индикацией устройства — никаких внешних исполнительных устройств.
"""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PowerReading:
    source: str                 # "external" | "battery"
    percent: float              # заряд, %
    low_battery: bool           # сигнал модуля «низкий заряд» (REQ-PWR-005)
    undervoltage: bool          # признак пониженного напряжения


@dataclass(frozen=True)
class PeerStatus:
    link_up: bool               # кабель и соединение
    last_heartbeat: float | None
    protocol: str | None        # "MAJOR.MINOR" второй стороны
    software: str | None
    hardware: str | None
    device_id: str | None
    detail: str = "ok"          # refused | timeout | closed | heartbeat | bad-messages | handshake | <причина несовместимости>
    telemetry: dict | None = None   # синтетическое состояние второй стороны (температуры, питание, видео, события)
    session: str | None = None      # согласованная версия сессии MAJOR.MINOR


class PowerProvider(Protocol):
    def read(self) -> PowerReading | None: ...   # None — телеметрия недоступна (REQ-PWR-007)


class TemperatureSource(Protocol):
    def read(self) -> dict[str, float] | None: ...  # датчик → °C; None — недоступно


class FanController(Protocol):
    nominal_rpm: int

    def set_duty(self, percent: float) -> None: ...
    def read_rpm(self) -> int | None: ...          # None — нет тахосигнала


class Indicator(Protocol):
    def set_state(self, state: str) -> None: ...


class Peer(Protocol):
    def status(self) -> PeerStatus: ...
