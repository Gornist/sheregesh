#!/bin/sh
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# Фиктивный cage: лидер группы процессов (ребёнок ретранслятора), «приложение» fake_app.py — его ребёнок.
#   term — обычный; ignore — cage и приложение игнорируют SIGTERM; frozen — как ignore, приложение заморозит run.sh;
#   hang — приложение перестаёт слать WATCHDOG; crash — cage падает с кодом 70 через 4 с.
mode=$1
case $mode in ignore|frozen) trap '' TERM ;; esac
/usr/bin/python3 -s /usr/local/lib/appstop/fake_app.py "$mode" &
echo "$$ $!" > "/run/appstop/$mode.pids"
[ "$mode" = crash ] && { sleep 4; exit 70; }
while :; do sleep 1; done
