# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Фиктивное приложение пульта: как sheregesh_console — adopt_pdeathsig, READY и WATCHDOG через sheregesh.sdnotify."""
import signal
import sys
import time

sys.path.insert(0, "/usr/lib/sheregesh/software")
from sheregesh import sdnotify                           # noqa: E402
from sheregesh.notify_relay import adopt_pdeathsig       # noqa: E402

mode = sys.argv[1]
adopt_pdeathsig()
if mode in ("ignore", "frozen"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
period = sdnotify.watchdog_interval() or 2.0
print(f"fake-app: mode={mode} watchdog period {period:g} s", flush=True)
sdnotify.ready(f"fake app {mode}")
while True:
    if mode != "hang":
        sdnotify.notify("WATCHDOG=1")
    time.sleep(period)
