# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S6 (ADR-0012, раздел 6): проверка status.module (sanitize_module) и тела module-cmd (check_command)."""
import json

import pytest

from sheregesh.module import wire
from sheregesh.module.lab import InlineEmulatorBackend
from sheregesh.module.service import ModuleService
from sheregesh.module.states import Timings
from sheregesh_emulators.devices import SimClock
from sheregesh_emulators.jrmodule import JrModuleEmulator

FULL = {
    "v": 1, "state": "READY", "reason": "init-ok", "since_s": 12.4, "seq": 17,
    "profile": {"id": "radiomaster-nomad", "name": "RadioMaster Nomad", "family": "elrs", "interface": "uart-crsf"},
    "profiles": [{"id": "radiomaster-nomad", "name": "RadioMaster Nomad"}],
    "backend": {"kind": "lab-emulator", "lab": True, "state": "running", "pid": 1234, "restarts": 0, "restart_limit": 3,
                "last_exit": None},
    "identity": {"vendor": "RadioMaster", "model": "Nomad", "firmware": "emu-1.0", "serial": "EMU-NOMAD-0001"},
    "error": {"code": "MOD-IFACE-ERROR", "text": "interface error reported by module", "since_s": 40.2, "state": "READY",
              "active": False},
    "recovery": {"auto": True, "attempt": 0, "next_s": None, "exhausted": False},
    "settings": {"enabled": True, "profile": "radiomaster-nomad", "backend": "lab-emulator", "auto_init": True,
                 "auto_recover": True, "revision": "r000002", "source": "store", "notice": ""},
    "diag": {"init_ms": 1003, "pong_age_s": 0.4, "faults": 2, "recoveries": 2, "transitions": 17},
    "output": "none",
    "history": [{"seq": 17, "from": "INITIALIZING", "state": "READY", "reason": "init-ok", "code": "", "ago_s": 12.4}],
    "cmd": {"req": 5, "op": "lab", "result": "ok", "error": ""},
}


def test_sanitize_example_from_adr_is_unchanged():
    assert wire.sanitize_module(json.loads(json.dumps(FULL))) == FULL


@pytest.mark.parametrize("bad", [None, [], "x", {}, {**FULL, "v": 2}, {**FULL, "v": True}, {**FULL, "v": "1"},
                                 {**FULL, "state": "ARMED"}, {**FULL, "state": None}])
def test_sanitize_rejects_whole_object(bad):
    assert wire.sanitize_module(bad) is None


def test_sanitize_output_other_than_none_is_unknown():
    for v in ("active", "uart", True, 1, None):
        assert wire.sanitize_module({**FULL, "output": v})["output"] == "unknown"
    assert wire.sanitize_module({k: v for k, v in FULL.items() if k != "output"})["output"] == "unknown"


def test_sanitize_types_lengths_and_defaults():
    raw = {"v": 1, "state": "FAULT", "reason": "x" * 100, "since_s": -5, "seq": 1e12, "extra": {"channels": [1, 2]},
           "profile": {"id": "BAD ID", "name": "n" * 100, "family": 5, "interface": "i" * 40},
           "profiles": [{"id": "p", "name": "P"}] * 20,
           "backend": {"kind": "uart", "lab": "yes", "state": "armed", "pid": "1", "restarts": True, "last_exit": -9},
           "identity": {"vendor": "v" * 50, "model": 3},
           "error": {"code": "SOMETHING", "text": "t"},
           "recovery": {"auto": 1, "attempt": -1, "next_s": "soon", "exhausted": True},
           "settings": {"enabled": "true", "source": "cloud", "notice": "n" * 100, "backend": "hardware"},
           "diag": {"init_ms": 2.7, "pong_age_s": float("nan")},
           "history": [{"seq": 1, "from": "X", "state": "READY", "reason": "r", "code": "BAD", "ago_s": 1}, {"state": "Z"},
                       "junk"] + [{"state": "READY"}] * 20,
           "cmd": {"req": True, "op": "lab", "result": "ok"}}
    m = wire.sanitize_module(raw)
    assert "extra" not in m and "cmd" not in m
    assert m["reason"] == "x" * 32 and m["since_s"] == 0 and m["seq"] == 10 ** 9
    assert m["profile"] == {"id": "", "name": "n" * 64, "family": "", "interface": "i" * 16}
    assert len(m["profiles"]) == 8
    assert m["backend"] == {"kind": "", "lab": False, "state": "", "pid": 0, "restarts": 0, "restart_limit": 0,
                            "last_exit": -9}
    assert m["identity"] == {"vendor": "v" * 32, "model": "", "firmware": "", "serial": ""}
    assert m["error"] is None
    assert m["recovery"] == {"auto": False, "attempt": 0, "next_s": None, "exhausted": True}
    assert m["settings"]["enabled"] is False and m["settings"]["source"] == "" and len(m["settings"]["notice"]) == 64
    assert m["settings"]["backend"] == "hardware"           # вид известен формату; схема блока его не допускает
    assert m["diag"]["init_ms"] == 2 and m["diag"]["pong_age_s"] is None
    assert m["history"][0] == {"seq": 1, "from": "", "state": "READY", "reason": "r", "code": "", "ago_s": 1.0}
    assert len(m["history"]) == 6                          # из первых 8 элементов отброшены 2 неверных
    assert wire.sanitize_module({**FULL, "cmd": {"req": 3, "op": "fly", "result": "unknown-op"}})["cmd"] == \
        {"req": 3, "op": "", "result": "unknown-op", "error": ""}
    assert "cmd" not in wire.sanitize_module({**FULL, "cmd": {"req": 3, "op": "lab", "result": "armed"}})


