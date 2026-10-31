# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Обязательные службы (TEST-SW-001): снимок по D-Bus и systemctl, очистка поля блока, события монитора при перезапуске и отказе."""
import pytest

from sheregesh import services as svc
from sheregesh.hal import PeerStatus
from sheregesh_emulators.scenario import Bench

DIAG = {"temp_warning_c": 70, "temp_critical_c": 80, "battery_warning_pct": 20, "battery_critical_pct": 10}
LINK = {"heartbeat_timeout_s": 3}

SHOW = """Id=sheregesh-console.service
LoadState=loaded
ActiveState=active
SubState=running
NRestarts=0
Result=success

Id=sheregesh-video-rx.service
LoadState=loaded
ActiveState=activating
SubState=auto-restart
NRestarts=2
Result=signal

Id=sheregesh-video-tx.service
LoadState=not-found
ActiveState=inactive
SubState=dead
NRestarts=0
Result=success
"""


def test_snapshot_parses_show_and_skips_units_that_are_not_installed():
    calls = []

    def run(argv):
        calls.append(argv)
        return SHOW
    snap = svc.snapshot(("sheregesh-console.service", "sheregesh-video-rx.service", "sheregesh-video-tx.service"), run)
    assert calls[0][:3] == ["systemctl", "show", "-p"]
    assert snap == {"sheregesh-console.service": {"active": "active", "sub": "running", "restarts": 0, "result": "success"},
                    "sheregesh-video-rx.service": {"active": "activating", "sub": "auto-restart", "restarts": 2,
                                                   "result": "signal"}}


def _bus(rows, props):
    """SystemdBus без шины: ответы ListUnitsByNames и Properties.Get подменены."""
    bus = object.__new__(svc.SystemdBus)
    calls = []

    def call(path, iface, method, args, reply):
        calls.append((path, method, args))
        if method == "ListUnitsByNames":
            return (rows,)
        return (props[(path, args[1][1])],)
    bus._call = call
    return bus, calls


def test_dbus_snapshot_reads_state_and_service_props_and_skips_not_loaded():
    rows = [("sheregesh-console.service", "", "loaded", "active", "running", "", "/u/console", 0, "", "/"),
            ("sheregesh-video-rx.service", "", "loaded", "activating", "auto-restart", "", "/u/rx", 0, "", "/"),
            ("sheregesh-video-tx.service", "", "not-found", "inactive", "dead", "", "/u/tx", 0, "", "/")]
    props = {("/u/console", "NRestarts"): 0, ("/u/console", "Result"): "success",
             ("/u/rx", "NRestarts"): 2, ("/u/rx", "Result"): "signal"}
    bus, calls = _bus(rows, props)
    names = ("sheregesh-console.service", "sheregesh-video-rx.service", "sheregesh-video-tx.service")
    assert bus.snapshot(names) == svc.snapshot(names, lambda argv: SHOW)           # тот же снимок, что у systemctl
    assert calls[0][1] == "ListUnitsByNames" and calls[0][2] == ("(as)", (list(names),))
    assert not any(c[0] == "/u/tx" for c in calls)                                  # не установлена — не опрашивается


def test_cached_uses_dbus_and_falls_back_to_systemctl_for_good():
    t = {"now": 0.0}
    shows = []
    state = {"fail": False, "made": 0}

    class Bus:
        def __init__(self):
            state["made"] += 1

        def snapshot(self, names):
            if state["fail"]:
                raise OSError("bus gone")
            return {"sheregesh-console.service": {"active": "active", "sub": "running", "restarts": 0,
                                                  "result": "success"}}

    def run(argv):
        shows.append(argv)
        return SHOW
    c = svc.Cached("console", period_s=2.0, run=run, clock=lambda: t["now"], bus=Bus)
    assert c.backend == "dbus" and "sheregesh-console.service" in c() and not shows
    t["now"] = 1.0
    c()
    assert state["made"] == 1                                    # не чаще period_s, шина одна
    state["fail"], t["now"] = True, 2.5
    assert c()["sheregesh-video-rx.service"]["restarts"] == 2 and c.backend == "systemctl" and len(shows) == 1
    state["fail"], t["now"] = False, 5.0
    c()
    assert c.backend == "systemctl" and len(shows) == 2 and state["made"] == 1


