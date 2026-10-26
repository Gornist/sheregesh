# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Бэкенды внешнего модуля с выходом CRSF (ADR-0014, раздел 3.2): crsf-uart и crsf-pty-lab.

Канал к модулю — порт sheregesh.crsf (UartPort на UART3 либо PtyPort к эмулятору ELRS TX) и поток CrsfOutput:
кадры каналов по режиму выхода (source → none | neutral | live) и служебные кадры (ping). Жизненный цикл модуля
менеджер (ADR-0012) получает теми же событиями, что от лабораторного эмулятора, но по ответам модуля CRSF:
  presence  — на probe, как только от модуля есть корректные кадры (свежее no_response_s профиля);
  init      — device info 0x29 после ping 0x28 (identity из имени: профиль, если имя начинается с crsf_name);
              READY наступает по device info, а синхронизация 0x3A или link statistics 0x14 обязаны появиться в
              течение no_response_s после него: настоящий ELRS TX отвечает только в окне после кадра хоста, и
              требование синхронизации до READY (когда кадров каналов ещё нет) могло бы не выполниться никогда;
  pong      — на ping менеджера, пока ответы модуля свежие;
  fault     — тишина модуля дольше no_response_s (MOD-NO-RESPONSE), ошибки CRC подряд без корректных кадров
              (MOD-IFACE-ERROR), после READY нет синхронизации 0x3A и link statistics 0x14 (MOD-NO-RESPONSE).
Пока модуль не READY, кадров каналов нет — только ping (CrsfOutput — раз в 100 мс; бэкенд, пока ждёт presence или
device info либо модуль молчит, дополнительно просит ping раз в PING_S).

