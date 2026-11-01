# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S7 (ADR-0013, раздел 3): тело rc (check_rc), rc-state (sanitize_rc_state), типы SHRG-LAB 1.4 (CHG-012)."""
import json

import pytest

from sheregesh import PROTOCOL_VERSION
from sheregesh.control import wire
from sheregesh.control.receiver import ControlReceiver
from sheregesh.link.codec import TYPES
from sheregesh_emulators.devices import SimClock

RC = {"n": 1234, "en": True, "ch": [0, -12, 1000, 3, -1000, 0, 0, 1000], "btn": 5,
      "dev": {"name": "OpenTX Radiomaster Boxer Joystick", "ok": True}}
STATE = {"v": 1, "state": "ACTIVE", "reason": "fresh", "since_s": 12.3, "n": 1234, "age_ms": 18, "rate_hz": 49.8,
         "gaps": 0, "stale": 0, "bad": 0, "late_max_ms": 41, "failsafes": 1, "timeout_ms": 500,
         "ch": [0, -12, 1000, 3, -1000, 0, 0, 1000], "btn": 5, "dev": {"name": "OpenTX Radiomaster Boxer Joystick",
                                                                        "ok": True}, "output": "none"}


def test_protocol_14_types():
    assert PROTOCOL_VERSION == "1.6"                   # 1.6 — обслуживание блока; 1.5 — SRTP; rc и rc-state с 1.4
    assert TYPES["1.4"] == TYPES["1.3"] | {"rc", "rc-state"}
    assert TYPES["1.5"] == TYPES["1.4"] | {"video-key", "video-key-ack", "video-stop", "video-fallback", "video-renew"}
    for old in ("1.0", "1.1", "1.2", "1.3"):
        assert not {"rc", "rc-state"} & TYPES[old]


def test_check_rc_example_roundtrip():
    f = wire.check_rc(json.loads(json.dumps(RC)))
    assert f == wire.RcFrame(1234, True, (0, -12, 1000, 3, -1000, 0, 0, 1000), 5, RC["dev"]["name"], True)
    assert wire.rc_body(f) == RC
    assert wire.check_rc({**RC, "btn": wire.BTN_MAX, "n": wire.N_MAX, "dev": {"name": "", "ok": False}}).dev_ok is False


@pytest.mark.parametrize("body", [
    None, [], "rc",
    {k: v for k, v in RC.items() if k != "dev"},                 # нет ключа
    {**RC, "arm": True},                                        # лишний ключ
    {**RC, "n": 0}, {**RC, "n": -1}, {**RC, "n": 2 ** 31}, {**RC, "n": True}, {**RC, "n": 1.0}, {**RC, "n": "1"},
    {**RC, "en": 1}, {**RC, "en": None},
    {**RC, "ch": [0] * 7}, {**RC, "ch": [0] * 9}, {**RC, "ch": [0] * 7 + [1001]}, {**RC, "ch": [0] * 7 + [-1001]},
    {**RC, "ch": [0] * 7 + [0.5]}, {**RC, "ch": [0] * 7 + [True]}, {**RC, "ch": (0,) * 8}, {**RC, "ch": "00000000"},
    {**RC, "btn": -1}, {**RC, "btn": 2 ** 24}, {**RC, "btn": False}, {**RC, "btn": 1.0},
    {**RC, "dev": None}, {**RC, "dev": {"name": "x"}}, {**RC, "dev": {"name": "x", "ok": True, "path": "/dev/x"}},
    {**RC, "dev": {"name": 1, "ok": True}}, {**RC, "dev": {"name": "x", "ok": 1}},
    {**RC, "dev": {"name": "x" * (wire.NAME_MAX + 1), "ok": True}},
])
def test_check_rc_rejects(body):
    with pytest.raises(wire.BadRc) as e:
        wire.check_rc(body)
    assert isinstance(e.value, ValueError) and 0 < len(e.value.text) <= 80


def test_sanitize_rc_state_full_and_receiver_snapshot():
    assert wire.sanitize_rc_state(json.loads(json.dumps(STATE))) == STATE
    clock = SimClock()
    r = ControlReceiver(clock)
    snap = r.snapshot()
    assert wire.sanitize_rc_state(json.loads(json.dumps(snap))) == snap      # то, что шлёт блок, проходит без потерь
    r.on_rc(wire.check_rc(RC))
    clock.advance(0.02)
    snap = r.snapshot()
    assert set(snap) == set(STATE) and wire.sanitize_rc_state(json.loads(json.dumps(snap))) == snap


@pytest.mark.parametrize("v", [None, [], {}, {**STATE, "v": 2}, {**STATE, "v": True}, {**STATE, "state": "ARMED"},
                               {k: x for k, x in STATE.items() if k != "state"}])
def test_sanitize_rc_state_rejects(v):
    assert wire.sanitize_rc_state(v) is None


def test_sanitize_rc_state_defaults_and_limits():
    out = wire.sanitize_rc_state({"v": 1, "state": "FAILSAFE", "reason": "armed", "age_ms": None, "n": -5,
                                  "rate_hz": float("nan"), "gaps": True, "late_max_ms": 1e12, "ch": [5000, -5000, "x",
                                                                                                      None, 1, 2, 3, 4],
                                  "btn": 2 ** 24, "dev": {"name": "y" * 300, "ok": "yes"}, "output": "crsf",
                                  "extra": {"x": 1}})
    assert out["state"] == "FAILSAFE" and out["reason"] == "" and out["age_ms"] is None and out["n"] == 0
    assert out["rate_hz"] == 0 and out["gaps"] == 0 and out["late_max_ms"] == int(wire.NUM_MAX)
    assert out["ch"] == [1000, -1000, 0, 0, 1, 2, 3, 4] and out["btn"] == 0
    assert out["dev"] == {"name": "y" * wire.NAME_MAX, "ok": False} and out["output"] == "unknown"
    assert "extra" not in out and out["since_s"] == 0 and out["timeout_ms"] == 0
    assert wire.sanitize_rc_state({"v": 1, "state": "IDLE", "ch": [1] * 7})["ch"] == [0] * 8
