# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Пульт как служба systemd (ADR-0002, S3.1): READY после инициализации приложения, WATCHDOG из цикла Qt;
состояния канала для интерфейса."""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from sheregesh_console.backend import peer_link

ROOT = Path(__file__).resolve().parents[2]


def test_peer_link_states():
    assert peer_link("connected", {}) == "CONNECTED"
    assert peer_link("auth-failed", {"lost_s": 2}) == "AUTH_FAILED"
    assert peer_link("incompatible", {}) == "INCOMPATIBLE"
    assert peer_link("lost", {"lost_s": 3.0}) == "RECONNECTING"           # связь только что была
    assert peer_link("lost", {"lost_s": 45.0}) == "DISCONNECTED"          # потеряна давно
    assert peer_link("connecting", {}) == "DISCONNECTED"                  # ещё не было


def test_console_sends_ready_then_watchdog(tmp_path):
    d = tempfile.mkdtemp(prefix="sd", dir="/tmp")
    path = os.path.join(d, "n")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(path)
    s.settimeout(30)
    env = {**os.environ, "NOTIFY_SOCKET": path, "WATCHDOG_USEC": "2000000", "QT_QPA_PLATFORM": "offscreen",
           "QT_QUICK_BACKEND": "software",
           "PYTHONPATH": os.pathsep.join(str(ROOT / p) for p in ("software", "software/emulators", "software/console"))}
    p = subprocess.Popen([sys.executable, "-m", "sheregesh_console", "--data-dir", str(tmp_path),
                          "--link-config", str(tmp_path / "absent.json")], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        t0 = time.monotonic()
        first = s.recv(256).decode()
        assert first.startswith("READY=1"), first
        assert s.recv(256).decode() == "WATCHDOG=1"
        assert time.monotonic() - t0 < 30
    finally:
        p.terminate()
        p.wait(10)
        s.close()
        shutil.rmtree(d)
