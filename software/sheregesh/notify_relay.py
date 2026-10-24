# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ретранслятор sd_notify для пульта под cage (S3.1).

Служба пульта открывает сеанс входа (PAMName=login), и pam_systemd переносит процессы в session-N.scope — вне cgroup
службы. systemd принимает уведомления от процессов вне cgroup только от главного PID, поэтому READY=1 и WATCHDOG=1
приложения (дочернего процесса cage) отбрасывались, и служба не выходила из activating (таймаут старта).

Ретранслятор — главный PID службы: создаёт свой сокет уведомлений в каталоге выполнения, передаёт его детям через
NOTIFY_SOCKET (WATCHDOG_PID снимается — пингует приложение) и пересылает systemd разрешённые строки от своего имени.
MAINPID, FDSTORE и прочие строки не пересылаются. Код выхода — код ребёнка.

Останов. Процессы сеанса не входят в cgroup службы, и systemd при останове или срабатывании watchdog убивает только
главный PID: старые cage и зависшее приложение оставались, держали DRM и tty1, и новые запуски падали до лимита
перезапусков (pi1, ee56bf1). Поэтому ребёнок запускается в своей группе процессов; по SIGTERM/SIGINT и по SIGABRT
(сигнал watchdog) группа получает SIGTERM, через GRACE_S — SIGKILL, а после выхода ребёнка добиваются оставшиеся
в группе. При гибели самого ретранслятора cage получает SIGKILL (PR_SET_PDEATHSIG), а приложение под cage по
переменной SHEREGESH_PDEATHSIG само просит SIGKILL при гибели cage (adopt_pdeathsig) — цепочка не оставляет процессов,
даже если приложение заморожено.

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
import time

from sheregesh import sdnotify

ALLOWED = ("READY=1", "WATCHDOG=1", "STOPPING=1", "RELOADING=1", "STATUS=", "ERRNO=", "EXTEND_TIMEOUT_USEC=")
PR_SET_PDEATHSIG = 1
GRACE_S = float(os.environ.get("SHEREGESH_RELAY_GRACE_S", "5"))
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGABRT)


def filter_lines(data: bytes) -> list[str]:
    out = []
    for line in data.decode("utf-8", "replace").splitlines():
        if line.startswith(ALLOWED) and len(line) <= 1024:
            out.append(line)
    return out


def _pdeathsig():
    try:
        ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    except (OSError, AttributeError):
        pass                                           # не Linux: только останов группы по сигналу


def adopt_pdeathsig():
    """Для процесса под ретранслятором (приложение — ребёнок cage): SIGKILL при гибели родителя. Вне Linux — ничего."""
    if os.environ.get("SHEREGESH_PDEATHSIG") != "1" or not sys.platform.startswith("linux"):
        return False
    _pdeathsig()
    if os.getppid() == 1:                              # родитель умер до prctl
        os._exit(1)
    return True


def _killpg(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


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
    env["SHEREGESH_PDEATHSIG"] = "1"                   # приложение под cage: SIGKILL при гибели cage
    sock = path = tmpdir = None
    if upstream:
        base = os.environ.get("RUNTIME_DIRECTORY", "").split(":")[0] or None
        tmpdir = tempfile.mkdtemp(prefix="notify-", dir=base)
        path = os.path.join(tmpdir, "sock")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | getattr(socket, "SOCK_CLOEXEC", 0))
        sock.bind(path)
        os.chmod(path, 0o600)
        env["NOTIFY_SOCKET"] = path
    child = subprocess.Popen(argv, env=env, process_group=0,
                             preexec_fn=_pdeathsig if sys.platform.startswith("linux") else None)
    stop = {"sig": None, "at": 0.0, "killed": False}

    def on_signal(signum, _frame):
        if signum in STOP_SIGNALS:
            if stop["sig"] is None:
                stop["sig"], stop["at"] = signum, time.monotonic()
                print(f"notify-relay: signal {signal.Signals(signum).name} — stopping process group {child.pid}",
                      file=sys.stderr, flush=True)
                _killpg(child.pid, signal.SIGTERM)
        else:
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass
    for s in (*STOP_SIGNALS, signal.SIGHUP):
        signal.signal(s, on_signal)
    try:
        while child.poll() is None:
            if stop["sig"] is not None and not stop["killed"] and time.monotonic() - stop["at"] > GRACE_S:
                print(f"notify-relay: group {child.pid} still alive after {GRACE_S:g} s — SIGKILL", file=sys.stderr,
                      flush=True)
                _killpg(child.pid, signal.SIGKILL)
                stop["killed"] = True
            if sock is None:
                try:
                    child.wait(0.5)
                except subprocess.TimeoutExpired:
                    pass
                continue
            try:
                ready, _, _ = select.select([sock], [], [], 0.5)
            except InterruptedError:
                continue
            if ready:
                lines = filter_lines(sock.recv(4096))
                if lines:
                    sdnotify.notify(*lines)
        _killpg(child.pid, signal.SIGKILL)             # оставшиеся в группе (например, зависшее приложение)
        if stop["sig"] in (signal.SIGTERM, signal.SIGINT):
            return 0                                   # штатный останов (systemctl stop): не «failed» (pi2, 57e8fe4)
        if stop["sig"] is not None:
            return 128 + stop["sig"]                   # SIGABRT — watchdog: отказ
        return child.returncode if child.returncode >= 0 else 128 - child.returncode
    finally:
        if sock is not None:
            sock.close()
            os.unlink(path)
            os.rmdir(tmpdir)


if __name__ == "__main__":
    sys.exit(main())