def test_check_command_accepts_section_63():
    ok = [({"req": 1, "op": "status"}, ("status", {}, 1)),
          ({"req": 2, "op": "reinit", "args": {}}, ("reinit", {}, 2)),
          ({"req": 3, "op": "reset"}, ("reset", {}, 3)),
          ({"req": 4, "op": "configure", "args": {"enabled": False}}, ("configure", {"enabled": False}, 4)),
          ({"req": 2 ** 31 - 1, "op": "configure", "args": {"auto_init": True, "auto_recover": False,
                                                           "profile": "radiomaster-nomad"}},
           ("configure", {"auto_init": True, "auto_recover": False, "profile": "radiomaster-nomad"}, 2 ** 31 - 1)),
          ({"req": 0, "op": "lab", "args": {"scenario": "detach"}}, ("lab", {"scenario": "detach"}, 0))]
    for body, expected in ok:
        assert wire.check_command(body) == expected
    for s in wire.LAB_SCENARIOS:
        assert wire.check_command({"req": 1, "op": "lab", "args": {"scenario": s}})[1] == {"scenario": s}


@pytest.mark.parametrize("body,result,req", [
    ("x", "bad-request", 0),
    ({"req": 1, "op": "status", "channel": 1}, "bad-request", 1),            # лишний ключ тела
    ({"op": "status"}, "bad-request", 0),                                     # нет req
    ({"req": -1, "op": "status"}, "bad-request", 0),
    ({"req": 2 ** 31, "op": "status"}, "bad-request", 0),
    ({"req": True, "op": "status"}, "bad-request", 0),
    ({"req": 1.5, "op": "status"}, "bad-request", 0),
    ({"req": 1, "op": 5}, "bad-request", 1),
    ({"req": 1, "op": "arm"}, "unknown-op", 1),
    ({"req": 1, "op": "output"}, "unknown-op", 1),
    ({"req": 1, "op": "status", "args": {"x": 1}}, "bad-request", 1),
    ({"req": 1, "op": "reinit", "args": []}, "bad-request", 1),
    ({"req": 1, "op": "configure", "args": {}}, "bad-request", 1),
    ({"req": 1, "op": "configure", "args": {"backend": "hardware"}}, "bad-request", 1),
    ({"req": 1, "op": "configure", "args": {"output": "on"}}, "bad-request", 1),
    ({"req": 1, "op": "configure", "args": {"enabled": 1}}, "bad-request", 1),
    ({"req": 1, "op": "configure", "args": {"profile": "Bad Profile"}}, "bad-request", 1),
    ({"req": 1, "op": "lab", "args": {}}, "bad-request", 1),
    ({"req": 1, "op": "lab", "args": {"scenario": "arm"}}, "bad-request", 1),
    ({"req": 1, "op": "lab", "args": {"scenario": "detach", "channel": 3}}, "bad-request", 1),
])
def test_check_command_rejects(body, result, req):
    with pytest.raises(wire.BadCommand) as e:
        wire.check_command(body)
    assert (e.value.result, e.value.req) == (result, req)
    assert e.value.result in wire.RESULTS


def test_service_status_is_valid_and_small():
    """Тело ModuleService.status() проходит sanitize_module без изменений и укладывается в 4 КиБ."""
    clock = SimClock()
    emu = JrModuleEmulator(clock)
    be = InlineEmulatorBackend(emu, clock=clock, timings=Timings())
    svc = ModuleService(None, clock=clock, log=lambda s: None, backend_factory=lambda s: be)
    svc.start()
    for i in range(400):
        clock.advance(0.1)
        svc.tick()
        if i % 40 == 5:
            svc.command({"req": i, "op": "lab", "args": {"scenario": ("detach", "attach", "iface-error", "heal")[i // 40 % 4]}})
    body = svc.status()
    assert len(json.dumps(body, ensure_ascii=False).encode()) <= wire.MAX_STATUS_BYTES
    assert wire.sanitize_module(json.loads(json.dumps(body))) == json.loads(json.dumps(body))
    assert len(body["history"]) == wire.HISTORY_MAX
