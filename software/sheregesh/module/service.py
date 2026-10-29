# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Внешний модуль в агенте удалённого блока (ADR-0012, разделы 5, 6 и 8): настройки, менеджер, бэкенд, команды
SHRG-LAB (module-cmd), тело status.module и сводка для link-status.json. Агент вызывает только этот класс.

Исключения наружу не выходят: внутренняя ошибка пишется в журнал, модуль переходит в FAULT, агент, heartbeat и
сторож systemd не затронуты (раздел 7.3). Сохранение настроек (fsync на microSD) — в своём исполнителе, цикл агента
не ждёт: команда configure сразу получает итог pending, окончательный — на одном из следующих тактов.

Режим выхода CRSF (ADR-0014, раздел 2) решается здесь, по состоянию модуля и сторожа команд (set_control):
  none     бэкенд не CRSF или модуль не READY — кадров каналов нет;
  neutral  модуль READY, ControlReceiver IDLE или FAILSAFE — каналы failsafe_ch из настроек, без взведения;
  live     модуль READY, ControlReceiver ACTIVE — каналы пульта, взведение.
Поток CrsfOutput читает итог через output_source() — одна ссылка на неизменяемый снимок OutputCommand, без
блокировок. Снимок live несёт срок действия (deadline сторожа: последний принятый RC + timeout_ms) и нейтраль
failsafe_ch: поток выхода сам переходит на нейтраль, если такт агента, обновляющий снимок, не пришёл вовремя
(ADR-0014, раздел 2.1). ACTIVE без срока live не даёт. Переходы — в журнал («crsf: neutral -> live»).
"""
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

from . import wire
from .backend import check_backend, make_backend
from .manager import ModuleManager
from .profiles import DEFAULT_PROFILE, REGISTRY, get_profile
from .settings import NEUTRAL, ModuleSettings, SettingsStore
from .states import State, Timings


class OutputCommand(NamedTuple):
    """Снимок для потока CrsfOutput (ADR-0014, 2.1): source() → (mode, ch[8], btn, deadline, neutral, session).
    deadline — срок live на шкале clock (None вне live); neutral — failsafe_ch на случай истечения срока."""
    mode: str
    ch: tuple
    btn: int
    deadline: float | None = None
    neutral: tuple = NEUTRAL
    session: int = 0


class ModuleService:
    def __init__(self, config_dir: Path | None, *, clock=time.monotonic, log, timings=Timings(),
                 emulator_command=None, backend_factory=None, wall=time.time, crsf_emulator_command=None):
        self.config_dir = config_dir
        self.clock, self.log, self.timings = clock, log, timings
        self.emulator_command = emulator_command
        self.crsf_emulator_command = crsf_emulator_command
        self.backend_factory = backend_factory or self._make_backend
        self.store = SettingsStore(config_dir, log=log, clock=wall)
        self.settings = ModuleSettings()
        self.settings_info = {"source": "memory", "revision": "", "notice": ""}
        self.manager = None
        self.cmd = None                    # итог последней команды этого сеанса (status.module.cmd)
        self._pool = None
        self._pending = None               # (Future, req, ModuleSettings, сеанс) — идущий configure
        self._session = 0
        self._last_internal = None
        self.output = wire.OUTPUT          # режим выхода: none | neutral | live
        self._control = ("IDLE", NEUTRAL, 0, None)      # состояние ControlReceiver, каналы, кнопки, срок
        self._out = OutputCommand(wire.OUTPUT, NEUTRAL, 0)   # для потока CrsfOutput

    def _make_backend(self, s):
        return make_backend(s, get_profile(s.profile), clock=self.clock, log=self.log, timings=self.timings,
                            emulator_command=self.emulator_command, source=self.output_source,
                            crsf_emulator_command=self.crsf_emulator_command)

    # ---------- выход CRSF ----------
    def output_source(self):
        """source() для CrsfOutput (ADR-0014, 2.1): OutputCommand. Вызывается из потока выхода."""
        return self._out

    def set_control(self, state, ch, btn, deadline=None) -> None:
        """Состояние сторожа команд (такт агента, 25 мс): режим выхода пересчитывается сразу. deadline — срок
        ControlReceiver.deadline(); повторный вызов с тем же сроком его не продлевает."""
        try:
            self._control = (state, tuple(ch), int(btn), None if deadline is None else float(deadline))
            self._update_output()
        except Exception as e:  # noqa: BLE001
            self._internal(e)

    def _update_output(self):
        m = self.manager
        crsf = m is not None and hasattr(m.backend, "crsf_snapshot")
        st, ch, btn, deadline = self._control
        neutral = tuple(self.settings.failsafe_ch)
        if not crsf or m.state != State.READY:
            out = OutputCommand(wire.OUTPUT, NEUTRAL, 0, session=self._session)
        elif st == "ACTIVE" and deadline is not None:
            out = OutputCommand("live", ch, btn, deadline, neutral, self._session)
        else:
            out = OutputCommand("neutral", neutral, 0, None, neutral, self._session)
        self._out = out
        if out[0] != self.output:
            why = f"module {m.state}" if m is not None and m.state is not None else "no module"
            self.log(f"crsf: {self.output} -> {out[0]} ({why}, control {st}"
                     f"{', backend ' + self.settings.backend if not crsf else ''})")
            self.output = out[0]
        if m is not None:
            m.output = self.output

    def validate(self, s: ModuleSettings) -> str | None:
        """Проверка настроек без запуска процесса: профиль из реестра, вид бэкенда (раздел 8)."""
        if s.profile not in REGISTRY:
            return f"unknown profile {s.profile!r}"
        return check_backend(s)

    # ---------- жизненный цикл ----------
    def start(self) -> None:
        """load() настроек, менеджер, бэкенд; эмулятора не ждёт, исключений не выпускает."""
        try:
            self.settings, self.settings_info = self.store.load()
            why = self.validate(self.settings)
            if why:
                self.log(f"settings: {why} — factory profile and backend in memory")
                self.settings = self.settings.replace(profile=DEFAULT_PROFILE, backend="lab-emulator")
                self.settings_info = {**self.settings_info, "source": "memory",
                                      "notice": ",".join(filter(None, [self.settings_info.get("notice"), "CFG-MEMORY"]))}
        except Exception as e:  # noqa: BLE001 — запуск модуля не должен мешать агенту
            self.log(f"internal error: settings: {e!r}")
            self.settings = ModuleSettings()
        try:
            self.manager = ModuleManager(self.settings, self.backend_factory, clock=self.clock, log=self.log,
                                         timings=self.timings)
            self.manager.start()
        except Exception as e:  # noqa: BLE001
            self._internal(e)

    def tick(self) -> None:
        """manager.tick() + завершение отложенного configure."""
        try:
            self._finish_configure()
            if self.manager is not None:
                self.manager.tick()
            self._update_output()
        except Exception as e:  # noqa: BLE001 — исключение подсистемы не останавливает агента
            self._internal(e)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
        if self.manager is not None:
            try:
                self.manager.close()
            except Exception as e:  # noqa: BLE001
                self.log(f"internal error on close: {e!r}")

    # ---------- команды ----------
    def new_session(self) -> None:
        """Новый сеанс управления: итог прежней команды не показывается (как video-select)."""
        self.cmd = None
        self._session += 1
        try:                                # команды прежнего сеанса не переживают его: нейтраль до нового RC
            self._control = ("FAILSAFE", NEUTRAL, 0, None)
            self._update_output()
        except Exception as e:  # noqa: BLE001
            self._internal(e)

    def command(self, body: dict) -> None:
        """Тело module-cmd; итог — в status()["cmd"]."""
        try:
            op, args, req = wire.check_command(body)
        except wire.BadCommand as e:
            self._done(e.req, e.op, e.result, e.text)
            return
        try:
            if self.manager is None:
                self._done(req, op, "error", "module subsystem is not running")
            elif op == "configure":
                self._configure(req, args)
            else:
                res = self.manager.command(op, args)
                self._done(req, op, res, _explain(op, res), f"lab {args['scenario']}" if op == "lab" else None)
        except Exception as e:  # noqa: BLE001
            self._done(req, op, "error", f"internal error: {e}"[:200])
            self._internal(e)

    def _done(self, req, op, result, error="", what=None):
        self.cmd = {"req": req, "op": op, "result": result, "error": error[:200]}
        self.log(f"{what or op or '?'} (req {req}): {result}{': ' + error if error and result != 'ok' else ''}")

    def _configure(self, req, args):
        if self._pending is not None:
            self._done(req, "configure", "busy", "configure already in progress")
            return
        new = self.settings.replace(**args)
        why = self.validate(new)
        if why:
            self._done(req, "configure", "invalid", why)
            return
        if new == self.settings:
            self._done(req, "configure", "ok", "")
            return
        if self.store.store is None:
            self._done(req, "configure", "not-saved", "settings storage is not available; value not applied")
            return
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="module-settings")
        self._pending = (self._pool.submit(self.store.save, new, self.validate), req, new, self._session)
        self.cmd = {"req": req, "op": "configure", "result": "pending", "error": ""}
        self.log(f"configure (req {req}): {', '.join(f'{k}={v}' for k, v in sorted(args.items()))} — saving")

    def _finish_configure(self):
        if self._pending is None or not self._pending[0].done():
            return
        fut, req, new, session = self._pending
        self._pending = None
        try:
            result, info = fut.result()
        except Exception as e:  # noqa: BLE001 — сбой исполнителя = значение не сохранено
            result, info = "not-saved", {"text": f"settings not saved: {e}"}
        if result == "ok":
            self.settings = new
            self.settings_info = {"source": info["source"], "revision": info["revision"], "notice": info["notice"]}
            self.manager.apply_settings(new)
        if session == self._session:
            self._done(req, "configure", result, info.get("text", ""))
        else:                                       # новый сеанс: итог только в журнал
            self.log(f"configure (req {req}): {result}")

    def _internal(self, e):
        text = f"{type(e).__name__}: {e}"[:160]
        if text != self._last_internal:            # повторяющаяся ошибка — одна строка
            self.log(f"internal error {text}")
            self._last_internal = text
        if self.manager is not None:
            try:
                self.manager.internal_error(text)
            except Exception:  # noqa: BLE001
                pass

    # ---------- телеметрия ----------
    @property
    def seq(self) -> int:
        return self.manager.seq if self.manager is not None else 0

    def status(self) -> dict:
        """Полное status.module (раздел 6.2)."""
        body = None
        if self.manager is not None:
            try:
                body = self.manager.snapshot()
            except Exception as e:  # noqa: BLE001 — телеметрия агента не должна падать из-за модуля
                self._internal(e)
        if body is None:
            body = {"v": wire.STATUS_V, "state": "FAULT", "reason": "internal-error", "output": wire.OUTPUT}
        body["settings"] = {**self.settings.as_dict(), **self.settings_info}
        if self.cmd is not None:
            body["cmd"] = dict(self.cmd)
        return body

    def health(self) -> dict:
        """Сжато для link-status.json (раздел 12.1)."""
        m = self.manager
        try:
            snap = m.snapshot() if m is not None else None
        except Exception as e:  # noqa: BLE001
            self._internal(e)
            snap = None
        if snap is None:
            return {"state": "FAULT", "seq": self.seq}
        b, err = snap["backend"], snap["error"]
        out = {"state": snap["state"], "seq": snap["seq"], "since_s": snap["since_s"], "profile": snap["profile"]["id"],
               "backend": self.settings.backend, "backend_state": b["state"], "pid": b["pid"],
               "restarts": b["restarts"],
               "error_code": err["code"] if err else "", "error_active": bool(err and err["active"]),
               "settings_revision": self.settings_info.get("revision", ""),
               "settings_source": self.settings_info.get("source", ""), "output": snap["output"]}
        c = snap.get("crsf")
        if isinstance(c, dict):
            out["crsf"] = {k: c.get(k) for k in ("mode", "port", "period_us", "synced", "frames_sent", "jitter_us_p99",
                                                 "crc_errors", "last_rx_ms")}
        return out


def _explain(op, res):
    return {"disabled": "module is disabled (settings)", "forbidden": "laboratory commands need a laboratory backend",
            "not-running": "emulator is not running", "unknown-op": f"unknown op {op!r}",
            "bad-request": "bad arguments", "error": "command failed"}.get(res, "")
