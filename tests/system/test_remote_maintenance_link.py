# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Протокол 1.6 сквозь настоящий процесс агента (loopback) и PeerClient: выгрузка журнала блока (части, SHA-256),
выключение — агент эмулятора завершается штатно (код 0); старый блок 1.5 — unsupported, ничего не исполняется."""
import time

from procs import Agent, wait_for
from sheregesh.link.client import PeerClient


def connected(pc):
    return wait_for(lambda: pc.snapshot()["phase"] == "connected", 10, 0.05)


def test_log_export_over_link():
    agent = Agent("--heartbeat-interval", "0.5")
    pc = PeerClient("127.0.0.1", agent.port, heartbeat_timeout_s=3.0).start()
    try:
        assert connected(pc)
        r = pc.remote_command("log-export", wait_s=15)
        assert r["ok"], r
        assert r["name"].startswith("sheregesh-remote-log-") and b"hello from console" in r["data"]
        assert pc.snapshot()["remote_cmd"]["state"] == "completed"
        assert pc.snapshot()["phase"] == "connected"           # канал жив после выгрузки
    finally:
        pc.stop()
        agent.stop()


def test_shutdown_stops_emulated_unit():
    agent = Agent("--heartbeat-interval", "0.5")
    pc = PeerClient("127.0.0.1", agent.port, heartbeat_timeout_s=3.0).start()
    try:
        assert connected(pc)
        t0 = time.monotonic()
        r = pc.remote_command("shutdown", wait_s=10)
        assert r["ok"] and r["result"] == "ok", r
        assert wait_for(lambda: agent.proc.poll() is not None, 10, 0.05)
        assert agent.proc.returncode == 0 and time.monotonic() - t0 < 6
    finally:
        pc.stop()
        agent.stop()


def test_old_remote_15_is_unsupported():
    agent = Agent("--protocol", "1.5", "--heartbeat-interval", "0.5")
    pc = PeerClient("127.0.0.1", agent.port, heartbeat_timeout_s=3.0).start()
    try:
        assert connected(pc) and pc.snapshot()["session"] == "1.5"
        for op in ("log-export", "shutdown"):
            assert pc.remote_command(op, wait_s=2)["result"] == "unsupported"
        time.sleep(1.5)
        assert agent.proc.poll() is None
    finally:
        pc.stop()
        agent.stop()
