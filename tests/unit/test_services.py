# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Обязательные службы (TEST-SW-001): снимок systemctl, очистка поля блока, события монитора при перезапуске и отказе."""
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
