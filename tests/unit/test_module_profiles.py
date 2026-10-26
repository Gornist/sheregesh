# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S6 (ADR-0012, разделы 2 и 5): профиль RadioMaster Nomad и расширяемость реестра профилей без изменения менеджера."""
import pytest

from sheregesh.module.lab import InlineEmulatorBackend
from sheregesh.module.manager import ModuleManager
from sheregesh.module.profiles import REGISTRY, ModuleProfile, get_profile, register_profile
from sheregesh.module.settings import ModuleSettings
from sheregesh.module.states import State, Timings
from sheregesh_emulators.devices import SimClock
from sheregesh_emulators.jrmodule import IDENTITIES, JrModuleEmulator


def test_nomad_profile():
    p = get_profile("radiomaster-nomad")
    assert (p.vendor, p.model, p.name, p.family, p.interface) == ("RadioMaster", "Nomad", "RadioMaster Nomad", "elrs",
                                                                   "uart-crsf")
    assert (p.init_timeout_s, p.ping_interval_s, p.response_timeout_s) == (5.0, 1.0, 3.0)
    assert p.describe() == {"id": "radiomaster-nomad", "name": "RadioMaster Nomad", "family": "elrs",
                            "interface": "uart-crsf"}
    assert p.matches(IDENTITIES["radiomaster-nomad"])
    assert p.matches({"vendor": "RadioMaster", "model": "Nomad", "firmware": "other", "serial": "x"})
    assert not p.matches({"vendor": "RadioMaster", "model": "Other"})
    assert not p.matches({"vendor": "Other", "model": "Nomad"})
    assert not p.matches(None)


def test_unknown_profile_and_bad_id():
    with pytest.raises(KeyError):
        get_profile("no-such-module")
    with pytest.raises(ValueError):
        ModuleProfile(id="Bad ID", vendor="v", model="m", name="n", family="f", interface="i")


def test_register_new_profile_works_without_manager_changes():
    """Поддельный профиль «crossfire-test» (другая модель и таймауты): менеджер опознаёт модуль по профилю, а модуль
    «Nomad» под этим профилем — несоответствие (MOD-PROFILE-MISMATCH)."""
    fake = ModuleProfile(id="crossfire-test", vendor="TestVendor", model="CF-Test", name="Test CF module",
                         family="test", interface="uart-test", init_timeout_s=2.0, ping_interval_s=0.5,
                         response_timeout_s=1.5)
    register_profile(fake)
    try:
        assert get_profile("crossfire-test") is fake
        ident = {"vendor": "TestVendor", "model": "CF-Test", "firmware": "t", "serial": "T-1"}
        for identity, expected in ((ident, State.READY), (None, State.FAULT)):
            clock = SimClock()
            emu = JrModuleEmulator(clock, profile_id="crossfire-test", identity=identity or IDENTITIES["radiomaster-nomad"])
            be = InlineEmulatorBackend(emu, clock=clock, timings=Timings())
            m = ModuleManager(ModuleSettings(profile="crossfire-test", auto_recover=False), lambda s: be, clock=clock, strict=True)
            m.start()
            for _ in range(30):
                clock.advance(0.1)
                m.tick()
            assert m.state == expected
            snap = m.snapshot()
            assert snap["profile"]["id"] == "crossfire-test"
            assert {"id": "crossfire-test", "name": "Test CF module"} in snap["profiles"]
            if expected == State.FAULT:
                assert snap["error"]["code"] == "MOD-PROFILE-MISMATCH"
            else:                                   # опрос — с периодом профиля (0,5 с), не Nomad
                pings = [a for op, a in be.sent if op == "ping"]
                assert len(pings) >= 2
    finally:
        REGISTRY.pop("crossfire-test", None)
