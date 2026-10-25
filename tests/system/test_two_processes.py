# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Два независимых процесса на 127.0.0.1: пульт (клиент + монитор) и эмулятор удалённого блока (L3).

Реальный TCP через loopback и реальные сигналы ОС. Это не испытание на эталонном Ethernet (L4).
"""
import signal
import time

import pytest
import yaml

from procs import Agent, free_port, wait_for
from sheregesh.config import console_defaults
from sheregesh.link.client import PeerClient, RemotePeer
from sheregesh_emulators import Bench

DIAG = yaml.safe_load(console_defaults()["diagnostics.yaml"])
LINK = yaml.safe_load(console_defaults()["link.yaml"])     # heartbeat_timeout_s = 3


@pytest.fixture
def agents():
    started = []

    def start(*args, **kw):
        a = Agent(*args, **kw)
        started.append(a)
        return a
    yield start
    for a in started:
        a.stop()


@pytest.fixture
def client():
    made = []

    def make(port, timeout=LINK["heartbeat_timeout_s"]):
        c = PeerClient(port=port, heartbeat_timeout_s=timeout).start()
        made.append(c)
        return c
    yield make
    for c in made:
        c.stop()


def bench_for(c):
    return Bench(DIAG, LINK, peer_factory=lambda clock: RemotePeer(c, clock))


def run_real(bench, seconds, dt=0.1):
    """Монитор в реальном времени: шаг модельных часов = реальная пауза."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(dt)
        bench.step(dt)
    return bench.snapshot


def codes(bench):
    return {e.code for e in bench.snapshot.active}


def logged(bench, code):
    return [e for e in bench.monitor.log.events if e.code == code]


def phase(c):
    return c.snapshot()["phase"]


# ---------------- 1. успешное соединение ----------------
@pytest.mark.test_id("TEST-SYS-002")
@pytest.mark.test_id("TEST-SW-003")
def test_successful_connection_exchanges_synthetic_state(agents, client):
    a = agents()
    c = client(a.port)
    assert wait_for(lambda: phase(c) == "connected", 5)
    snap = wait_for(lambda: c.snapshot()["telemetry"] and c.snapshot(), 5)
    assert snap["session"] == "1.2" and snap["peer"]["device_id"] == "remote-emu-01"
    t = snap["telemetry"]
    assert {"soc", "board"} <= set(t["temperatures"]) and 0 < t["power"]["percent"] <= 100
    b = bench_for(c)
    s = run_real(b, 3)
    assert s.peer_state == "connected" and not codes(b) & {"PEER-LOST", "PEER-INCOMPAT", "PEER-SLOW"}
    assert s.peer.telemetry["temperatures"]["soc"] > 0
    cnt = c.snapshot()["counters"]
    assert cnt["bad_messages"] == 0 and cnt["seq_gaps"] == 0 and cnt["received"] >= 6


@pytest.mark.test_id("TEST-SYS-002")
def test_ten_minutes_equivalent_exchange_without_loss(agents, client):
    """Ускоренный аналог критерия «10 мин без потерь»: heartbeat 20 раз в секунду, 10 с → ≥ 300 сообщений."""
    a = agents("--heartbeat-interval", "0.05")
    c = client(a.port)
    assert wait_for(lambda: phase(c) == "connected", 5)
    time.sleep(10)
    cnt = c.snapshot()["counters"]
    assert cnt["received"] >= 300 and cnt["seq_gaps"] == 0 and cnt["bad_messages"] == 0


# ---------------- 2. несовместимые версии ----------------
@pytest.mark.test_id("TEST-SW-003")
@pytest.mark.test_id("TEST-SYS-008")
@pytest.mark.parametrize("args,reason", [(("--protocol", "2.0"), "major"),
                                         (("--hardware", "HW-9.9 (неизвестная)"), "hardware")])
