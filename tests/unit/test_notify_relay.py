# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ретранслятор sd_notify пульта: уведомления внука (приложения под cage) уходят в systemd от главного PID."""
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from sheregesh.notify_relay import filter_lines

ROOT = Path(__file__).resolve().parents[2]
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "software")}
# «Приложение» за промежуточным процессом (как cage): шлёт READY, MAINPID (должен быть отброшен), WATCHDOG.
GRANDCHILD = ("import os, socket, sys, time; "
              "a = os.environ['NOTIFY_SOCKET']; s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM); "
              "print('wdpid', os.environ.get('WATCHDOG_PID'), 'usec', os.environ.get('WATCHDOG_USEC'), flush=True); "
              "s.sendto(b'READY=1\\nSTATUS=app ready\\nMAINPID=1', a); s.sendto(b'MAINPID=1', a); "
              "s.sendto(b'WATCHDOG=1', a); time.sleep(float(sys.argv[1])); sys.exit(7)")


def upstream():
    d = tempfile.mkdtemp(prefix="sd", dir="/tmp")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(os.path.join(d, "n"))
    s.settimeout(15)
    return d, s


def relay(d, sleep, **extra):
    inner = [sys.executable, "-c", GRANDCHILD, str(sleep)]
    middle = [sys.executable, "-c", "import subprocess, sys; sys.exit(subprocess.call(sys.argv[1:]))", *inner]
    env = {**ENV, "NOTIFY_SOCKET": os.path.join(d, "n"), "WATCHDOG_USEC": "4000000", "WATCHDOG_PID": "1", **extra}
    return subprocess.Popen([sys.executable, "-m", "sheregesh.notify_relay", "--", *middle], env=env,
                            stdout=subprocess.PIPE, text=True)


def test_filter_drops_mainpid_and_unknown():
    assert filter_lines(b"READY=1\nMAINPID=1\nFDSTORE=1\nSTATUS=ok\nWATCHDOG=1") == ["READY=1", "STATUS=ok", "WATCHDOG=1"]
    assert filter_lines(b"MAINPID=42") == []


def test_grandchild_notifications_relayed_from_main_pid():
    d, s = upstream()
    p = relay(d, 0.5, RUNTIME_DIRECTORY=d)
    try:
        line = p.stdout.readline().split()
        assert line[1] == "None" and line[3] == "4000000"      # WATCHDOG_PID снят: пингует приложение
        msgs = [s.recv(512).decode() for _ in range(2)]
        assert msgs == ["READY=1\nSTATUS=app ready", "WATCHDOG=1"]
        assert p.wait(15) == 7                                   # код выхода ребёнка
        assert not [x for x in os.listdir(d) if x.startswith("notify-")]   # свой сокет убран
    finally:
        p.kill()
        s.close()
        shutil.rmtree(d)


def test_sigterm_forwarded_to_child():
    d, s = upstream()
    p = relay(d, 60)
    try:
        assert s.recv(512).decode().startswith("READY=1")
        p.send_signal(signal.SIGTERM)
        t0 = time.monotonic()
        assert p.wait(15) == 0 and time.monotonic() - t0 < 10        # штатный останов — не «failed» у systemd
    finally:
        p.kill()
        s.close()
        shutil.rmtree(d)


def test_without_systemd_just_runs_command():
    r = subprocess.run([sys.executable, "-m", "sheregesh.notify_relay", "--", sys.executable, "-c", "raise SystemExit(3)"],
                       env={k: v for k, v in ENV.items() if k != "NOTIFY_SOCKET"}, timeout=20)
    assert r.returncode == 3


def test_watchdog_abort_kills_frozen_grandchild():
    """pi1, ee56bf1: после SIGABRT watchdog старые cage и замороженное приложение не должны пережить ретранслятор."""
    d, s = upstream()
    p = relay(d, 120, SHEREGESH_RELAY_GRACE_S="1")
    try:
        assert s.recv(512).decode().startswith("READY=1")
        pgid = os.getpgid(p.pid)
        members = [int(x) for x in subprocess.run(["pgrep", "-P", str(p.pid)], capture_output=True,
                                                    text=True).stdout.split()]
        assert members, "нет дочернего процесса"
        group = os.getpgid(members[0])
        assert group != pgid                                       # ребёнок — в своей группе
        grand = [int(x) for x in subprocess.run(["pgrep", "-g", str(group)], capture_output=True, text=True).stdout.split()]
        assert len(grand) >= 2
        for pid in grand:
            os.kill(pid, signal.SIGSTOP)                           # «зависли» и cage, и приложение
        p.send_signal(signal.SIGABRT)
        assert p.wait(15) == 128 + signal.SIGABRT
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and subprocess.run(["pgrep", "-g", str(group)], capture_output=True).returncode == 0:
            time.sleep(0.1)
        assert subprocess.run(["pgrep", "-g", str(group)], capture_output=True).returncode != 0
    finally:
        p.kill()
        s.close()
        shutil.rmtree(d)


ADOPTING = ("import os, sys, time; sys.path.insert(0, sys.argv[1]); "
            "from sheregesh.notify_relay import adopt_pdeathsig; print(adopt_pdeathsig(), os.getpid(), flush=True); "
            "time.sleep(120)")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG только в Linux")
def test_relay_sigkill_leaves_no_processes_even_with_frozen_app():
    """Аварийная гибель самого ретранслятора: cage (PDEATHSIG от ретранслятора) и замороженное приложение
    (adopt_pdeathsig) не остаются."""
    inner = [sys.executable, "-c", ADOPTING, str(ROOT / "software")]
    middle = [sys.executable, "-c", "import subprocess, sys; sys.exit(subprocess.call(sys.argv[1:]))", *inner]
    env = {k: v for k, v in ENV.items() if k != "NOTIFY_SOCKET"}
    p = subprocess.Popen([sys.executable, "-m", "sheregesh.notify_relay", "--", *middle], env=env,
                         stdout=subprocess.PIPE, text=True)
    try:
        adopted, app = p.stdout.readline().split()
        assert adopted == "True"
        app = int(app)
        cage = int(open(f"/proc/{app}/stat").read().split(")")[1].split()[1])
        os.kill(app, signal.SIGSTOP)                                # приложение «зависло»
        p.kill()                                                   # ретранслятор убит аварийно
        p.wait(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (os.path.exists(f"/proc/{app}") or os.path.exists(f"/proc/{cage}")):
            time.sleep(0.1)
        for pid in (cage, app):
            st = open(f"/proc/{pid}/stat").read().split(")")[1].split()[0] if os.path.exists(f"/proc/{pid}") else None
            assert st in (None, "Z"), f"процесс {pid} остался: {st}"
    finally:
        p.kill()
