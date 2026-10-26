# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S7 (ADR-0013, 4.2): передача пульта (StickSender, поток 50 Гц) через настоящий PeerClient к настоящему процессу
агента (loopback). Источник стиков подменный (аппаратуры нет). Проверяется сквозное поведение пульта: после старта
передача выключена и блок IDLE; включение → ACTIVE, эхо каналов, задержка по часам клиента; выключение → IDLE
(disabled); пропажа устройства → FAILSAFE (device-lost), передача выключена и после возврата устройства; остановка
передачи пульта → FAILSAFE (timeout). Протокол и сторож блока проверяет test_rc_link (агент R)."""
import statistics
import time

import pytest

from procs import Agent, wait_for
from sheregesh.link.client import PeerClient
from sheregesh_console import sticks as s

AXES = [0, -12, 1000, 3, -1000, 640, -355, 1000]


class FakeSource:
    def __init__(self):
        self.st = {**s.empty_state(), "present": True, "name": "OpenTX Radiomaster Boxer Joystick",
                   "axes": list(AXES), "buttons": 5, "axis_count": 8, "button_count": 24, "generation": 1}

    def state(self):
        return dict(self.st, axes=list(self.st["axes"]))

    def unplug(self):
        self.st.update(present=False, axes=[0] * 8, buttons=0)

    def plug(self):
        self.st.update(present=True, axes=list(AXES), buttons=5, generation=self.st["generation"] + 1)


@pytest.fixture
def rig(tmp_path):
    agent = Agent("--heartbeat-interval", "0.3", "--status-file", str(tmp_path / "st.json"))
    pc = PeerClient("127.0.0.1", agent.port, heartbeat_timeout_s=3).start()
    src = FakeSource()
    snd = s.StickSender(src, lambda: pc).start()
    try:
        assert wait_for(lambda: pc.snapshot()["phase"] == "connected", 10)
        yield agent, pc, src, snd
    finally:
        snd.stop()
        pc.stop()
        agent.stop()


def remote(pc):
    return pc.snapshot().get("rc_state") or {}


def test_end_to_end_enable_disable_and_device_loss(rig):
    agent, pc, src, snd = rig
    assert pc.snapshot()["session"] == "1.4"
    assert wait_for(lambda: remote(pc).get("state") == "IDLE", 3)       # ST-02: после старта пульта — IDLE
    time.sleep(0.3)
    assert snd.status()["sent"] == 0 and snd.enabled is False

    assert snd.set_enabled(True) == "ok"
    t0 = time.monotonic()
    assert wait_for(lambda: remote(pc).get("state") == "ACTIVE", 1.0)    # ST-03
    assert time.monotonic() - t0 <= 1.0
    assert wait_for(lambda: remote(pc).get("ch") == AXES and remote(pc).get("btn") == 5, 1.0)   # ST-04
    time.sleep(2.5)
    r = remote(pc)
    assert r["rate_hz"] >= 45 and r["gaps"] == 0 and r["output"] == "none"
    lat = snd.status()["latency"]
    assert lat["source"] == "client" and lat["samples"] >= 10
    assert lat["median_ms"] <= 30 and statistics.median(lat["recent_ms"]) <= 30   # ST-05 (loopback)

    assert snd.set_enabled(False) == "ok"                                 # ST-07: оператор — IDLE, не FAILSAFE
    assert wait_for(lambda: remote(pc).get("state") == "IDLE" and remote(pc).get("reason") == "disabled", 1.0)

    assert snd.set_enabled(True) == "ok"
    assert wait_for(lambda: remote(pc).get("state") == "ACTIVE", 1.0)
    src.unplug()                                                          # ST-08
    assert wait_for(lambda: remote(pc).get("state") == "FAILSAFE" and remote(pc).get("reason") == "device-lost", 1.0)
    assert snd.enabled is False and snd.status()["off_reason"] == "device-lost"
    src.plug()
    sent = snd.status()["sent"]
    time.sleep(0.5)
    assert snd.enabled is False and snd.status()["sent"] == sent          # вернули — передача выключена


def test_console_stop_gives_failsafe_on_remote(rig):
    agent, pc, src, snd = rig
    assert snd.set_enabled(True) == "ok"
    assert wait_for(lambda: remote(pc).get("state") == "ACTIVE", 1.5)
    last = time.monotonic()
    snd.stop()                                                            # ST-06: прощального кадра нет
    assert wait_for(lambda: remote(pc).get("state") == "FAILSAFE", 2.0)
    r = remote(pc)
    assert r["reason"] == "timeout" and r["ch"] == [0] * 8 and r["btn"] == 0
    assert time.monotonic() - last <= 0.5 + 0.1 + 0.3                      # timeout + сторож + период rc-state/опрос
