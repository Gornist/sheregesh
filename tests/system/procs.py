# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Запуск процесса эмулятора удалённого блока для интеграционных тестов."""
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV = {**os.environ, "PYTHONPATH": os.pathsep.join(str(ROOT / p) for p in
                                                    ("software", "software/emulators", "software/remote"))}


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Agent:
    def __init__(self, *args, port=None):
        self.port = port or free_port()
        self.proc = subprocess.Popen([sys.executable, "-m", "sheregesh_remote", "--port", str(self.port), *args],
                                     env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     start_new_session=True)
        self.banner = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            self.banner.append(line.strip())
            if line.startswith("LISTENING"):
                break
        assert any(b.startswith("LISTENING") for b in self.banner), self.stderr()

    def stderr(self):
        try:
            return self.proc.communicate(timeout=1)[1]
        except subprocess.TimeoutExpired:
            return ""

    def signal(self, sig):
        os.kill(self.proc.pid, sig)

    def stop(self):
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGCONT)
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()


def wait_for(pred, timeout, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()