def test_cached_without_bus_factory_or_gi_uses_systemctl():
    assert svc.Cached("console", bus=None, run=lambda a: SHOW).backend == "systemctl"

    def no_gi():
        raise ImportError("no gi")
    c = svc.Cached("console", bus=no_gi, run=lambda a: SHOW)
    assert c()["sheregesh-console.service"]["active"] == "active" and c.backend == "systemctl"


def test_sanitize_keeps_only_expected_fields_and_names():
    raw = {"sheregesh-video-tx.service": {"active": "active", "sub": "running", "restarts": 3, "result": "success",
                                          "extra": "x" * 100},
           "evil.service": {"active": "active"}, "sheregesh-x": "not a dict",
           "sheregesh-remote-net.service": {"active": "failed" * 10, "restarts": True}}
    out = svc.sanitize(raw)
    assert set(out) == {"sheregesh-video-tx.service", "sheregesh-remote-net.service"}
    assert out["sheregesh-video-tx.service"] == {"active": "active", "sub": "running", "result": "success", "restarts": 3}
    assert len(out["sheregesh-remote-net.service"]["active"]) <= 16 and out["sheregesh-remote-net.service"]["restarts"] == 0
    assert svc.sanitize("x") is None


def snap(active="active", restarts=0):
    return {"active": active, "sub": "running", "restarts": restarts, "result": "success"}


@pytest.mark.test_id("TEST-SW-001")
def test_console_service_restart_and_failure_raise_events_until_back():
    local = {"sheregesh-video-rx.service": snap()}
    b = Bench(DIAG, LINK, services=lambda: local)
    b.step()
    assert not [e for e in b.monitor.log.events if e.code.startswith("SVC-")]
    local["sheregesh-video-rx.service"] = snap(restarts=1)
    b.step()
    ev = [e for e in b.monitor.log.events if e.code == "SVC-RESTART"]
    assert len(ev) == 1 and ev[0].params == {"service": "video-rx", "restarts": 1}
    local["sheregesh-video-rx.service"] = snap(active="failed", restarts=1)
    b.step()
    assert b.monitor.log.is_active("SVC-DOWN") and "video-rx (failed)" in b.monitor.log._active["SVC-DOWN"].params["services"]
    local["sheregesh-video-rx.service"] = snap(restarts=1)
    b.step()
    assert not b.monitor.log.is_active("SVC-DOWN")
    assert b.snapshot.services["console"] == local


@pytest.mark.test_id("TEST-SW-001")
def test_gui_restart_after_crash_reports_its_own_failure_once():
    """Пульт перезапущен после сбоя GUI: первый снимок — уже с restarts > 0 — даёт одно событие."""
    b = Bench(DIAG, LINK, services=lambda: {"sheregesh-console.service": snap(restarts=1)})
    for _ in range(3):
        b.step()
    assert [e.params for e in b.monitor.log.events if e.code == "SVC-RESTART"] == [{"service": "console", "restarts": 1}]


class Peer:
    def __init__(self):
        self.telemetry, self.up = {}, True

    def status(self):
        return PeerStatus(self.up, 0.0, "1.5", "0.1.0.dev0", "HW-1.0 Rev A", "remote-1", telemetry=self.telemetry)


@pytest.mark.test_id("TEST-SW-001")
def test_remote_services_from_telemetry_and_ignored_without_link():
    peer = Peer()
    b = Bench(DIAG, LINK, peer_factory=lambda clock: peer)
    peer.telemetry = {"services": {"sheregesh-video-tx.service": snap()}}
    b.step()
    peer.telemetry = {"services": {"sheregesh-video-tx.service": snap(restarts=1)}}
    b.step()
    assert [e.params for e in b.monitor.log.events if e.code == "SVC-RESTART"] == [{"service": "video-tx", "restarts": 1}]
    peer.telemetry = {"services": {"sheregesh-video-tx.service": snap(active="failed", restarts=1)}}
    peer.up = False                                    # связи нет — службы блока не судим
    b.step()
    assert not b.monitor.log.is_active("SVC-DOWN") and b.snapshot.services["remote"] == {}