def test_incompatible_peer_is_shown_and_data_not_accepted(agents, client, args, reason):
    a = agents(*args)
    c = client(a.port)
    assert wait_for(lambda: phase(c) == "incompatible", 5)
    assert c.snapshot()["detail"] == reason and c.snapshot()["telemetry"] is None
    b = bench_for(c)
    run_real(b, 1)
    assert "PEER-INCOMPAT" in codes(b) and b.snapshot.peer_state == "incompatible"


@pytest.mark.test_id("TEST-SW-003")
def test_newer_minor_peer_negotiates_lower_session(agents, client):
    a = agents("--protocol", "1.4")
    c = client(a.port)
    assert wait_for(lambda: phase(c) == "connected", 5)
    assert c.snapshot()["session"] == "1.2"


# ---------------- 3. второго процесса нет ----------------
@pytest.mark.test_id("TEST-SW-004")
def test_missing_remote_process(client):
    c = client(free_port())
    assert wait_for(lambda: c.snapshot()["detail"] == "refused", 3)
    b = bench_for(c)
    t0 = b.clock()
    for _ in range(50):
        b.step(0.1)
        time.sleep(0.1)
        if "PEER-LOST" in codes(b):
            break
    assert "PEER-LOST" in codes(b) and b.clock() - t0 <= 5
    assert b.snapshot.peer.detail == "refused"


# ---------------- 4. неожиданное завершение и 5. восстановление ----------------
@pytest.mark.test_id("TEST-SW-004")
@pytest.mark.test_id("TEST-SYS-002")
def test_crash_detected_and_recovered_after_restart(agents, client):
    a = agents("--fault", "2:crash")
    c = client(a.port)
    assert wait_for(lambda: phase(c) == "connected", 5)
    b = bench_for(c)
    run_real(b, 1)
    assert a.proc.wait(timeout=10) == 70, "эмулятор должен аварийно завершиться"
    run_real(b, 1)
    assert "PEER-LOST" in codes(b) and c.snapshot()["detail"] in ("closed", "refused")
    t_restart = time.monotonic()
    agents(port=a.port)                               # новый процесс на том же порту
    assert wait_for(lambda: phase(c) == "connected", 10)
    assert time.monotonic() - t_restart <= 10
    run_real(b, 1)
    assert "PEER-LOST" not in codes(b)
    assert [e.active for e in logged(b, "PEER-LOST")] == [False]
    assert c.snapshot()["counters"]["connects"] == 2


@pytest.mark.test_id("TEST-SW-004")
def test_sigkill_without_goodbye(agents, client):
    a = agents()
    c = client(a.port)
    assert wait_for(lambda: phase(c) == "connected", 5)
    a.signal(signal.SIGKILL)
    assert wait_for(lambda: phase(c) == "connecting", 5)


# ---------------- 6. повреждённые сообщения ----------------
@pytest.mark.test_id("TEST-SYS-002")
def test_corrupted_messages_are_discarded_link_kept(agents, client):
    kinds = ["garbage", "json", "fields", "utf8", "long"]
    a = agents(*[a for i, k in enumerate(kinds) for a in ("--fault", f"{1.5 + 0.2 * i}:corrupt:{k}")])
    c = client(a.port)
    b = bench_for(c)
    run_real(b, 4)
    cnt = c.snapshot()["counters"]
    assert cnt["bad_messages"] == 5 and cnt["connects"] == 1
    assert phase(c) == "connected" and b.snapshot.peer_state == "connected"
    reasons = sorted(e.params["reason"] for e in logged(b, "LINK-BAD-MSG"))
    assert reasons == sorted(["not-json", "not-json", "fields", "not-utf8", "too-long"])


def test_flood_of_corrupted_messages_resets_connection(agents, client):
    a = agents(*[a for i in range(12) for a in ("--fault", f"{1.5 + 0.01 * i}:corrupt:garbage")])
    c = client(a.port)
    assert wait_for(lambda: c.snapshot()["counters"]["connects"] >= 2, 10)
    assert wait_for(lambda: phase(c) == "connected", 5)


