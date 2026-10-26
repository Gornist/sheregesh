# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Канал к внешнему модулю (ADR-0012, раздел 5): контракт ModuleBackend, события, выбор бэкенда по настройкам.

Бэкенды (module.yaml backend): lab-emulator — лабораторный эмулятор состояния (sheregesh.module.lab, S6, по
умолчанию); crsf-pty-lab и crsf-uart — выход CRSF на модуль (sheregesh.module.crsf, ADR-0014). Вид "hardware" —
только интерфейс S6 (HardwareBackendStub, отказ в конструкторе); настройками не выбирается. У любого нелабораторного
бэкенда supports_lab = False — лабораторные команды ему не передаются.
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
    """Канал не поднят. code — код ошибки модуля (states.ERROR_CODES) для FAULT; без него — EMU-START-FAILED."""

    def __init__(self, text="", code=None):
        super().__init__(text)
        self.code = code


class ModuleBackend(Protocol):
    kind: str                     # "lab-emulator" | "crsf-pty-lab" | "crsf-uart" | "hardware"
    supports_lab: bool            # True только у лабораторных бэкендов

    def start(self) -> None:
        """Запуск канала; результат — событиями channel-up / channel-down."""

    def stop(self) -> None:
        """Неблокирующая остановка (досылается в poll); событий после неё нет."""

    def poll(self) -> list:
        """Забрать накопленные события; keepalive и таймауты канала — здесь."""

    def send(self, op: str, **args) -> bool:
        """probe | init | ping | lab; False — канала нет (ничего не отправлено)."""

    # Необязательно (бэкенды CRSF): crsf_snapshot() -> dict — блок status.module.crsf.

    def lab_local(self, scenario: str) -> bool:
        """emu-unavailable / emu-available — на стороне родителя."""

    def info(self) -> dict:
        """{"kind","lab","state","pid","restarts","restart_limit","last_exit"}; restarts заполняет менеджер."""

    def close(self) -> None:
        """Блокирующая, не дольше stop_timeout_s; только при выходе агента."""


class HardwareBackendStub:
    """Вид "hardware" формата S6 — только интерфейс; аппаратный выход — бэкенд crsf-uart (ADR-0014)."""
    kind = "hardware"
    supports_lab = False

    def __init__(self, *a, **kw):
        raise ModuleError("hardware backend stub: use backend crsf-uart")


BACKENDS = ("lab-emulator", "crsf-pty-lab", "crsf-uart")


def make_backend(settings, profile, *, clock, log, timings, emulator_command=None, source=None,
                 crsf_emulator_command=None):
    """Бэкенд по настройкам: lab-emulator → LabEmulatorProcessBackend; crsf-pty-lab → PtyCrsfLabBackend;
    crsf-uart → UartCrsfBackend (source — режим и каналы выхода, ADR-0014); иначе ModuleError."""
    if settings.backend == "lab-emulator":
        from .lab import LabEmulatorProcessBackend
        return LabEmulatorProcessBackend(profile.id, clock=clock, log=log, timings=timings, command=emulator_command)
    if settings.backend in ("crsf-pty-lab", "crsf-uart"):
        from . import crsf
        src = source or (lambda: ("none", (0,) * 8, 0))
        if settings.backend == "crsf-pty-lab":
            return crsf.PtyCrsfLabBackend(profile, settings, clock=clock, log=log, timings=timings, source=src,
                                          command=crsf_emulator_command)
        return crsf.UartCrsfBackend(profile, settings, clock=clock, log=log, timings=timings, source=src)
    raise ModuleError(f"backend {settings.backend!r} is not available")


def check_backend(settings) -> str | None:
    """Проверка вида бэкенда без запуска процесса (health_check сохранения настроек): None — допустим."""
    return None if settings.backend in BACKENDS else f"backend {settings.backend!r} is not available"
