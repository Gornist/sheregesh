# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ретранслятор sd_notify для пульта под cage (S3.1).

Служба пульта открывает сеанс входа (PAMName=login), и pam_systemd переносит процессы в session-N.scope — вне cgroup
службы. systemd принимает уведомления от процессов вне cgroup только от главного PID, поэтому READY=1 и WATCHDOG=1
приложения (дочернего процесса cage) отбрасывались, и служба не выходила из activating (таймаут старта).

Ретранслятор — главный PID службы: создаёт свой сокет уведомлений в каталоге выполнения, передаёт его детям через
NOTIFY_SOCKET (WATCHDOG_PID снимается — пингует приложение) и пересылает systemd разрешённые строки от своего имени.
MAINPID, FDSTORE и прочие строки не пересылаются. Сигналы останова передаются ребёнку; при гибели ретранслятора
ребёнок получает SIGTERM (PR_SET_PDEATHSIG). Код выхода — код ребёнка.

  sheregesh-notify-relay -- /usr/bin/cage -s -- /usr/bin/sheregesh-console ...
"""
import ctypes
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile

from sheregesh import sdnotify

ALLOWED = ("READY=1", "WATCHDOG=1", "STOPPING=1", "RELOADING=1", "STATUS=", "ERRNO=", "EXTEND_TIMEOUT_USEC=")
PR_SET_PDEATHSIG = 1


def filter_lines(data: bytes) -> list[str]:
    out = []
    for line in data.decode("utf-8", "replace").splitlines():
        if line.startswith(ALLOWED) and len(line) <= 1024:
            out.append(line)
    return out


def _pdeathsig():
    try:
        ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except (OSError, AttributeError):
        pass                                           # не Linux: только пересылка сигналов


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        print("usage: sheregesh-notify-relay -- COMMAND [ARGS...]", file=sys.stderr)
        return 2
    upstream = os.environ.get("NOTIFY_SOCKET")
    env = dict(os.environ)
    env.pop("WATCHDOG_PID", None)                      # пингует приложение, а не ретранслятор
    sock = path = tmpdir = None
    if upstream:
        base = os.environ.get("RUNTIME_DIRECTORY", "").split(":")[0] or None
        tmpdir = tempfile.mkdtemp(prefix="notify-", dir=base)
        path = os.path.join(tmpdir, "sock")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | getattr(socket, "SOCK_CLOEXEC", 0))
        sock.bind(path)
        os.chmod(path, 0o600)
        env["NOTIFY_SOCKET"] = path
    child = subprocess.Popen(argv, env=env, preexec_fn=_pdeathsig if sys.platform.startswith("linux") else None)

    def forward(signum, _frame):
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, forward)
    try:
        while child.poll() is None:
            if sock is None:
                try:
                    child.wait(1.0)
                except subprocess.TimeoutExpired:
                    pass
                continue
            try:
                ready, _, _ = select.select([sock], [], [], 1.0)
            except InterruptedError:
                continue
            if ready:
                lines = filter_lines(sock.recv(4096))
                if lines:
                    sdnotify.notify(*lines)
        return child.returncode if child.returncode >= 0 else 128 - child.returncode
    finally:
        if sock is not None:
            sock.close()
            os.unlink(path)
            os.rmdir(tmpdir)


if __name__ == "__main__":
    sys.exit(main())
