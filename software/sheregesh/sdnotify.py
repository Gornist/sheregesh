# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Протокол уведомлений systemd (sd_notify) без внешних библиотек (ADR-0002: Type=notify, WatchdogSec).

READY=1 отправляется после фактической инициализации приложения, WATCHDOG=1 — из основного цикла.
Вне systemd (нет NOTIFY_SOCKET) все вызовы ничего не делают.
"""
import os
import socket


def notify(*lines: str) -> bool:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]                         # абстрактное пространство имён
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | getattr(socket, "SOCK_CLOEXEC", 0)) as s:
            s.connect(addr)
            s.sendall("\n".join(lines).encode())
        return True
    except OSError:
        return False


def watchdog_interval() -> float | None:
    """Период пинга (половина WatchdogSec) или None, если watchdog не задан для этого процесса."""
    usec = os.environ.get("WATCHDOG_USEC")
    pid = os.environ.get("WATCHDOG_PID")
    if not usec or (pid and pid.isdigit() and int(pid) != os.getpid()):
        return None
    try:
        return max(0.5, int(usec) / 1e6 / 2)
    except ValueError:
        return None


def ready(status: str = "") -> bool:
    return notify("READY=1", *([f"STATUS={status}"] if status else []))


def ping() -> bool:
    return notify("WATCHDOG=1")


def stopping(status: str = "") -> bool:
    return notify("STOPPING=1", *([f"STATUS={status}"] if status else []))
