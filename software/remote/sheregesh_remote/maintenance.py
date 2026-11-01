# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Обслуживание блока по команде пульта (протокол 1.6, sheregesh.link.remotecmd): выгрузка журнала и выключение.

Журнал: в сетевом профиле — journalctl служб «Шерегеша» за текущую загрузку (пользователь sheregesh-ui в группе adm
читает системный журнал), в эмуляторе — последние строки журнала самого агента (RING). Секреты заменяются,
размер ограничен remotecmd.MAX_LOG. Выключение: сетевой профиль — «start» юнита sheregesh-remote-poweroff.service
(он выключает плату через remotecmd.SHUTDOWN_DELAY_S, правило polkit 54-sheregesh-remote-power.rules), эмулятор —
SIGTERM своему процессу через то же время. Режим разработчика: sheregesh-ota-client dev-arm (агент OTA принимает его
только от службы устройства), в эмуляторе агента OTA нет — error. Одна команда за раз: вторая во время первой — busy."""
import asyncio
import collections
import hashlib
import json
import os
import signal
import subprocess
import time

from sheregesh.link import remotecmd
from sheregesh.redact import redact

JOURNAL_CMD = ["journalctl", "-b", "--no-pager", "-o", "short-iso", "-u", "sheregesh-*", "-n", "20000"]
JOURNAL_TIMEOUT_S = 20.0
POWEROFF_UNIT = "sheregesh-remote-poweroff.service"
OTA_DEV_ARM = ["sheregesh-ota-client", "dev-arm"]
RING = collections.deque(maxlen=4000)           # журнал агента (agent.log) — для эмулятора без journald


def remember(line: str) -> None:
    RING.append(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}")


def journal(network: bool, run=subprocess.run) -> bytes:
    """Текст журнала блока до фильтра и ограничения; OSError/RuntimeError — журнал недоступен."""
    if not network:
        return ("\n".join(RING) + "\n").encode("utf-8")
    try:
        r = run(JOURNAL_CMD, capture_output=True, timeout=JOURNAL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"journalctl: no answer in {JOURNAL_TIMEOUT_S:g} s") from None
    if r.returncode != 0:
        raise RuntimeError(f"journalctl exit {r.returncode}: {r.stderr.decode('utf-8', 'replace').strip()[:160]}")
    return r.stdout


def export_payload(raw: bytes, role_name: str = "remote") -> tuple[str, bytes]:
    """(имя файла, содержимое): секреты заменены, последние MAX_LOG байт."""
    text = redact(raw.decode("utf-8", "replace"))
    name = time.strftime(f"sheregesh-{role_name}-log-%Y%m%d-%H%M%S.txt")
    return name, remotecmd.limit(text.encode("utf-8"))


def poweroff(network: bool, run=subprocess.run) -> str:
    """Запустить выключение; "" — принято, иначе текст ошибки (плата не выключится)."""
    if not network:
        asyncio.get_running_loop().call_later(remotecmd.SHUTDOWN_DELAY_S, os.kill, os.getpid(), signal.SIGTERM)
        return ""
    try:
        r = run(["systemctl", "start", "--no-block", POWEROFF_UNIT], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"systemctl: {e}"
    return "" if r.returncode == 0 else f"systemctl exit {r.returncode}: {r.stderr.decode('utf-8', 'replace').strip()[:160]}"


def dev_arm(network: bool, run=subprocess.run) -> tuple[bool, str]:
    """(разрешено, причина агента OTA)."""
    if not network:
        return False, "no OTA agent in the emulator"
    try:
        r = run(OTA_DEV_ARM, capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"sheregesh-ota-client: {e}"
    lines = [x for x in r.stdout.decode("utf-8", "replace").splitlines() if x.strip()]
    try:
        res = json.loads(lines[-1]) if lines else {}
    except ValueError:
        res = {}
    if not isinstance(res, dict):
        res = {}
    reason = str(res.get("reason") or r.stderr.decode("utf-8", "replace").strip() or f"exit {r.returncode}")[:200]
    return res.get("result") == "OK", reason


class Maintenance:
    """send(тип, тело) — отправка текущему сеансу (Agent.send с writer сеанса), log(text) — журнал агента."""

    def __init__(self, network: bool, log, run=subprocess.run):
        self.network = network
        self.log = log
        self.run = run
        self.busy = False

    async def handle(self, body, send) -> None:
        try:
            cmd = remotecmd.check_command(body)
        except remotecmd.BadCommand as e:
            req = body.get("req") if isinstance(body, dict) and isinstance(body.get("req"), int) else 0
            self.log(f"remote-cmd rejected: {e.text}")
            await send("remote-cmd-result", {"req": req, "op": str(body.get("op", ""))[:32] if isinstance(body, dict)
                                             else "", "result": "bad-request", "error": e.text})
            return
        req, op = cmd["req"], cmd["op"]
        if self.busy:
            await send("remote-cmd-result", {"req": req, "op": op, "result": "busy", "error": "another command runs"})
            return
        self.busy = True
        try:
            if op == "log-export":
                await self._export(req, send)
            elif op == "dev-arm":
                await self._dev_arm(req, send)
            else:
                await self._shutdown(req, send)
        finally:
            self.busy = False

    async def _export(self, req, send):
        try:
            raw = await asyncio.to_thread(journal, self.network, self.run)
        except (OSError, RuntimeError) as e:
            self.log(f"remote-cmd {req} log-export failed: {e}")
            await send("remote-cmd-result", {"req": req, "op": "log-export", "result": "error", "error": str(e)[:200]})
            return
        name, data = export_payload(raw)
        parts = remotecmd.split(data)
        for i, chunk in enumerate(parts):
            await send("remote-log", {"req": req, "part": i, "parts": len(parts), "data": chunk})
        await send("remote-cmd-result", {"req": req, "op": "log-export", "result": "ok", "error": "", "name": name,
                                         "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                         "parts": len(parts)})
        self.log(f"remote-cmd {req} log-export: {name}, {len(data)} bytes, {len(parts)} parts")

    async def _dev_arm(self, req, send):
        self.log(f"remote-cmd {req} dev-arm requested by console")
        ok, reason = await asyncio.to_thread(dev_arm, self.network, self.run)
        self.log(f"remote-cmd {req} dev-arm: {'armed' if ok else 'refused'}: {reason}")
        await send("remote-cmd-result", {"req": req, "op": "dev-arm", "result": "ok" if ok else "error",
                                         "error": reason})

    async def _shutdown(self, req, send):
        self.log(f"remote-cmd {req} shutdown requested by console")
        err = await asyncio.to_thread(poweroff, self.network, self.run) if self.network else poweroff(False)
        if err:
            self.log(f"remote-cmd {req} shutdown failed: {err}")
            await send("remote-cmd-result", {"req": req, "op": "shutdown", "result": "error", "error": err[:200]})
            return
        await send("remote-cmd-result", {"req": req, "op": "shutdown", "result": "ok", "error": ""})
        self.log(f"remote-cmd {req} shutdown: powering off in {remotecmd.SHUTDOWN_DELAY_S:g} s")
