# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Лабораторный эмулятор внешнего модуля формата JR (ADR-0012, разделы 6.4 и 7).

Моделирует только состояние модуля: присутствие, инициализацию с идентичностью, ответы на опрос, ошибку интерфейса,
зависание и аварию процесса. Радиосигналов, каналов, значений органов управления и любого выхода не формирует;
интерфейсов, устройств и сети не открывает (D6, ADR-0012 раздел 12.3).

Ядро JrModuleEmulator — чистая модель на инъецированных часах (SimClock): системные часы не читаются, одинаковый
сценарий даёт одинаковые ответы. Дочерний процесс агента удалённого блока (main) лишь продвигает свой SimClock
реальным прошедшим временем и обменивается JSON-строками по stdin/stdout:

  python3 -s -m sheregesh_emulators.jrmodule --profile radiomaster-nomad [--speed 1] [--refuse-start]

Первая строка — hello; по EOF stdin — выход 0, сценарий crash — 70, --refuse-start — 69 (до hello).
"""
import argparse
import json
import os
import select
import sys
import time

from sheregesh.module import wire
from sheregesh.module.states import State

from .devices import SimClock

MAX_LINE = 4096                    # байт на сообщение канала (раздел 7.2)
SLOW_INIT_S = 8.0                  # slow-init: следующая инициализация дольше init_timeout_s профиля (5 с)
EXIT_CRASH = 70
EXIT_REFUSED = 69
IFACE_TEXT = "interface error reported by module"
LAB_SCENARIOS = wire.LAB_SCENARIOS
PARENT_SCENARIOS = ("emu-unavailable", "emu-available")      # исполняет родитель (LabEmulatorProcessBackend)

IDENTITIES = {
    "radiomaster-nomad": {"vendor": "RadioMaster", "model": "Nomad", "firmware": "emu-1.0", "serial": "EMU-NOMAD-0001"},
}


class JrModuleEmulator:
    """Модель модуля: сообщения канала → ответы (handle), самопроизвольные сообщения и созревшие ответы — drain()."""

    def __init__(self, clock, profile_id="radiomaster-nomad", identity=None, init_s=1.0, present=True):
        self.clock = clock
        self.profile_id = profile_id
        self.identity = dict(identity or IDENTITIES.get(profile_id) or
                             {"vendor": "Unknown", "model": profile_id[:32], "firmware": "emu-1.0", "serial": "EMU-0000"})
        self.init_s = float(init_s)
        self.initially_present = bool(present)
        self.restart()

    def restart(self):
        """Новый процесс: лабораторные флаги сброшены, модуль вставлен (как после перезапуска службы)."""
        self.present = self.initially_present
        self.iface_error = False
        self.hang = False
        self.slow_next = False
        self.crashed = False
        self._pending = []           # (срок, id, профиль) ответов init
        self._out = []

    # ---------- канал ----------
    def handle(self, msg: dict) -> list:
        """Сообщение родителя → немедленные ответы (раздел 7.2). Неизвестное — без ответа."""
        op = msg.get("op") if isinstance(msg, dict) else None
        mid = msg.get("id") if isinstance(msg, dict) and isinstance(msg.get("id"), int) else 0
        if op == "keepalive":
            return [{"ev": "alive"}]
        if op == "probe":                         # обнаружение — физический признак: отвечает при любом сценарии
            return [{"ev": "presence", "present": self.present}]
        if op == "init":
            if self.present and not self.hang:
                due = self.clock() + (SLOW_INIT_S if self.slow_next else self.init_s)
                self.slow_next = False
                self._pending.append((due, mid, msg.get("profile")))
            return []
        if op == "ping":
            if not self.present or self.hang:
                return []
            if self.iface_error:
                return [{"ev": "fault", "code": "MOD-IFACE-ERROR", "text": IFACE_TEXT}]
            return [{"ev": "pong", "id": mid}]
        if op == "lab":
            scenario = msg.get("scenario")
            ok = self.lab(scenario)
            error = "" if ok else ("scenario is handled by the parent process" if scenario in PARENT_SCENARIOS
                                   else "unknown scenario")
            return [{"ev": "lab", "scenario": scenario if scenario in LAB_SCENARIOS else "", "ok": ok, "error": error}]
        return []

    def step(self, dt: float = 0.0) -> None:
        """Созревшие ответы init — в очередь drain(). Время — только из часов (dt оставлен для единообразия с
        эмуляторами devices: часы продвигает владелец)."""
        now = self.clock()
        due = [p for p in self._pending if p[0] <= now]
        self._pending = [p for p in self._pending if p[0] > now]
        for _, mid, profile in due:
            if not self.present or self.hang:
                continue
            if self.iface_error:
                self._out.append({"ev": "init", "id": mid, "ok": False, "code": "MOD-IFACE-ERROR", "text": IFACE_TEXT})
            elif profile != self.profile_id:
                self._out.append({"ev": "init", "id": mid, "ok": False, "code": "MOD-INIT-FAILED",
                                  "text": f"module does not support profile {str(profile)[:32]!r}"})
            else:
                self._out.append({"ev": "init", "id": mid, "ok": True, "identity": dict(self.identity)})

    def drain(self) -> list:
        """Самопроизвольные сообщения (presence при attach/detach) и созревшие ответы init."""
        self.step(0.0)
        out, self._out = self._out, []
        return out

    def lab(self, scenario: str) -> bool:
        """Лабораторный сценарий (раздел 6.4). emu-* — дело родителя: False."""
        if scenario == "attach":
            self.present = True
            self._out.append({"ev": "presence", "present": True})
        elif scenario == "detach":
            self.present = False
            self._pending.clear()
            self._out.append({"ev": "presence", "present": False})
        elif scenario == "iface-error":
            self.iface_error = True
        elif scenario == "hang":
            self.hang = True
            self._pending.clear()
        elif scenario == "heal":
            self.iface_error = self.hang = self.slow_next = False
        elif scenario == "slow-init":
            self.slow_next = True
        elif scenario == "crash":
            self.crashed = True
        else:
            return False
        return True


# Общий сценарий unit (InlineEmulatorBackend + SimClock) и L3: (t, сценарий, состояние) — в момент t модельного
# времени от ModuleManager.start() подаётся сценарий ("" — ничего); состояние должно наблюдаться (текущее или переход
# в него) в промежутке [t, t следующей строки). Последняя строка — до t + 10 с. Таймауты — по умолчанию (Timings,
# профиль Nomad), init_s эмулятора — 1 с, auto_init и auto_recover включены.
SCRIPT_FULL = [
    (0.0, "", State.READY),                  # старт → DISCONNECTED → DETECTED → INITIALIZING → READY
    (5.0, "detach", State.DISCONNECTED),
    (8.0, "attach", State.READY),
    (12.0, "iface-error", State.FAULT),      # FAULT MOD-IFACE-ERROR; восстановление по backoff, снова FAULT
    (16.0, "heal", State.READY),
    (26.0, "hang", State.FAULT),             # первый FAULT — MOD-NO-RESPONSE, повтор — MOD-INIT-TIMEOUT
    (32.0, "heal", State.READY),
    (44.0, "slow-init", State.READY),        # действует на следующую инициализацию
    (46.0, "detach", State.DISCONNECTED),
    (48.0, "attach", State.FAULT),           # инициализация 8 с > 5 с → MOD-INIT-TIMEOUT
    (55.0, "", State.READY),
    (60.0, "crash", State.FAULT),            # EMU-EXITED → перезапуск процесса (бюджет 1/3) → READY
    (62.0, "", State.READY),
]


# ---------- дочерний процесс ----------
def _log(text):
    print(f"[jrmodule] {text}", file=sys.stderr, flush=True)


def _write(msg):
    data = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(data) > MAX_LINE:
        raise ValueError("message exceeds 4096 bytes")
    view = memoryview(data)
    while view:
        n = os.write(1, view)
        view = view[n:]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m sheregesh_emulators.jrmodule", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="radiomaster-nomad", choices=sorted(IDENTITIES))
    ap.add_argument("--speed", type=float, default=1.0, help="ускорение модельного времени")
    ap.add_argument("--refuse-start", action="store_true", help="отказ запуска (лабораторный сценарий emu-unavailable)")
    a = ap.parse_args(argv)
    if a.refuse_start:
        _log(f"start refused (lab scenario emu-unavailable): exit {EXIT_REFUSED}")
        return EXIT_REFUSED
    clock = SimClock()
    emu = JrModuleEmulator(clock, a.profile)
    fd = sys.stdin.fileno()
    buf = b""
    last = time.monotonic()
    try:
        _write({"ev": "hello", "emulator": "jrmodule-lab", "version": 1, "profile": a.profile, "pid": os.getpid()})
        _log(f"started pid={os.getpid()} profile={a.profile} (laboratory emulator, no radio output)")
        while True:
            ready, _, _ = select.select([fd], [], [], 0.05)
            now = time.monotonic()
            clock.advance((now - last) * a.speed)
            last = now
            if ready:
                data = os.read(fd, 65536)
                if not data:
                    return 0                      # EOF stdin: родитель закрыл канал
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        _log("bad line from parent ignored")
                        continue
                    for m in emu.handle(msg):
                        _write(m)
                    if isinstance(msg, dict) and msg.get("op") == "lab":
                        _log(f"lab {str(msg.get('scenario'))[:32]}")
                if len(buf) > MAX_LINE:
                    _log("line from parent exceeds 4096 bytes: dropped")
                    buf = b""
            for m in emu.drain():
                _write(m)
            if emu.crashed:
                _log(f"crash (lab scenario): exit {EXIT_CRASH}")
                os._exit(EXIT_CRASH)
    except (BrokenPipeError, KeyboardInterrupt):
        return 0


if __name__ == "__main__":
    sys.exit(main())
