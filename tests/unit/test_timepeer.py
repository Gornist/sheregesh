# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""sheregesh.timepeer (ADR-0017): строгое имя экземпляра, systemctl списком, ошибки не бросаются."""
import subprocess
import types

from sheregesh import timepeer as T


def test_instance_only_strict_addresses():
    assert T.instance("192.168.1.92") == "192.168.1.92"
    assert T.instance("FE80::1%eth0") == "fe80::1"
    for bad in (None, "", "127.0.0.1", "::1", "0.0.0.0", "224.0.0.1", "host.local", "1.2.3.4;id", "1.2.3.4/24"):
        assert T.instance(bad) is None, bad


def test_start_runs_unit_and_reports(monkeypatch):
    monkeypatch.delenv(T.SYSTEMCTL_ENV, raising=False)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stderr="")
    assert T.start("192.168.1.171", run=run) == (True, "192.168.1.171")
    assert calls == [["systemctl", "start", "sheregesh-time-peer@192.168.1.171.service"]]
    assert T.start("127.0.0.1", run=run)[0] is False and len(calls) == 1          # loopback не передаётся


def test_start_failures_do_not_raise():
    def fail(cmd, **kw):
        return types.SimpleNamespace(returncode=4, stderr="Access denied")

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 10)
    ok, why = T.start("192.168.1.171", run=fail)
    assert not ok and "4" in why and "Access denied" in why
    assert T.start("192.168.1.171", run=boom)[0] is False
