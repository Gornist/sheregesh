# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Канал к внешнему модулю (ADR-0012, раздел 5): контракт ModuleBackend, события, заглушка аппаратного бэкенда.

S6: единственная реализация — лабораторный эмулятор (sheregesh.module.lab). Аппаратного бэкенда нет:
HardwareBackendStub — только интерфейс с отказом в конструкторе; make_backend для любого вида, кроме lab-emulator,
отказывает. У любого нелабораторного бэкенда supports_lab = False — лабораторные команды ему не передаются.
"""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class BackendEvent:
    kind: str                     # channel-up | channel-down | presence | init | pong | fault | lab
    data: dict = field(default_factory=dict)
    # channel-down: {"reason": "exited|no-keepalive|start-failed|protocol", "code": int|None}
    # presence: {"present": bool}; init: {"id": int, "ok": bool, "identity": dict, "code": str, "text": str}
    # pong: {"id": int}; fault: {"code": str, "text": str}; lab: {"scenario": str, "ok": bool, "error": str}


class ModuleError(Exception):
    pass


class ModuleBackend(Protocol):
    kind: str                     # "lab-emulator" | "hardware"
    supports_lab: bool            # True только у лабораторных бэкендов

    def start(self) -> None:
        """Запуск канала; результат — событиями channel-up / channel-down."""

    def stop(self) -> None:
        """Неблокирующая остановка (досылается в poll); событий после неё нет."""

    def poll(self) -> list:
        """Забрать накопленные события; keepalive и таймауты канала — здесь."""

    def send(self, op: str, **args) -> bool:
        """probe | init | ping | lab; False — канала нет (ничего не отправлено)."""

    def lab_local(self, scenario: str) -> bool:
        """emu-unavailable / emu-available — на стороне родителя."""

    def info(self) -> dict:
        """{"kind","lab","state","pid","restarts","restart_limit","last_exit"}; restarts заполняет менеджер."""

    def close(self) -> None:
        """Блокирующая, не дольше stop_timeout_s; только при выходе агента."""


class HardwareBackendStub:
    """Аппаратный бэкенд (UART/CRSF) — только интерфейс. В S6 не создаётся: открытия интерфейсов и выхода нет."""
    kind = "hardware"
    supports_lab = False

    def __init__(self, *a, **kw):
        raise ModuleError("hardware backend is not available in S6")


def make_backend(settings, profile, *, clock, log, timings, emulator_command=None):
    """Бэкенд по настройкам: "lab-emulator" → LabEmulatorProcessBackend; всё остальное → ModuleError."""
    if settings.backend != "lab-emulator":
        raise ModuleError(f"backend {settings.backend!r} is not available in S6")
    from .lab import LabEmulatorProcessBackend
    return LabEmulatorProcessBackend(profile.id, clock=clock, log=log, timings=timings, command=emulator_command)


def check_backend(settings) -> str | None:
    """Проверка вида бэкенда без запуска процесса (health_check сохранения настроек): None — допустим."""
    return None if settings.backend == "lab-emulator" else f"backend {settings.backend!r} is not available in S6"
