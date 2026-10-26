# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S6 (ADR-0012, раздел 12.3): лабораторный эмулятор не даёт выхода управления, аппаратного бэкенда в S6 нет.

(б) на каждом такте SCRIPT_FULL и всех сценариев 6.4 output == "none";
(в) аппаратный бэкенд пока не создаётся, схема remote-1 его не допускает;
(г) лабораторная команда без лабораторного бэкенда — forbidden, ничего не передаётся;
(д) module-cmd не принимает никаких аргументов, кроме перечисленных в 6.3.
Запрета на интерфейсы устройств в коде модуля нет (D7): будущий аппаратный бэкенд работает с UART."""

import pytest

from sheregesh.config import Schema
from sheregesh.module import wire
from sheregesh.module.backend import HardwareBackendStub, ModuleError, make_backend
from sheregesh.module.lab import InlineEmulatorBackend
from sheregesh.module.manager import ModuleManager
from sheregesh.module.profiles import get_profile
from sheregesh.module.settings import ModuleSettings
from sheregesh.module.states import Timings
from sheregesh_emulators.devices import SimClock
from sheregesh_emulators.jrmodule import SCRIPT_FULL, JrModuleEmulator

def _rig():
    clock = SimClock()
    be = InlineEmulatorBackend(JrModuleEmulator(clock), clock=clock, timings=Timings())
    m = ModuleManager(ModuleSettings(), lambda s: be, clock=clock, strict=True)
    m.start()
    return clock, m


def test_b_output_none_on_every_tick():
    clock, m = _rig()
    plan = [(t, s) for t, s, _ in SCRIPT_FULL if s]
    t = 80.0
    for s in wire.LAB_SCENARIOS:                     # все сценарии 6.4, включая emu-*, затем reset
        plan.append((t, s))
        t += 12.0
    plan.append((t, "reset"))
    while clock() < t + 30:
        while plan and plan[0][0] <= clock() + 1e-9:
            _, s = plan.pop(0)
            m.command("reset", {}) if s == "reset" else m.command("lab", {"scenario": s})
        clock.advance(0.1)
        m.tick()
        snap = m.snapshot()
        assert snap["output"] == "none"
        assert set(snap) == {"v", "state", "reason", "since_s", "seq", "profile", "profiles", "backend", "identity",
                             "error", "recovery", "diag", "output", "history"}


def test_c_no_hardware_backend():
    with pytest.raises(ModuleError):
        HardwareBackendStub()
    assert HardwareBackendStub.supports_lab is False
    for kind in ("hardware", "uart", ""):
        with pytest.raises(ModuleError):
            make_backend(ModuleSettings(backend=kind), get_profile("radiomaster-nomad"), clock=SimClock(),
                         log=lambda s: None, timings=Timings())
    text = "enabled: true\nprofile: radiomaster-nomad\nbackend: hardware\nauto_init: true\nauto_recover: true\n"
    assert any(i.kind == "enum" for i in Schema.load("remote-1").validate({"module.yaml": text}))


def test_lab_forbidden_without_lab_backend():
    """(г) Будущий аппаратный бэкенд обязан иметь supports_lab = False: лабораторные команды ему не передаются."""
    class NotLab(InlineEmulatorBackend):
        kind = "hardware"
        supports_lab = False
    clock = SimClock()
    be = NotLab(JrModuleEmulator(clock), clock=clock, timings=Timings())
    m = ModuleManager(ModuleSettings(), lambda s: be, clock=clock, strict=True)
    m.start()
    for _ in range(30):
        clock.advance(0.1)
        m.tick()
    before = list(be.sent)
    for s in wire.LAB_SCENARIOS:
        assert m.command("lab", {"scenario": s}) == "forbidden"
    assert be.sent == before and be.refuse is False and be.running


def test_e_check_command_only_listed_args():
    allowed = {"status": set(), "reinit": set(), "reset": set(), "configure": set(wire.CONFIGURE_KEYS),
               "lab": {"scenario"}}
    probes = ["channel", "channels", "value", "arm", "armed", "output", "throttle", "power", "rf", "backend", "device",
              "port", "baud", "path"]
    for op in wire.OPS:
        for key in probes:
            assert key not in allowed[op]
            with pytest.raises(wire.BadCommand) as e:
                wire.check_command({"req": 1, "op": op, "args": {key: 1}})
            assert e.value.result == "bad-request"
    assert set(wire.CONFIGURE_KEYS) == {"enabled", "auto_init", "auto_recover", "profile"}
