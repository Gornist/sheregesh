# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Лабораторные бэкенды внешнего модуля (ADR-0012, разделы 5 и 7).

LabEmulatorProcessBackend — эмулятор модуля дочерним процессом агента (sheregesh_emulators.jrmodule) через
sheregesh.video.spawn: умирает вместе с агентом (pdeathsig), своя группа процессов, SIGHUP игнорируется. Канал —
JSON-строки ≤ 4096 байт по stdin/stdout (раздел 7.2); stderr наследуется (журнал службы, префикс [jrmodule]).
Ничего не блокирует цикл агента: stdout читает поток-демон в очередь, stdin неблокирующий, остановка досылается
в poll(); единственная блокирующая операция — close() при выходе агента, не дольше stop_timeout_s.

InlineEmulatorBackend — тот же контракт поверх JrModuleEmulator в процессе (детерминированные тесты на SimClock).
"""
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

from .backend import BackendEvent

MAX_LINE = 4096
EMULATOR_MODULE = "sheregesh_emulators.jrmodule"
PARENT_SCENARIOS = ("emu-unavailable", "emu-available")


def default_command(profile_id):
    """Эмулятор из того же выпуска, что и агент: -s, как у launcher (-I отключил бы PYTHONPATH)."""
    return [sys.executable, "-s", "-m", EMULATOR_MODULE, "--profile", profile_id]


def child_env():
    """PYTHONPATH дочернего процесса — каталоги уже загруженных пакетов (выпуск DEV deploy или /usr/lib/sheregesh)."""
    import sheregesh
    paths = [os.path.dirname(os.path.dirname(os.path.abspath(sheregesh.__file__)))]
    try:
        import sheregesh_emulators
        paths.append(os.path.dirname(os.path.dirname(os.path.abspath(sheregesh_emulators.__file__))))
    except ImportError:
        pass
    env = dict(os.environ)
    old = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([*paths, *([old] if old else [])]))
    return env


def exit_code(rc):
    """Код выхода процесса; отрицательный — номер сигнала. Сторож pdeath.py (macOS) сообщает сигнал как 128 + N."""
    if rc is None:
        return None
    return -(rc - 128) if rc > 128 else rc


def event_of(msg):
    """Сообщение эмулятора → BackendEvent или None (alive, hello и неизвестные события)."""
    ev = msg.get("ev")

    def text(k, n=200):
        return msg[k][:n] if isinstance(msg.get(k), str) else ""
    mid = msg.get("id") if isinstance(msg.get("id"), int) and not isinstance(msg.get("id"), bool) else 0
    if ev == "presence":
        return BackendEvent("presence", {"present": msg.get("present") is True})
    if ev == "init":
        ident = msg.get("identity") if isinstance(msg.get("identity"), dict) else {}
        return BackendEvent("init", {"id": mid, "ok": msg.get("ok") is True,
                                     "identity": {k: str(v)[:32] for k, v in list(ident.items())[:8]},
                                     "code": text("code", 32), "text": text("text")})
    if ev == "pong":
        return BackendEvent("pong", {"id": mid})
    if ev == "fault":
        return BackendEvent("fault", {"code": text("code", 32), "text": text("text")})
    if ev == "lab":
        return BackendEvent("lab", {"scenario": text("scenario", 32), "ok": msg.get("ok") is True,
                                    "error": text("error")})
    return None


def _killpg(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _reader(stream, q):
    """Поток-демон jrmodule-reader: строки stdout эмулятора → очередь; слишком длинная строка — too-long."""
    try:
        while True:
            line = stream.readline(MAX_LINE + 2)
            if not line:
                break
            if not line.endswith(b"\n"):
                if len(line) > MAX_LINE:
                    q.put(("too-long", None))
                    break
                continue                                  # неполная строка перед EOF
            line = line[:-1]
            if len(line) > MAX_LINE:
                q.put(("too-long", None))
                break
            q.put(("line", line))
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass
        q.put(("eof", None))


class LabEmulatorProcessBackend:
    kind = "lab-emulator"
    supports_lab = True

    def __init__(self, profile_id, *, clock, log, timings, command=None):
        self.profile_id = profile_id
        self.clock, self.log, self.timings = clock, log, timings
        self.command = list(command) if command else default_command(profile_id)
        self.refuse = False                 # emu-unavailable: новые запуски отклоняются (--refuse-start, код 69)
        self.proc = None
        self.state = "stopped"
        self.pid = 0
        self.last_exit = None
        self.starts = 0
        self._events = []
        self._dying = []                    # [процесс, срок SIGTERM, срок SIGKILL, SIGTERM отправлен]
        self._q = None
        self._wbuf = bytearray()
        self._hello = False
        self._eof = False
        self._exit_seen = None
        self._started_at = self._last_rx = self._next_ka = 0.0

    # ---------- контракт ModuleBackend ----------
    def start(self):
        if self.proc is not None:            # не должно случаться: прежний процесс — немедленно
            self._retire(self.proc, kill=True)
            self.proc = None
        cmd = self.command + (["--refuse-start"] if self.refuse else [])
        from .. import video
        try:
            proc = video.spawn(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=child_env())
        except OSError as e:
            self.state, self.pid = "stopped", 0
            self.log(f"emulator start failed: {e}")
            self._events.append(BackendEvent("channel-down", {"reason": "start-failed", "code": None}))
            return
        os.set_blocking(proc.stdin.fileno(), False)
        self.proc = proc
        self.state = "restarting" if self.starts else "starting"
        self.starts += 1
        self.pid = 0
        self._hello, self._eof, self._exit_seen = False, False, None
        self._wbuf = bytearray()
        self._started_at = self._last_rx = self.clock()
        self._q = queue.SimpleQueue()
        threading.Thread(target=_reader, args=(proc.stdout, self._q), name="jrmodule-reader", daemon=True).start()
        if self.refuse:
            self.log("emulator start (lab scenario emu-unavailable: start will be refused)")

    def stop(self):
        if self.proc is not None:
            self.log(f"emulator stopping pid={self.pid or self.proc.pid}")
            self._retire(self.proc, kill=False)
        self.proc = None
        self.state, self.pid, self._hello = "stopped", 0, False
        self._events.clear()

    def poll(self):
        now = self.clock()
        self._reap(now)
        p = self.proc
        if p is not None:
            self._drain_queue(now)
        p = self.proc
        if p is not None:
            rc = p.poll()
            if rc is not None:
                self._exit_seen = self._exit_seen if self._exit_seen is not None else now
                if self._eof or now - self._exit_seen >= 1.0:       # дочитать ответы до EOF (crash: lab, затем выход)
                    self._drain_queue(now)
                    self._down("exited" if self._hello else "start-failed", exit_code(rc), reap=False)
            elif not self._hello and now - self._started_at > self.timings.hello_timeout_s:
                self._down("start-failed", None, why=f"no hello in {self.timings.hello_timeout_s:g} s")
            elif self._hello:
                if now - self._last_rx > self.timings.keepalive_timeout_s:
                    self._down("no-keepalive", None, why=f"no reply in {self.timings.keepalive_timeout_s:g} s")
                elif now >= self._next_ka:
                    self._next_ka = now + self.timings.keepalive_interval_s
                    self._write({"op": "keepalive"})
        if self.proc is not None and self._wbuf:
            self._flush()
        ev, self._events = self._events, []
        return ev

    def send(self, op, **args):
        if self.proc is None or not self._hello:
            return False
        return self._write({"op": op, **args})

    def lab_local(self, scenario):
        if scenario == "emu-unavailable":
            self.refuse = True
            if self.proc is not None:            # текущий процесс — SIGKILL; выход увидит poll() (EMU-EXITED)
                self.log(f"lab emu-unavailable: emulator pid={self.pid or self.proc.pid} killed, new starts refused")
                _killpg(self.proc, signal.SIGKILL)
            return True
        if scenario == "emu-available":
            self.refuse = False
            return True
        return False

    def info(self):
        return {"kind": self.kind, "lab": True, "state": self.state, "pid": self.pid, "restarts": 0,
                "restart_limit": self.timings.restart_limit, "last_exit": self.last_exit}

    def close(self):
        """Блокирующая остановка всех процессов не дольше stop_timeout_s (выход агента)."""
        procs = [d[0] for d in self._dying] + ([self.proc] if self.proc is not None else [])
        self._dying, self.proc = [], None
        self.state, self.pid = "stopped", 0
        for p in procs:
            try:
                p.stdin.close()
            except OSError:
                pass
        end = time.monotonic() + self.timings.stop_timeout_s
        for sig, until in ((signal.SIGTERM, end - self.timings.stop_timeout_s / 2), (signal.SIGKILL, end)):
            for p in procs:
                try:
                    p.wait(timeout=max(0.0, until - time.monotonic()))
                except subprocess.TimeoutExpired:
                    _killpg(p, sig)
        for p in procs:
            try:
                p.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass

    # ---------- внутреннее ----------
    def _drain_queue(self, now):
        while self.proc is not None:
            try:
                kind, line = self._q.get_nowait()
            except queue.Empty:
                return
            if kind == "eof":
                self._eof = True
                continue
            if kind == "too-long":
                self._down("protocol", None, why="line exceeds 4096 bytes")
                return
            try:
                msg = json.loads(line)
            except ValueError:
                msg = None
            if not isinstance(msg, dict) or not isinstance(msg.get("ev"), str):
                self._down("protocol", None, why="not a JSON event")
                return
            self._last_rx = now
            if not self._hello:
                if msg["ev"] != "hello":
                    self._down("protocol", None, why="first line is not hello")
                    return
                self._hello = True
                pid = msg.get("pid")
                self.pid = pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else self.proc.pid
                self.state = "running"
                self._next_ka = now + self.timings.keepalive_interval_s
                self.log(f"emulator started pid={self.pid}")
                self._events.append(BackendEvent("channel-up", {"pid": self.pid}))
                continue
            ev = event_of(msg)
            if ev is not None:
                self._events.append(ev)

    def _write(self, msg):
        data = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(data) > MAX_LINE:
            return False
        self._wbuf += data
        return self._flush()

    def _flush(self):
        p = self.proc
        try:
            while self._wbuf:
                n = os.write(p.stdin.fileno(), self._wbuf)
                del self._wbuf[:n]
        except BlockingIOError:              # процесс не читает вход (завис): канал потерян
            self._down("no-keepalive", None, why="emulator does not read its input")
            return False
        except (BrokenPipeError, OSError, ValueError):
            self._wbuf.clear()                # процесс завершается: итог — по коду выхода в poll()
            return False
        return True

    def _down(self, reason, code, why="", reap=True):
        """Канал потерян: процесс убит (если жив), событие channel-down, итог — в журнал."""
        p = self.proc
        if reap:                             # жив (таймаут, протокол): SIGKILL группе, досмотр в _reap
            self._retire(p, kill=True)
            code = -signal.SIGKILL if code is None else code
        else:
            try:
                p.stdin.close()
            except OSError:
                pass
        self.proc = None
        self.last_exit = code
        self.state, self.pid, self._hello = "stopped", 0, False
        self.log(f"emulator {'exited' if not reap else 'killed'} ({reason}{': ' + why if why else ''}) "
                 f"code={code}")
        self._events.append(BackendEvent("channel-down", {"reason": reason, "code": code}))

    def _retire(self, p, kill):
        try:
            p.stdin.close()
        except OSError:
            pass
        now = self.clock()
        if kill:
            _killpg(p, signal.SIGKILL)
        self._dying.append([p, now + self.timings.stop_timeout_s / 2, now + self.timings.stop_timeout_s, kill])

    def _reap(self, now):
        keep = []
        for d in self._dying:
            p, term_at, kill_at, termed = d
            if p.poll() is not None:
                continue
            if now >= kill_at:
                _killpg(p, signal.SIGKILL)
            elif now >= term_at and not termed:
                _killpg(p, signal.SIGTERM)
                d[3] = True
            keep.append(d)
        self._dying = keep


class InlineEmulatorBackend:
    """Контракт ModuleBackend поверх JrModuleEmulator в процессе: ответы доставляются следующим poll(), как из канала.

    crash → channel-down {"reason": "exited", "code": 70}; emu-unavailable → последующие start() дают start-failed
    (код 69); start_ok=False — запуск всегда неудачен; freeze() — процесс «завис»: нет ответов, через
    keepalive_timeout_s — channel-down no-keepalive."""
    kind = "lab-emulator"
    supports_lab = True

    def __init__(self, emulator, *, clock, timings, start_ok=True):
        self.emulator, self.clock, self.timings = emulator, clock, timings
        self.start_ok = start_ok
        self.refuse = False
        self.running = False
        self.frozen = False
        self.state = "stopped"
        self.pid = 0
        self.last_exit = None
        self.starts = 0
        self.sent = []                       # (op, args) — для проверок
        self._events = []
        self._last_alive = 0.0

    def start(self):
        self.starts += 1
        if not self.start_ok or self.refuse:
            self.running, self.state, self.pid = False, "stopped", 0
            self.last_exit = 69 if self.refuse else None
            self._events.append(BackendEvent("channel-down", {"reason": "start-failed", "code": self.last_exit}))
            return
        self.emulator.restart()
        self.running, self.frozen = True, False
        self.state, self.pid = "running", 1000 + self.starts
        self._last_alive = self.clock()
        self._events.append(BackendEvent("channel-up", {"pid": self.pid}))

    def stop(self):
        self.running, self.state, self.pid = False, "stopped", 0
        self._events.clear()

    def freeze(self):
        self.frozen = True

    def poll(self):
        if self.running and not self.frozen:
            self._last_alive = self.clock()
            self._take(self.emulator.drain())
            if self.emulator.crashed:
                self._exit(70, "exited")
        elif self.running and self.clock() - self._last_alive > self.timings.keepalive_timeout_s:
            self._exit(-signal.SIGKILL, "no-keepalive")
        ev, self._events = self._events, []
        return ev

    def send(self, op, **args):
        if not self.running:
            return False
        self.sent.append((op, dict(args)))
        if not self.frozen:
            self._take(self.emulator.handle({"op": op, **args}))
        return True

    def lab_local(self, scenario):
        if scenario == "emu-unavailable":
            self.refuse = True
            if self.running:
                self._exit(-signal.SIGKILL, "exited")
            return True
        if scenario == "emu-available":
            self.refuse = False
            return True
        return False

    def info(self):
        return {"kind": self.kind, "lab": True, "state": self.state, "pid": self.pid, "restarts": 0,
                "restart_limit": self.timings.restart_limit, "last_exit": self.last_exit}

    def close(self):
        self.stop()

    def _take(self, msgs):
        for m in msgs:
            ev = event_of(m)
            if ev is not None:
                self._events.append(ev)

    def _exit(self, code, reason):
        self.running, self.state, self.pid, self.last_exit = False, "stopped", 0, code
        self._events.append(BackendEvent("channel-down", {"reason": reason, "code": code}))
