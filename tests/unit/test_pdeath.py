# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Сторож pdeath.py (вне Linux вместо setpriv --pdeathsig): процесс GStreamer не переживает своего родителя."""
import os
import signal
import subprocess
import sys
import time

from sheregesh import video

PDEATH = str(video._PDEATH)
SLEEPER = [sys.executable, "-c", "import os, time; print(os.getpid(), flush=True); time.sleep(60)"]


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_dead(pid, timeout=3.0):
    deadline = time.monotonic() + timeout
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not alive(pid)


def test_child_killed_when_parent_dies():
    parent_code = ("import os, subprocess, sys, time; "
                   f"subprocess.Popen([sys.executable, '-I', {PDEATH!r}, '--parent', str(os.getpid()), '--', *{SLEEPER!r}]); "
                   "time.sleep(60)")
    parent = subprocess.Popen([sys.executable, "-c", parent_code], stdout=subprocess.PIPE, text=True)
    try:
        child = int(parent.stdout.readline())       # вывод команды проходит через сторож без изменений
        assert alive(child)
        parent.kill()
        parent.wait(5)
        assert wait_dead(child), "команда пережила родителя сторожа"
    finally:
        parent.kill()
        parent.wait(5)


def test_exit_code_passthrough_and_signal_forwarding():
    me = str(os.getpid())
    rc = subprocess.run([sys.executable, "-I", PDEATH, "--parent", me, "--", sys.executable, "-c", "raise SystemExit(3)"])
    assert rc.returncode == 3
    p = subprocess.Popen([sys.executable, "-I", PDEATH, "--parent", me, "--", *SLEEPER], stdout=subprocess.PIPE, text=True)
    child = int(p.stdout.readline())
    p.send_signal(signal.SIGTERM)
    assert p.wait(5) == 128 + signal.SIGTERM
    assert wait_dead(child)


def test_dead_parent_at_start_kills_immediately():
    p = subprocess.Popen([sys.executable, "-I", PDEATH, "--parent", "1", "--", *SLEEPER], stdout=subprocess.PIPE, text=True)
    assert p.wait(5) == 128 + signal.SIGKILL
    printed = p.stdout.read().split()                # команда могла не успеть напечатать свой PID
    assert all(wait_dead(int(pid)) for pid in printed)


def test_bad_usage():
    assert subprocess.run([sys.executable, "-I", PDEATH, "sleep", "1"], capture_output=True).returncode == 2