# ---------------- 7. задержка и отсутствие heartbeat ----------------
@pytest.mark.test_id("TEST-SW-004")
def test_delayed_heartbeat_warns_without_disconnect(agents, client):
    a = agents("--fault", "2:delay:1.2")
    c = client(a.port)
    b = bench_for(c)
    run_real(b, 7)
    assert logged(b, "PEER-SLOW"), "задержка должна дать предупреждение PEER-SLOW"
    assert not logged(b, "PEER-LOST") and c.snapshot()["counters"]["connects"] == 1


@pytest.mark.test_id("TEST-SW-004")
@pytest.mark.test_id("TEST-SYS-002")
def test_silent_peer_detected_and_link_recovers(agents, client):
    a = agents("--fault", "2:silence:6")
    c = client(a.port)
    b = bench_for(c)
    run_real(b, 2)
    assert b.snapshot.peer_state == "connected"
    t0 = b.clock()
    lost_at = None
    for _ in range(80):
        b.step(0.1)
        time.sleep(0.1)
        if lost_at is None and "PEER-LOST" in codes(b):
            lost_at = b.clock()
    assert lost_at is not None and lost_at - t0 <= 5, "отсутствие heartbeat — не позднее 5 с"
    assert logged(b, "LINK-HB-TIMEOUT")
    assert wait_for(lambda: phase(c) == "connected", 10)
    run_real(b, 1)
    assert "PEER-LOST" not in codes(b)


@pytest.mark.test_id("TEST-SW-004")
def test_frozen_process_detected_and_resumed(agents, client):
    """Процесс жив, но заморожен (SIGSTOP): TCP не рвётся, heartbeat нет."""
    a = agents()
    c = client(a.port)
    b = bench_for(c)
    run_real(b, 2)
    a.signal(signal.SIGSTOP)
    run_real(b, 5)
    assert "PEER-LOST" in codes(b)
    a.signal(signal.SIGCONT)
    assert wait_for(lambda: phase(c) == "connected", 10)
    run_real(b, 1)
    assert "PEER-LOST" not in codes(b)


def test_remote_alerts_shown_on_console(agents, client):
    a = agents("--fault", "1.5:alert:TEMP-HIGH", "--fault", "4:alert-clear")
    c = client(a.port)
    b = bench_for(c)
    run_real(b, 3)
    assert "REMOTE-ALERT" in codes(b)
    run_real(b, 2.5)
    assert "REMOTE-ALERT" not in codes(b)


def test_agent_refuses_non_loopback_bind():
    import subprocess
    import sys
    from procs import ENV
    r = subprocess.run([sys.executable, "-m", "sheregesh_remote", "--host", "0.0.0.0", "--port", "0"],
                       env=ENV, capture_output=True, text=True, timeout=10)
    assert r.returncode != 0 and "loopback" in r.stderr


def test_client_refuses_non_loopback_target():
    with pytest.raises(ValueError):
        PeerClient(host="192.168.77.2")


def test_video_sender_dies_with_crashed_agent():
    """Аварийное завершение блока не оставляет живой видеогенератор (дефект S1.5)."""
    from sheregesh import video as gst
    if gst.choose_codec() is None:
        pytest.skip("нет кодека")
    import subprocess as sp
    a = Agent("--video", "--video-port", str(free_port(kind=2)), "--video-width", "320", "--video-height", "180")
    try:
        def descendants(pid):          # вне Linux между блоком и gst-launch есть сторож pdeath.py
            kids = sp.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()
            return kids + [d for k in kids for d in descendants(k)]
        def gst_running():
            pids = descendants(a.proc.pid)
            names = sp.run(["ps", "-o", "comm=", "-p", ",".join(pids)], capture_output=True, text=True).stdout if pids else ""
            return pids if "gst-launch" in names else []
        children = wait_for(gst_running, 5)
        assert children, "видеогенератор не запущен"
        a.signal(signal.SIGKILL)
        a.proc.wait(5)
        gone = wait_for(lambda: all(sp.run(["kill", "-0", c], capture_output=True).returncode != 0 for c in children), 5)
        assert gone, "видеогенератор пережил свой процесс"
    finally:
        a.stop()
