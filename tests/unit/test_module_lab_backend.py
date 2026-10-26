# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S6 (ADR-0012, раздел 7): LabEmulatorProcessBackend с настоящим дочерним процессом эмулятора модуля.

hello, crash → channel-down code=70, внешний SIGKILL, --refuse-start (69), отсутствие hello (подмена команды),
строка > 4096 байт, неблокирующая запись при зависшем процессе, close() ≤ 1 с, нет сирот. Реальное время (процессы),
ожидания короткие."""
import os
import signal
import sys
import time

import pytest

from sheregesh.module.lab import LabEmulatorProcessBackend
from sheregesh.module.states import Timings

FAST = Timings(hello_timeout_s=3.0, keepalive_interval_s=0.2, keepalive_timeout_s=1.0, stop_timeout_s=1.0)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as f:                       # Linux: зомби — уже не процесс
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def wait_gone(pid, timeout=3.0, backend=None):
    """Процесс исчез; backend — досматривать (poll), как это делает такт агента: иначе зомби не убран."""
    end = time.monotonic() + timeout
    while time.monotonic() < end and alive(pid):
        if backend is not None:
            backend.poll()
        time.sleep(0.02)
    return not alive(pid)


class Run:
    def __init__(self, command=None, timings=FAST):
        self.logs = []
        self.b = LabEmulatorProcessBackend("radiomaster-nomad", clock=time.monotonic, log=self.logs.append,
                                           timings=timings, command=command)
        self.events = []
        self.unread = []

    def poll_until(self, kind, timeout=5.0, **match):
        """Первое ещё не прочитанное событие kind (события одного poll() не теряются)."""
        end = time.monotonic() + timeout
        while True:
            new = self.b.poll()
            self.events += new
            self.unread += new
            for i, ev in enumerate(self.unread):
                if ev.kind == kind and all(ev.data.get(k) == v for k, v in match.items()):
                    del self.unread[:i + 1]
                    return ev
            if time.monotonic() >= end:
                break
            time.sleep(0.02)
        raise AssertionError(f"no {kind} {match} in {timeout} s: {self.events} {self.logs}")

    def started(self):
        self.b.start()
        ev = self.poll_until("channel-up")
        assert self.b.info()["state"] == "running" and self.b.pid > 0
        return ev.data["pid"]

    def close(self):
        self.b.close()


@pytest.fixture
def run():
    runs = []

    def make(*a, **kw):
        r = Run(*a, **kw)
        runs.append(r)
        return r
    yield make
    for r in runs:
        r.close()


def test_hello_probe_init_ping(run):
    r = run()
    assert r.b.send("probe") is False                                  # до hello канала нет
    pid = r.started()
    assert any(f"emulator started pid={pid}" in x for x in r.logs)
    assert r.b.send("probe")
    assert r.poll_until("presence").data == {"present": True}
    r.b.send("init", id=1, profile="radiomaster-nomad")
    ev = r.poll_until("init", timeout=3)
    assert ev.data["ok"] and ev.data["identity"]["model"] == "Nomad"
    r.b.send("ping", id=2)
    assert r.poll_until("pong").data == {"id": 2}
    for _ in range(15):                                                 # keepalive держит канал при молчании модуля
        assert not [e for e in r.b.poll() if e.kind == "channel-down"]
        time.sleep(0.1)
    assert r.b.info()["state"] == "running"


def test_crash_gives_exit_70(run):
    r = run()
    pid = r.started()
    r.b.send("lab", scenario="crash")
    assert r.poll_until("lab").data["ok"]                               # ответ на lab — до выхода
    ev = r.poll_until("channel-down")
    assert ev.data == {"reason": "exited", "code": 70}
    assert r.b.info()["last_exit"] == 70 and r.b.info()["state"] == "stopped" and r.b.info()["pid"] == 0
    assert wait_gone(pid, backend=r.b)


def test_external_sigkill(run):
    r = run()
    pid = r.started()
    os.kill(pid, signal.SIGKILL)
    ev = r.poll_until("channel-down")
    assert ev.data["reason"] == "exited" and ev.data["code"] == -signal.SIGKILL
    r.b.start()                                                         # перезапуск того же бэкенда
    r.poll_until("channel-up")
    assert r.b.pid != pid and r.b.info()["state"] == "running"


def test_refuse_start_and_lab_local(run):
    r = run()
    pid = r.started()
    assert r.b.lab_local("emu-unavailable")
    ev = r.poll_until("channel-down")
    assert ev.data["reason"] == "exited" and ev.data["code"] == -signal.SIGKILL and wait_gone(pid, backend=r.b)
    r.b.start()
    ev = r.poll_until("channel-down")
    assert ev.data == {"reason": "start-failed", "code": 69}
    assert r.b.lab_local("emu-available")
    r.started()
    assert r.b.lab_local("detach") is False


def test_no_hello_is_start_failed(run):
    r = run(command=[sys.executable, "-c", "import time; time.sleep(30)"],
            timings=Timings(hello_timeout_s=0.5, stop_timeout_s=1.0))
    r.b.start()
    pgid = r.b.proc.pid
    t0 = time.monotonic()
    ev = r.poll_until("channel-down", timeout=3)
    assert ev.data["reason"] == "start-failed" and 0.5 <= time.monotonic() - t0 < 1.5
    assert any("no hello" in x for x in r.logs)
    assert wait_gone(pgid, backend=r.b)


def test_too_long_line_is_protocol_violation(run):
    script = ("import json,sys,time; print(json.dumps({'ev':'hello','pid':1}), flush=True); "
              "print('{\"ev\":\"pong\",\"x\":\"' + 'y'*5000 + '\"}', flush=True); time.sleep(30)")
    r = run(command=[sys.executable, "-c", script])
    r.b.start()
    r.poll_until("channel-up")
    ev = r.poll_until("channel-down")
    assert ev.data["reason"] == "protocol"
    assert any("4096" in x for x in r.logs)


def test_first_line_must_be_hello(run):
    r = run(command=[sys.executable, "-c", "print('{\"ev\":\"pong\"}', flush=True); import time; time.sleep(30)"])
    r.b.start()
    assert r.poll_until("channel-down").data["reason"] == "protocol"


def test_hung_process_never_blocks_writes(run):
    r = run(timings=Timings(keepalive_interval_s=0.2, keepalive_timeout_s=1.0, stop_timeout_s=1.0))
    pid = r.started()
    os.kill(pid, signal.SIGSTOP)
    try:
        worst = 0.0
        payload = "x" * 3000
        down = None
        end = time.monotonic() + 5
        while time.monotonic() < end and down is None:
            t0 = time.monotonic()
            r.b.send("lab", scenario=payload)                           # ~3 КБ за раз: канал быстро переполняется
            worst = max(worst, time.monotonic() - t0)
            down = next((e for e in r.b.poll() if e.kind == "channel-down"), None)
        assert down is not None and down.data["reason"] == "no-keepalive"
        assert worst < 0.1, worst
    finally:
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
    assert wait_gone(pid, backend=r.b)


def test_close_is_bounded_and_leaves_no_orphans(run):
    r = run()
    pid = r.started()
    os.kill(pid, signal.SIGSTOP)                                        # не реагирует ни на EOF, ни на SIGTERM
    t0 = time.monotonic()
    r.b.close()
    assert time.monotonic() - t0 <= 1.3
    assert wait_gone(pid, 2)


def test_stop_is_non_blocking_and_reaps(run):
    r = run()
    pid = r.started()
    t0 = time.monotonic()
    r.b.stop()
    assert time.monotonic() - t0 < 0.05
    assert r.b.info()["state"] == "stopped"
    end = time.monotonic() + 3
    while time.monotonic() < end and (alive(pid) or r.b._dying):
        assert r.b.poll() == []                                         # после stop событий нет
        time.sleep(0.05)
    assert not alive(pid) and r.b._dying == []
