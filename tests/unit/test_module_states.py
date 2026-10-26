# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S6 (ADR-0012, раздел 4): перечень состояний, допустимые переходы (таблица 4.2) и запрет остальных."""
import pytest

from sheregesh.module import wire
from sheregesh.module.manager import ModuleManager
from sheregesh.module.settings import ModuleSettings
from sheregesh.module.states import ALLOWED, INITIAL, State, Timings

S = State
# Таблица 4.2 построчно: (из, в)
TABLE_42 = [
    (None, S.DISABLED), (None, S.DISCONNECTED),
    (S.DISABLED, S.DISCONNECTED),
    *[(s, S.DISABLED) for s in S if s != S.DISABLED],
    (S.DISCONNECTED, S.DETECTED),
    (S.DETECTED, S.INITIALIZING),
    (S.DETECTED, S.DISCONNECTED), (S.INITIALIZING, S.DISCONNECTED), (S.READY, S.DISCONNECTED),
    (S.INITIALIZING, S.READY),
    (S.INITIALIZING, S.FAULT),
    (S.READY, S.FAULT),
    *[(s, S.FAULT) for s in (S.DISCONNECTED, S.DETECTED, S.INITIALIZING, S.READY, S.RECOVERING)],
    (S.DISCONNECTED, S.RECOVERING), (S.READY, S.RECOVERING),
    (S.FAULT, S.RECOVERING),
    (S.RECOVERING, S.DETECTED), (S.RECOVERING, S.DISCONNECTED),
    (S.RECOVERING, S.FAULT),
]


def test_states_match_wire_and_adr():
    assert [s.value for s in S] == list(wire.STATES) == ["DISCONNECTED", "DETECTED", "INITIALIZING", "READY", "FAULT",
                                                         "RECOVERING", "DISABLED"]


def test_allowed_matches_table_42():
    table = {}
    for frm, to in TABLE_42:
        table.setdefault(frm, set()).add(to)
    assert INITIAL == table.pop(None)
    assert {k: set(v) for k, v in ALLOWED.items()} == table
    assert set(ALLOWED) == set(S)
    for frm, tos in ALLOWED.items():
        assert frm not in tos                       # переходов «в себя» нет (FAULT → FAULT — обновление ошибки)


def test_timings_defaults_match_section_43():
    t = Timings()
    assert (t.hello_timeout_s, t.keepalive_interval_s, t.keepalive_timeout_s, t.recover_timeout_s) == (3, 1, 3, 10)
    assert tuple(t.recover_backoff_s) == (1, 2, 4, 8, 16, 30)
    assert (t.restart_limit, t.restart_window_s, t.stop_timeout_s) == (3, 60, 1)


def _manager(strict, logs):
    m = ModuleManager(ModuleSettings(enabled=False), lambda s: None, clock=lambda: 0.0, log=logs.append, strict=strict)
    m.start()
    return m


def test_illegal_transition_strict_raises():
    m = _manager(True, [])
    assert m.state == S.DISABLED
    with pytest.raises(AssertionError, match="illegal transition DISABLED -> READY"):
        m._transition(S.READY, "init-ok")


def test_illegal_transition_in_service_is_logged_and_not_done():
    logs = []
    m = _manager(False, logs)
    seq = m.seq
    assert m._transition(S.READY, "init-ok") is False
    assert m.state == S.DISABLED and m.seq == seq
    assert any("illegal transition DISABLED -> READY" in s for s in logs)