UartCrsfBackend: линия JR-отсека (uart.device: auto — find_uart3(), иначе путь); нет UART — ModuleError с кодом
MOD-IFACE-ERROR и текстом «UART не найден (uart-not-found)», агент не падает. Лабораторных команд нет.
PtyCrsfLabBackend: псевдотерминал (PtyPort) и эмулятор ELRS TX дочерним процессом (sheregesh_emulators.elrs_tx
--tty ведомая_сторона) через sheregesh.video.spawn, как jrmodule: своя группа процессов, умирает с агентом; его
stdout — JSON-строки (state: принятые каналы, взведение, частота, линия) → блок crsf.emu; лабораторные сценарии —
строкой {"op":"lab"} в stdin.
Перезапуски процесса ограничивает бюджет менеджера (канал вниз → RECOVERING → новый запуск).
"""
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

from .backend import BackendEvent, ModuleError
from .lab import _killpg, _reader, child_env, exit_code
from .states import MOD_IFACE_ERROR, MOD_NO_RESPONSE

PING_S = 0.5                       # ping модулю, пока он не READY (обнаружение, device info)
CRC_FAULT = 8                      # ошибок CRC подряд (crc_errors_run разборщика) → MOD-IFACE-ERROR
EMULATOR_MODULE = "sheregesh_emulators.elrs_tx"
UART_NOT_FOUND = "UART не найден (uart-not-found): нет устройства UART3 (serial@7e201600) — dtoverlay=uart3 в образе"
# Лабораторные сценарии module-cmd → сценарий эмулятора ELRS TX (ADR-0014, 3.1); emu-* и crash — у родителя.
# Сценарии jrmodule отображаются по смыслу: модуль пропал/завис — silent, ошибка интерфейса — crc, вернулся — heal.
LAB_MAP = {"silent": "silent", "garbage": "garbage", "crc": "crc", "heal": "heal", "hang": "silent",
           "detach": "silent", "attach": "heal", "iface-error": "crc"}
PARENT_SCENARIOS = ("emu-unavailable", "emu-available", "crash")
SNAP_KEYS = ("mode", "period_us", "synced", "sync_age_ms", "frames_sent", "jitter_us_p99", "crc_errors", "collisions",
             "link", "device", "last_rx_ms")


def _crsf():
    """Пакет протокола sheregesh.crsf — лениво: lab-emulator и пульт его не импортируют."""
    from ..crsf import output, port
    return port, output


class CrsfBackend:
    """Общее для crsf-uart и crsf-pty-lab: CrsfOutput поверх порта, события менеджеру по снимку выхода."""
    kind = "crsf"
    supports_lab = False

    def __init__(self, profile, settings, *, clock, log, timings, source, output_factory=None, port_factory=None):
        self.profile, self.settings = profile, settings
        self.clock, self.log, self.timings = clock, log, timings
        self.source = source
        self.output_factory = output_factory or self._default_output     # тесты: CrsfOutput на SimClock
        self.port_factory = port_factory or self._open_port               # тесты: LoopbackPort эмулятора
        self.out = None
        self.port = None
        self.port_name = ""
        self.state = "stopped"
        self.pid = 0
        self.last_exit = None
        self._events = []
        self._reset_link()

    def _reset_link(self):
        self._probe = False                 # менеджер ждёт presence
        self._init_id = None                # идёт инициализация (ждём device info)
        self._fresh = False                 # от модуля есть свежие корректные кадры
        self._silence_reported = False
        self._ready_at = None               # init ok отправлен: ждём синхронизацию или link statistics
        self._crc_reported = False
        self._next_ping = 0.0

    # ---------- порт и поток выхода ----------
    def _default_output(self, port):
        _, output = _crsf()
        return output.CrsfOutput(port, self.source, clock=time.monotonic, log=self.log)

    def _open_port(self):
        """→ (порт, имя для телеметрии). ModuleError — канала нет."""
        raise NotImplementedError

    def _start_output(self):
        port, self.port_name = self.port_factory()
        self.port = port
        try:
            self.out = self.output_factory(port)
            self.out.start()
        except Exception:
            self._close_output()
            raise

    def _close_output(self):
        out, port = self.out, self.port
        self.out = self.port = None
        if out is not None:
            try:
                out.stop()                  # поток выхода: не дольше одного периода (join ≤ 1 с)
            except Exception as e:  # noqa: BLE001 — остановка потока не должна ронять агента
                self.log(f"crsf output stop: {e!r}")
        if port is not None:
            try:
                port.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------- контракт ModuleBackend ----------
    def start(self):
        self._reset_link()
        self._start_output()                # ModuleError/OSError — менеджер: FAULT (код из ModuleError)
        self.state = "running"
        self.log(f"crsf: port {self.port_name} open, output thread started")
        self._events.append(BackendEvent("channel-up", {"pid": self.pid}))

    def stop(self):
        self._close_output()
        self.state, self._events = "stopped", []
        self._reset_link()

    def close(self):
        self.stop()

    def poll(self):
        if self.out is not None:
            self._watch(self.clock())
        ev, self._events = self._events, []
        return ev

    def send(self, op, **args):
        if self.out is None:
            return False
        if op == "probe":
            self._probe = True
            if self._fresh:
                self._presence()
        elif op == "init":
            self._init_id = args.get("id")
            self._ping()
        elif op == "ping":
            if self._fresh:
                self._events.append(BackendEvent("pong", {"id": args.get("id", 0)}))
        elif op == "lab":
            return self.lab(args.get("scenario"))
        return True

    def lab(self, scenario):
        return False

    def lab_local(self, scenario):
        return False

    def info(self):
        return {"kind": self.kind, "lab": self.supports_lab, "state": self.state, "pid": self.pid, "restarts": 0,
                "restart_limit": self.timings.restart_limit, "last_exit": self.last_exit}

    def crsf_snapshot(self):
        """Блок status.module.crsf: снимок CrsfOutput (раздел 3.1, поля SNAP_KEYS) и имя порта."""
        snap = self.out.snapshot() if self.out is not None else {"mode": "none"}
        out = {k: snap.get(k) for k in SNAP_KEYS}
        out["mode"] = out["mode"] or "none"
        out["port"] = self.port_name
        out["emu"] = None
        return out

    # ---------- события по ответам модуля ----------
    def _ping(self):
        self._next_ping = self.clock() + PING_S
        try:
            self.out.request_ping()
        except Exception as e:  # noqa: BLE001
            self.log(f"crsf ping: {e!r}")

    def _presence(self):
        self._probe = False
        self._events.append(BackendEvent("presence", {"present": True}))

    def _fault(self, code, text):
        self._init_id = self._ready_at = None
        self._events.append(BackendEvent("fault", {"code": code, "text": text}))

    def _watch(self, now):
        s = self.out.snapshot()
        age = s.get("last_rx_ms")
        age = age if isinstance(age, (int, float)) and not isinstance(age, bool) else None
        fresh = age is not None and age <= self.profile.no_response_s * 1000
        if fresh:
            self._fresh, self._silence_reported = True, False
        elif self._fresh:
            self._fresh = False
            if not self._silence_reported:
                self._silence_reported = True
                self.log(f"crsf: module silent for {self.profile.no_response_s:g} s")
                self._fault(MOD_NO_RESPONSE, f"no CRSF frames from module for {self.profile.no_response_s:g} s")
        run = s.get("crc_errors_run") or 0
        if run < CRC_FAULT:
            self._crc_reported = False
        elif not self._crc_reported:
            self._crc_reported = True
            self.log(f"crsf: {run} CRC errors in a row")
            self._fault(MOD_IFACE_ERROR, f"CRSF CRC errors: {run} in a row from module")
        if self._probe and self._fresh:
            self._presence()
        dev = s.get("device")
        if self._init_id is not None and self._fresh and isinstance(dev, dict):
            name = str(dev.get("name") or "")
            if self.profile.matches_crsf(name):
                ident = {"vendor": self.profile.vendor, "model": self.profile.model,
                         "firmware": str(dev.get("sw") or "")[:32], "serial": str(dev.get("serial") or "")[:32]}
            else:
                ident = {"vendor": "?", "model": name[:32] or "?", "firmware": str(dev.get("sw") or "")[:32],
                         "serial": str(dev.get("serial") or "")[:32]}
            self.log(f"crsf: device info {name!r} sw={dev.get('sw')}")
            self._events.append(BackendEvent("init", {"id": self._init_id, "ok": True, "identity": ident}))
            self._init_id, self._ready_at = None, now
        elif self._ready_at is not None:
            if s.get("synced") or s.get("link"):
                self._ready_at = None
            elif now - self._ready_at > self.profile.no_response_s:
                self._fault(MOD_NO_RESPONSE, f"no CRSF sync (0x3A) or link statistics (0x14) in "
                                             f"{self.profile.no_response_s:g} s")
        need_ping = self._init_id is not None or self._probe or not self._fresh
        if need_ping and now >= self._next_ping:
            self._ping()


class UartCrsfBackend(CrsfBackend):
    """crsf-uart: линия JR-отсека на UART3 (ADR-0014, 3.1); лабораторных команд нет."""
    kind = "crsf-uart"
    supports_lab = False

    def __init__(self, profile, settings, *, find_uart=None, **kw):
        super().__init__(profile, settings, **kw)
        self.find_uart = find_uart

    def _open_port(self):
        port, _ = _crsf()
        u = self.settings.uart_dict()
        path = u.get("device") or "auto"
        if path == "auto":
            path = (self.find_uart or port.find_uart3)()
            if not path:
                raise ModuleError(UART_NOT_FOUND, code=MOD_IFACE_ERROR)
        baud = int(u.get("baud") or self.profile.baud)
        try:
            p = port.UartPort(path, baud=baud, half_duplex=bool(u.get("half_duplex", True)))
        except OSError as e:
            raise ModuleError(f"UART {path}: {e.strerror or e}", code=MOD_IFACE_ERROR) from e
        return p, str(path)


class PtyCrsfLabBackend(CrsfBackend):
    """crsf-pty-lab: псевдотерминал и эмулятор ELRS TX дочерним процессом (лабораторный)."""
    kind = "crsf-pty-lab"
    supports_lab = True
    lab_scenarios = (*LAB_MAP, *PARENT_SCENARIOS)

    def __init__(self, profile, settings, *, command=None, **kw):
        super().__init__(profile, settings, **kw)
        self.command = list(command) if command else [sys.executable, "-s", "-m", EMULATOR_MODULE]
        self.refuse = False                 # emu-unavailable: новые запуски отклоняются
        self.proc = None
        self.starts = 0
        self.emu = None                     # последняя строка состояния эмулятора
        self._q = None
        self._eof = False
        self._exit_seen = None
        self._dying = []

    def _open_port(self):
        port, _ = _crsf()
        p = port.PtyPort()
        return p, "pty"

    def start(self):
        self._reset_link()
        if self.proc is not None:
            self._retire(self.proc)
            self.proc = None
        if self.refuse:
            self.last_exit = 69
            self.log("emulator start refused (lab scenario emu-unavailable)")
            self._events.append(BackendEvent("channel-down", {"reason": "start-failed", "code": 69}))
            return
        self._start_output()
        from .. import video
        try:
            cmd = self.command + ["--tty", _peer_path(self.port)]
            proc = video.spawn(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=child_env())
        except (OSError, ModuleError) as e:
            self._close_output()
            self.log(f"emulator start failed: {e}")
            self._events.append(BackendEvent("channel-down", {"reason": "start-failed", "code": None}))
            return
        os.set_blocking(proc.stdin.fileno(), False)
        self.proc, self.pid = proc, proc.pid
        self.state = "restarting" if self.starts else "starting"
        self.starts += 1
        self.emu, self._eof, self._exit_seen = None, False, None
        self._q = queue.SimpleQueue()
        threading.Thread(target=_reader, args=(proc.stdout, self._q), name="elrs-tx-reader", daemon=True).start()
        self.log(f"emulator ELRS TX started pid={proc.pid} port={self.port_name}")
        self.state = "running"
        self._events.append(BackendEvent("channel-up", {"pid": self.pid}))

    def stop(self):
        if self.proc is not None:
            self.log(f"emulator stopping pid={self.pid}")
            self._retire(self.proc)
        self.proc, self.pid, self.emu = None, 0, None
        super().stop()

    def close(self):
        procs = [d[0] for d in self._dying] + ([self.proc] if self.proc is not None else [])
        self._dying, self.proc = [], None
        super().stop()
        end = time.monotonic() + self.timings.stop_timeout_s
        for p in procs:
            _killpg(p, signal.SIGTERM)
        for p in procs:
            try:
                p.wait(timeout=max(0.0, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                _killpg(p, signal.SIGKILL)
                try:
                    p.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass

    def poll(self):
        now = self.clock()
        self._reap(now)
        p = self.proc
        if p is not None:
            self._drain()
            rc = p.poll()
            if rc is not None:
                self._exit_seen = self._exit_seen if self._exit_seen is not None else now
                if self._eof or now - self._exit_seen >= 1.0:
                    self._drain()
                    self._down(exit_code(rc))
        return super().poll()

    def lab(self, scenario):
        if scenario == "crash":
            return self.lab_local(scenario)
        target = LAB_MAP.get(scenario)
        if target is None or self.proc is None:
            return False
        data = json.dumps({"op": "lab", "scenario": target}).encode() + b"\n"
        try:
            os.write(self.proc.stdin.fileno(), data)
        except (BlockingIOError, BrokenPipeError, OSError, ValueError):
            return False
        self.log(f"lab {scenario} -> emulator {target}")
        self._events.append(BackendEvent("lab", {"scenario": scenario, "ok": True, "error": ""}))
        return True

    def lab_local(self, scenario):
        if scenario == "emu-unavailable":
            self.refuse = True
            if self.proc is not None:
                self.log(f"lab emu-unavailable: emulator pid={self.pid} killed, new starts refused")
                _killpg(self.proc, signal.SIGKILL)
            return True
        if scenario == "emu-available":
            self.refuse = False
            return True
        if scenario == "crash" and self.proc is not None:
            self.log(f"lab crash: emulator pid={self.pid} killed")
            _killpg(self.proc, signal.SIGKILL)
            return True
        return False

    def crsf_snapshot(self):
        snap = super().crsf_snapshot()
        snap["emu"] = _emu_view(self.emu)
        return snap

    # ---------- процесс эмулятора ----------
    def _drain(self):
        while self._q is not None:
            try:
                kind, line = self._q.get_nowait()
            except queue.Empty:
                return
            if kind == "eof":
                self._eof = True
                continue
            if kind != "line":
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("ev") == "state":
                self.emu = msg
            elif msg.get("ev") == "lab" and not msg.get("ok"):
                self.log(f"lab refused by emulator: {str(msg.get('error'))[:80]}")

    def _down(self, code):
        self._close_output()
        p, self.proc = self.proc, None
        try:
            p.stdin.close()
        except OSError:
            pass
        self.last_exit, self.state, self.pid = code, "stopped", 0
        self.log(f"emulator ELRS TX exited code={code}")
        self._reset_link()
        self._events.append(BackendEvent("channel-down", {"reason": "exited", "code": code}))

    def _retire(self, p):
        try:
            p.stdin.close()
        except OSError:
            pass
        now = self.clock()
        _killpg(p, signal.SIGTERM)
        self._dying.append([p, now + self.timings.stop_timeout_s])

    def _reap(self, now):
        keep = []
        for p, kill_at in self._dying:
            if p.poll() is not None:
                continue
            if now >= kill_at:
                _killpg(p, signal.SIGKILL)
            keep.append([p, kill_at])
        self._dying = keep


def _peer_path(port):
    """Путь к ведомой стороне псевдотерминала для эмулятора (PtyPort.path)."""
    for attr in ("path", "peer_path", "slave_path"):
        v = getattr(port, attr, None)
        if isinstance(v, str) and v:
            return v
    fd = getattr(port, "slave_fd", None)
    if isinstance(fd, int):
        return os.ttyname(fd)
    raise ModuleError("pty: no peer path")


def _emu_view(m):
    """Строка state эмулятора ELRS TX → crsf.emu: линия (connected | uart-disconnected), принятые оси 1–8 в шкале
    пульта −1000…1000 (axes), взведение, число кадров каналов, их частота и джиттер, ошибки CRC со стороны модуля."""
    if not isinstance(m, dict):
        return None
    axes = m.get("axes")
    return {"state": str(m.get("host") or "")[:24], "ch": list(axes)[:8] if isinstance(axes, list) else [],
            "armed": m.get("armed"), "frames": m.get("rc_frames", 0), "rate_hz": m.get("rate_hz", 0),
            "jitter_us_p99": m.get("jitter_us_p99", 0), "crc_errors": m.get("crc_errors", 0)}
