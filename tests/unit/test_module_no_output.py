# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S6 (ADR-0012, раздел 12.3) в смысле S8 (ADR-0014): output — режим выхода CRSF (none | neutral | live); у бэкенда
lab-emulator (по умолчанию) выхода нет никогда.

(б) lab-emulator: на каждом такте SCRIPT_FULL и всех сценариев 6.4 output == "none", блока crsf нет — и при командах
    пульта (ControlReceiver ACTIVE);
(в) вид "hardware" S6 не создаётся и схемой remote-1 не допускается; допустимы только lab-emulator, crsf-pty-lab,
    crsf-uart;
(г) лабораторная команда без лабораторного бэкенда — forbidden, ничего не передаётся;
(д) module-cmd не принимает никаких аргументов, кроме перечисленных в 6.3 (+ backend из BACKENDS, ADR-0014):
    каналов, значений и взведения в module-cmd нет — значения идут только кадрами rc (ADR-0013).
Запрета на интерфейсы устройств в коде модуля нет (D7): бэкенд crsf-uart работает с UART."""

import pytest

from sheregesh.config import Schema
from sheregesh.module import wire
from sheregesh.module.backend import HardwareBackendStub, ModuleError, make_backend
from sheregesh.module.lab import InlineEmulatorBackend
from sheregesh.module.manager import ModuleManager
from sheregesh.module.profiles import get_profile
from sheregesh.module.service import ModuleService
from sheregesh.module.settings import ModuleSettings
from sheregesh.module.states import State, Timings
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


def test_b_lab_emulator_output_none_with_live_commands():
    """Сторож команд ACTIVE не даёт выхода у lab-emulator: output и source() — none, центр, без кнопок."""
    clock = SimClock()
    be = InlineEmulatorBackend(JrModuleEmulator(clock), clock=clock, timings=Timings())
    svc = ModuleService(None, clock=clock, log=lambda s: None, backend_factory=lambda s: be)
    svc.start()
    for i in range(60):
        clock.advance(0.1)
        svc.set_control(("IDLE", "ACTIVE", "FAILSAFE")[i % 3], [500] * 8, 7)
        svc.tick()
        body = svc.status()
        assert body["output"] == "none" and "crsf" not in body
        assert svc.output_source() == ("none", (0,) * 8, 0)
    assert svc.manager.state == State.READY


def test_c_no_hardware_backend():
    with pytest.raises(ModuleError):
        HardwareBackendStub()
    assert HardwareBackendStub.supports_lab is False
    for kind in ("hardware", "uart", ""):
        with pytest.raises(ModuleError):
            make_backend(ModuleSettings(backend=kind), get_profile("radiomaster-nomad"), clock=SimClock(),
                         log=lambda s: None, timings=Timings())
    schema = Schema.load("remote-1")
    base = "enabled: true\nprofile: radiomaster-nomad\nauto_init: true\nauto_recover: true\n"
    assert any(i.kind == "enum" for i in schema.validate({"module.yaml": base + "backend: hardware\n"}))
    for kind in wire.BACKENDS:
        assert schema.validate({"module.yaml": base + f"backend: {kind}\n"}) == []
    assert ModuleSettings().backend == "lab-emulator"          # по умолчанию — поведение S6/S7


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
    probes = ["channel", "channels", "ch", "value", "arm", "armed", "output", "mode", "throttle", "power", "rf",
              "device", "port", "baud", "path", "uart", "failsafe_ch"]
    for op in wire.OPS:
        for key in probes:
            assert key not in allowed[op]
            with pytest.raises(wire.BadCommand) as e:
                wire.check_command({"req": 1, "op": op, "args": {key: 1}})
            assert e.value.result == "bad-request"
    assert set(wire.CONFIGURE_KEYS) == {"enabled", "auto_init", "auto_recover", "profile", "backend"}
    for kind in wire.BACKENDS:
        assert wire.check_command({"req": 1, "op": "configure", "args": {"backend": kind}})[1] == {"backend": kind}
    for bad in ("hardware", "uart", "live", "crsf", "", 1, True):
        with pytest.raises(wire.BadCommand) as e:
            wire.check_command({"req": 1, "op": "configure", "args": {"backend": bad}})
        assert e.value.result == "bad-request"
