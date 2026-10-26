# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""ModuleView пульта (S6, ADR-0012, 10.2) без QML: support, stale, время по часам пульта, тексты, слоты."""
import copy

import pytest

from sheregesh_console import module as m

TEL = {"v": 1, "state": "READY", "reason": "init-ok", "since_s": 12.0, "seq": 17,
       "profile": {"id": "radiomaster-nomad", "name": "RadioMaster Nomad", "family": "elrs", "interface": "uart-crsf"},
       "backend": {"kind": "lab-emulator", "lab": True, "state": "running", "pid": 1234, "restarts": 0,
                   "restart_limit": 3, "last_exit": None},
       "error": {"code": "MOD-IFACE-ERROR", "text": "iface\x07 error", "since_s": 40.0, "state": "READY", "active": False},
       "settings": {"enabled": True, "revision": "r000002", "source": "store"}, "output": "none",
       "history": [{"seq": 17, "from": "INITIALIZING", "state": "READY", "reason": "init-ok", "code": "", "ago_s": 12.0}]}
OK = {"session": "1.3", "link": "CONNECTED", "mode": "network"}


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def view(qapp):
    clock = Clock()
    v = m.ModuleView(wall=clock)
    v.clock = clock
    return v


@pytest.mark.parametrize("module,session,link,mode,received,expected", [
    (TEL, "1.3", "CONNECTED", "network", True, "ok"),
    (TEL, "1.4", "CONNECTED", "process", True, "ok"),
    (TEL, "2.0", "CONNECTED", "network", True, "ok"),
    (None, "1.3", "CONNECTED", "network", True, "absent"),
    ({"state": "ARMED"}, "1.3", "CONNECTED", "network", True, "absent"),       # неизвестное состояние — не показывать
    (None, "1.2", "CONNECTED", "network", True, "old-remote"),
    (TEL, "1.2", "CONNECTED", "network", True, "old-remote"),
    (TEL, "1.3", "RECONNECTING", "network", True, "no-link"),
    (TEL, "1.3", "AUTH_FAILED", "network", True, "no-link"),
    (None, "1.3", "CONNECTED", "network", False, "no-link"),                   # до первого status
    (None, None, "CONNECTED", "network", True, "no-link"),
    (TEL, "1.3", "CONNECTED", "emulated", True, "local"),
])
def test_support(module, session, link, mode, received, expected):
    assert m.support_of(module, session, link, mode, received) == expected


def test_changed_at_from_seq_by_console_clock(view):
    view.update(telemetry=TEL, **OK)
    t0 = view.clock.t
    assert view.changedAt == m.clock_text(t0 - 12.0) and view.changedAgoS == 12
    view.clock.t += 5.3                                  # следующий status: тот же переход, since_s вырос
    view.update(telemetry={**TEL, "since_s": 17.4}, **OK)
    assert view.changedAt == m.clock_text(t0 - 12.0) and view.changedAgoS == 17
    view.update(telemetry={**TEL, "seq": 18, "state": "FAULT", "since_s": 0.2}, **OK)
    assert view.changedAt == m.clock_text(view.clock.t - 0.2) and view.state == "FAULT"


def test_error_time_stable_and_new_error_recomputed(view):
    view.update(telemetry=TEL, **OK)
    at = view.lastErrorAt
    view.clock.t += 3
    view.update(telemetry={**TEL, "error": {**TEL["error"], "since_s": 43.4}}, **OK)
    assert view.lastErrorAt == at                        # задержка доставки не «сдвигает» время ошибки
    view.update(telemetry={**TEL, "error": {**TEL["error"], "since_s": 1.0, "active": True}}, **OK)
    assert view.lastErrorAt == m.clock_text(view.clock.t - 1.0) and view.errorActive
    assert view.lastErrorText == "iface  error"           # управляющие символы блока не попадают на экран


def test_stale_on_link_loss_and_cleared_for_other_support(view):
    view.update(telemetry=TEL, **OK)
    view.clock.t += 2
    view.update(telemetry=TEL, session="1.3", link="RECONNECTING", mode="network")
    assert view.support == "no-link" and view.stale and view.state == "" and view.lastState == "READY"
    assert view.profileName == "RadioMaster Nomad" and view.staleAt == m.clock_text(view.clock.t - 2)
    view.update(telemetry=None, session="1.2", link="CONNECTED", mode="network")
    assert view.support == "old-remote" and not view.stale and view.profileName == "" and view.protocolNote == "1.2"
    view.update(telemetry=None, session="1.2", link="RECONNECTING", mode="network")
    assert view.support == "no-link" and not view.stale       # прежних данных модуля нет


