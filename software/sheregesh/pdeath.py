# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Сторож: завершает дочерний процесс вместе с родителем там, где нет PR_SET_PDEATHSIG (macOS, BSD).

  python3 -I pdeath.py --parent PID -- команда [аргументы]

Запускает команду и раз в 0,1 с проверяет, жив ли родитель PID (сторож переподчиняется другому
процессу, если родитель умер). При смерти родителя — SIGKILL команде и выход. SIGTERM/SIGINT/SIGHUP
передаются команде. Код выхода — код команды (128 + N, если она завершена сигналом N).
Только стандартная библиотека: файл запускается по пути, без пакета sheregesh в sys.path.
"""
import os
import signal
import subprocess
import sys
import time

POLL_S = 0.1


def main(argv):
    if len(argv) < 4 or argv[0] != "--parent" or argv[2] != "--":
        print("usage: pdeath.py --parent PID -- command [args]", file=sys.stderr)
        return 2
    parent, cmd = int(argv[1]), argv[3:]
    child = subprocess.Popen(cmd)                  # stdin — как у сторожа (spawn: DEVNULL или канал декодера)
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, lambda n, _f: child.send_signal(n))
    while child.poll() is None:
        if os.getppid() != parent:
            child.kill()
            child.wait()
            return 128 + signal.SIGKILL
        time.sleep(POLL_S)
    return child.returncode if child.returncode >= 0 else 128 - child.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
