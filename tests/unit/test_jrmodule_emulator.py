# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S6 (ADR-0012, разделы 6.4 и 7.2): ядро эмулятора модуля JR — ответы на keepalive/probe/init/ping, лабораторные
сценарии, детерминизм на SimClock."""
from sheregesh.module import wire
from sheregesh_emulators.devices import SimClock
from sheregesh_emulators.jrmodule import IDENTITIES, LAB_SCENARIOS, SCRIPT_FULL, SLOW_INIT_S, JrModuleEmulator


def emu(**kw):
    c = SimClock()
    return c, JrModuleEmulator(c, **kw)


def advance(c, e, seconds):
    out = []
    for _ in range(int(round(seconds / 0.1))):
        c.advance(0.1)
        out += e.drain()
    return out


def test_keepalive_probe_ping():
    c, e = emu()
    assert e.handle({"op": "keepalive"}) == [{"ev": "alive"}]
    assert e.handle({"op": "probe"}) == [{"ev": "presence", "present": True}]
    assert e.handle({"op": "ping", "id": 7}) == [{"ev": "pong", "id": 7}]
    assert e.handle({"op": "unknown"}) == [] and e.handle("junk") == []
    c, e = emu(present=False)
    assert e.handle({"op": "probe"}) == [{"ev": "presence", "present": False}]
    assert e.handle({"op": "ping", "id": 1}) == []


def test_init_after_init_s_with_identity():
    c, e = emu(init_s=1.0)
    assert e.handle({"op": "init", "id": 3, "profile": "radiomaster-nomad"}) == []
    assert advance(c, e, 0.9) == []
    assert advance(c, e, 0.1) == [{"ev": "init", "id": 3, "ok": True, "identity": IDENTITIES["radiomaster-nomad"]}]
    e.handle({"op": "init", "id": 4, "profile": "other-module"})
    out = advance(c, e, 1.0)
    assert out[0]["ok"] is False and out[0]["code"] == "MOD-INIT-FAILED"


def test_scenarios():
    c, e = emu()
    assert e.handle({"op": "lab", "scenario": "detach"}) == [{"ev": "lab", "scenario": "detach", "ok": True, "error": ""}]
    assert e.drain() == [{"ev": "presence", "present": False}]
    assert e.handle({"op": "probe"})[0]["present"] is False
    e.handle({"op": "init", "id": 1, "profile": "radiomaster-nomad"})
    assert advance(c, e, 2) == []                                 # вынут: на init не отвечает
    e.lab("attach")
    assert e.drain() == [{"ev": "presence", "present": True}]
    e.lab("iface-error")
    assert e.handle({"op": "ping", "id": 2}) == [{"ev": "fault", "code": "MOD-IFACE-ERROR",
                                                  "text": "interface error reported by module"}]
    e.handle({"op": "init", "id": 2, "profile": "radiomaster-nomad"})
    assert advance(c, e, 1)[0]["code"] == "MOD-IFACE-ERROR"
    assert e.handle({"op": "probe"})[0]["present"] is True       # обнаружение — физический признак
    e.lab("heal")
    e.lab("hang")
    assert e.handle({"op": "probe"}) == [{"ev": "presence", "present": True}]
    assert e.handle({"op": "ping", "id": 3}) == [] and e.handle({"op": "keepalive"}) == [{"ev": "alive"}]
    e.handle({"op": "init", "id": 5, "profile": "radiomaster-nomad"})
    assert advance(c, e, 3) == []
    e.lab("heal")
    e.lab("slow-init")
    e.handle({"op": "init", "id": 6, "profile": "radiomaster-nomad"})
    assert advance(c, e, SLOW_INIT_S - 0.1) == []
    assert advance(c, e, 0.1)[0]["ok"] is True
    e.handle({"op": "init", "id": 7, "profile": "radiomaster-nomad"})         # slow-init — только один раз
    assert advance(c, e, 1.0)[0]["id"] == 7
    assert e.lab("crash") and e.crashed
    for s in ("emu-unavailable", "emu-available"):                 # исполняет родитель
        r = e.handle({"op": "lab", "scenario": s})[0]
        assert r["ok"] is False and "parent" in r["error"]
    assert e.handle({"op": "lab", "scenario": "arm"})[0] == {"ev": "lab", "scenario": "", "ok": False,
                                                             "error": "unknown scenario"}
    e.restart()
    assert not e.crashed and e.present and not e.iface_error


def test_scenarios_match_wire_and_script():
    assert LAB_SCENARIOS == wire.LAB_SCENARIOS
    assert all(s in ("",) + wire.LAB_SCENARIOS for _, s, _ in SCRIPT_FULL)
    assert [t for t, _, _ in SCRIPT_FULL] == sorted(t for t, _, _ in SCRIPT_FULL)


def test_deterministic():
    def run():
        c, e = emu()
        out = []
        for i, s in enumerate(["detach", "attach", "iface-error", "heal", "hang", "heal", "slow-init"] * 3):
            out += e.handle({"op": "lab", "scenario": s})
            out += e.handle({"op": "init", "id": i, "profile": "radiomaster-nomad"})
            out += e.handle({"op": "ping", "id": i})
            out += advance(c, e, 0.7)
        return out
    assert run() == run()