def test_lab_scenarios_only_with_lab_backend(view):
    view.update(telemetry=TEL, **OK)
    assert view.isLab and view.labScenarios == list(m.LAB_SCENARIOS)
    view.update(telemetry={**TEL, "backend": {**TEL["backend"], "lab": False, "kind": "hardware"}}, **OK)
    assert not view.isLab and view.labScenarios == []
    assert view.runLabScenario("detach", True) is False and view.command["result"] == "forbidden"


def test_slots_need_confirmation_and_support(view):
    calls = []
    view._command_fn = lambda: (lambda op, args, wait: calls.append(op) or {"ok": True, "result": "ok"})
    for slot in (lambda c: view.requestReinit(c), lambda c: view.requestReset(c), lambda c: view.setEnabled(False, c),
                 lambda c: view.setAutoInit(True, c), lambda c: view.runLabScenario("detach", c)):
        assert slot(False) is False                      # без подтверждения — ничего
    assert calls == [] and view.command["state"] == "idle"
    assert view.requestReinit(True) is False and view.command == {**view.command, "state": "error", "result": "no-link"}
    view.update(telemetry=None, session="1.2", link="CONNECTED", mode="network")
    assert view.requestReset(True) is False and view.command["result"] == "unsupported"
    assert calls == []


def test_configure_validates_types(view):
    view._command_fn = lambda: (lambda op, args, wait: {"ok": True, "result": "ok"})
    view.update(telemetry=TEL, **OK)
    assert view.configure({"backend": "hardware"}, True) is False and view.command["result"] == "bad-request"
    assert view.configure({"enabled": 1}, True) is False
    assert view.configure({}, True) is False


def test_changed_emitted_only_on_visible_change(view):
    n = []
    view.changed.connect(lambda: n.append(1))
    view.update(telemetry=TEL, **OK)
    k = len(n)
    view.update(telemetry=copy.deepcopy(TEL), **OK)
    assert len(n) == k


def test_snapshot_without_translator_is_english(view):
    view.update(telemetry={**TEL, "state": "FAULT", "seq": 18}, **OK)
    snap = view.snapshot()
    assert snap["support"] == "ok" and snap["session"] == "1.3" and snap["telemetry"]["seq"] == 18
    assert snap["display"]["stateText"] == "Fault" and snap["display"]["errorText"] == "Interface error"
    assert snap["command"]["state"] == "idle"


def test_text_tables_cover_wire_lists():
    assert set(m.STATE_SOURCE) == set(m.STATES)
    assert set(m.SUPPORT_SOURCE) == {"no-link", "old-remote", "absent", "local"}
    if m._wire is not None:                               # после слияния с агентом B — те же коды, что у блока
        assert set(m.ERROR_SOURCE) == set(m._wire.ERROR_CODES)
        assert tuple(m.LAB_SCENARIOS) == tuple(m._wire.LAB_SCENARIOS)


def test_unit_restart_resets_history_times(view):
    """После перезапуска блока seq начинается заново: время перехода с тем же seq — новое, а не прежнее."""
    h = lambda seq, ago: {"seq": seq, "from": "INITIALIZING", "state": "READY", "reason": "init-ok", "code": "",  # noqa: E731
                          "ago_s": ago}
    view.update(telemetry={**TEL, "seq": 5, "since_s": 1.0, "history": [h(5, 1.0), h(4, 2.0)]}, **OK)
    old = view.history[1]["at"]
    view.update(telemetry=TEL, session="1.3", link="DISCONNECTED", mode="network")
    view.clock.t += 600
    view.update(telemetry={**TEL, "seq": 4, "since_s": 3.0, "history": [h(4, 3.0)]}, **OK)
    assert view.history[0]["at"] == m.clock_text(view.clock.t - 3.0) != old
    assert view.changedAt == m.clock_text(view.clock.t - 3.0)
