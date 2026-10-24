# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Испытательные юниты из настоящего tools/image/units/sheregesh-console.service.

Сохраняются Type, NotifyAccess, WatchdogSec, TimeoutStartSec, TimeoutStopSec, Restart*, StartLimit*, RuntimeDirectory,
Environment, ExecStopPost, NoNewPrivileges, IPAddress*. ExecStart: тот же ретранслятор, вместо cage — fake_cage.sh %i.
  a@ — без User/PAMName (процессы в cgroup службы);
  b@ — User=sheregesh-ui, PAMName=login, как на плате (процессы уходят в session-N.scope).
Убраны зависимости от железа и платы: TTYPath, Conflicts=getty@tty1, After/Wants, RequiresMountsFor, Condition*.
"""
import sys
from pathlib import Path

src, out = Path(sys.argv[1]), Path(sys.argv[2])
DROP_ALL = ("TTYPath=", "Conflicts=", "After=", "Wants=", "RequiresMountsFor=", "ConditionPathExists=", "[Install]",
            "WantedBy=")
DROP_A = ("User=", "PAMName=")
for variant, drop in (("a", DROP_ALL + DROP_A), ("b", DROP_ALL)):
    lines = []
    for line in src.read_text().splitlines():
        if line.startswith("#") or line.startswith(drop):
            continue
        if line.startswith("ExecStart="):
            head, _, _ = line.partition(" -- /usr/bin/cage")
            assert head == "ExecStart=/usr/bin/sheregesh-notify-relay", line
            line = head + " -- /usr/local/lib/appstop/fake_cage.sh %i"
        if line.startswith("Description="):
            line += f" — испытание app-stop ({variant})"
        lines.append(line)
    (out / f"sheregesh-console-{variant}@.service").write_text("\n".join(lines) + "\n")
