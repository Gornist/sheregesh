# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Запуск одной командой: tools/dev/sheregesh-emu поднимает два процесса и завершает их по Ctrl+C."""
import os
import signal
import socket
import subprocess
import sys
import time

from procs import ROOT, free_port, wait_for


def children_of(pgid):
    """[pid, pgid, имя] процессов группы; имя без пути (в macOS ps выводит полный путь, напр. .../MacOS/Python)."""
    out = subprocess.run(["ps", "-eo", "pid,pgid,comm"], capture_output=True, text=True).stdout.split("\n")[1:]
    rows = [line.split(None, 2) for line in out if line.split()]
    return [[pid, pg, os.path.basename(comm).lower()] for pid, pg, comm in rows if pg == str(pgid)]


def test_one_command_starts_two_processes_and_stops_cleanly():
    port, vport = free_port(), free_port(socket.SOCK_DGRAM)
    env = {**os.environ, "SHEREGESH_PYTHON": sys.executable, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software",
           "SHEREGESH_PORT": str(port), "SHEREGESH_VIDEO_PORT": str(vport)}
    p = subprocess.Popen([str(ROOT / "tools/dev/sheregesh-emu"), "--demo"], env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        def listening():
            with socket.socket() as s:
                return s.connect_ex(("127.0.0.1", port)) == 0
        if not wait_for(listening, 15):
            os.killpg(p.pid, signal.SIGKILL)
            raise AssertionError("процесс удалённого блока не слушает порт:\n" + p.stdout.read())
        comms = lambda: {c[2] for c in children_of(p.pid)}           # noqa: E731
        assert wait_for(lambda: sum(1 for c in children_of(p.pid) if c[2].startswith("python")) >= 2, 15), comms()
        time.sleep(3)
        os.killpg(p.pid, signal.SIGINT)                               # как Ctrl+C в терминале
        p.wait(timeout=20)
        assert wait_for(lambda: not children_of(p.pid), 5), f"остались процессы: {children_of(p.pid)}"
        out = p.stdout.read()
        assert "Traceback" not in out, out
        assert "hello from console proto=1.2 -> ok" in out
    finally:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
