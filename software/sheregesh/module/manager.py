# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Менеджер внешнего модуля (ADR-0012, разделы 4 и 5): машина состояний, таймауты, бюджет перезапусков, последняя
ошибка, история, диагностика.

Не знает ни модели модуля (ModuleProfile), ни способа связи с ним (ModuleBackend). Время — только инъецированные
часы clock (монотонные секунды; в тестах SimClock), системные часы не читаются. Все методы неблокирующие и
вызываются из одного потока (цикл агента). Единственная точка переходов — tick() (события бэкенда и таймеры) и
command()/apply_settings(). Выхода управления нет при любом состоянии: snapshot()["output"] — всегда "none".
"""
import time
from collections import deque

from . import wire
from .backend import ModuleError
from .profiles import REGISTRY
from .states import (ALLOWED, EMU_EXITED, EMU_NO_KEEPALIVE, EMU_START_FAILED, EMU_UNAVAILABLE, ERROR_CODES, INITIAL,
                     MOD_IFACE_ERROR, MOD_INIT_FAILED, MOD_INIT_TIMEOUT, MOD_NO_RESPONSE, MOD_PROFILE_MISMATCH,
                     MOD_RECOVER_TIMEOUT, State, Timings)

S = State
HISTORY_KEEP = 32                 # в памяти; в телеметрию — wire.HISTORY_MAX последних


class RestartBudget:
    """Не более limit перезапусков в скользящем окне window_s (раздел 7.3)."""

    def __init__(self, limit: int, window_s: float, clock):
        self.limit, self.window_s, self.clock = limit, window_s, clock
        self._t = []

    def _prune(self):
        now = self.clock()
        self._t = [t for t in self._t if now - t < self.window_s]

    def try_spend(self) -> bool:
        self._prune()
        if len(self._t) >= self.limit:
            return False
        self._t.append(self.clock())
        return True

    def reset(self) -> None:
        self._t.clear()

    def used(self) -> int:
        self._prune()
        return len(self._t)


class ModuleManager:
    def __init__(self, settings, backend_factory, *, profiles=REGISTRY, clock=time.monotonic, log=lambda s: None,
                 timings=Timings(), strict=False):
        self.settings = settings
        self.factory = backend_factory
        self.profiles = profiles
        self.profile = profiles[settings.profile]           # KeyError — профиля нет (настройки проверяет сервис)
        self.clock, self.log, self.timings, self.strict = clock, log, timings, strict
        self.state = None                 # до start(); затем State
        self.seq = 0                      # число переходов с запуска
        self.reason = ""
        self._since = clock()
        self.backend = None
        self.channel = "down"             # down | starting | up — канал к модулю (процесс эмулятора)
        self._retired = []                # (бэкенд, срок) — остановленные при смене профиля, досматриваются в tick
        self.budget = RestartBudget(timings.restart_limit, timings.restart_window_s, clock)
        self.exhausted = False            # бюджет исчерпан: EMU-UNAVAILABLE, автоматических попыток нет
        self._backoff_i = 0               # попыток восстановления с последнего READY / reset / reinit
        self._next_attempt = None
        self.identity = None
        self.error = None                 # последняя ошибка: {"code","text","t","state","active"}
        self.history = deque(maxlen=HISTORY_KEEP)
        self.faults = self.recoveries = 0
        self._tick_no = 0
        self._detected_tick = -1
        self._init_id = 0
        self._init_started = self._init_deadline = None
        self._init_ms = 0
        self._ping_id = 0
        self._next_ping = self._last_pong = None
        self._recover_deadline = None

    # ---------- публичный API ----------
    def start(self) -> None:
        if not self.settings.enabled:
            self._transition(S.DISABLED, "start")
            return
        self._transition(S.DISCONNECTED, "start")
        self._launch("start-failed")

    def tick(self) -> None:
        """poll() бэкенда, затем таймеры. Единственная точка переходов по событиям."""
        self._tick_no += 1
        now = self.clock()
        if self._retired:
            keep = []
            for b, until in self._retired:
                b.poll()
                if now >= until:
                    b.close()
                else:
                    keep.append((b, until))
            self._retired = keep
        if self.backend is not None:
            for ev in self.backend.poll():
                self._on_event(ev)
        self._timers(self.clock())

    def command(self, op: str, args: dict) -> str:
        """reinit | reset | lab | status → код результата (раздел 6.3). configure исполняет ModuleService."""
        if op == "status":
            return "ok"
        if op not in ("reinit", "reset", "lab"):
            return "unknown-op"
        if self.state == S.DISABLED:
            return "disabled"
        if op == "reinit":
            self._backoff_i = 0
            st = self.state
            if st in (S.READY, S.DISCONNECTED, S.FAULT):
                self._recover("cmd-reinit")
            elif st == S.DETECTED:
                self._begin_init("cmd-reinit")
            elif st == S.INITIALIZING:             # перехода нет: инициализация начинается заново
                self.log("reinit: initialization restarted")
                self._send_init()
            elif st == S.RECOVERING:
                self._recover_deadline = self.clock() + self.timings.recover_timeout_s
                if self.channel == "up":
                    self.backend.send("probe")
            return "ok"
        if op == "reset":
            self.budget.reset()
            self._backoff_i = 0
            self.exhausted = False
            if self.state == S.FAULT:
                self._recover("cmd-reset")
            return "ok"
        scenario = (args or {}).get("scenario")
        if scenario not in wire.LAB_SCENARIOS:
            return "bad-request"
        b = self.backend
        if b is None:                             # фабрика отказала: канала нет
            return "not-running" if self.settings.backend == "lab-emulator" else "forbidden"
        if not getattr(b, "supports_lab", False):
            return "forbidden"                    # запрет структурный: только лабораторный бэкенд (раздел 6.4)
        if scenario in ("emu-unavailable", "emu-available"):
            return "ok" if b.lab_local(scenario) else "error"
        if self.channel != "up":
            return "not-running"
        return "ok" if b.send("lab", scenario=scenario) else "not-running"

    def apply_settings(self, s) -> None:
        """Новые настройки после успешного сохранения: enabled и profile меняют состояние, auto_* — поведение."""
        old, self.settings = self.settings, s
        profile = self.profiles[s.profile]
        if not s.enabled:
            if self.state != S.DISABLED:
                self._disable("settings-disabled")
            self._set_profile(profile)
            return
        if self.state == S.DISABLED:
            self._set_profile(profile)
            self._enable("settings-enabled")
            return
        if profile.id != self.profile.id:          # другой профиль — перезапуск канала через DISABLED
            self._disable("settings-profile")
            self._set_profile(profile)
            self._enable("settings-profile")
            return
        if not s.auto_recover:
            self._next_attempt = None
        elif not old.auto_recover and self.state == S.FAULT and self._next_attempt is None:
            self._schedule_recovery()

    def snapshot(self) -> dict:
        """Тело status.module без "cmd" и "settings" (раздел 6.2)."""
        now = self.clock()
        st = self.state or S.DISCONNECTED
        if self.backend is not None:
            b = dict(self.backend.info())
            b["lab"] = bool(getattr(self.backend, "supports_lab", False))
        else:
            b = {"kind": self.settings.backend, "lab": self.settings.backend == "lab-emulator", "state": "stopped",
                 "pid": 0, "last_exit": None}
        b["restarts"] = self.budget.used()
        b["restart_limit"] = self.timings.restart_limit
        if self.exhausted:
            b["state"] = "unavailable"
        err = None
        if self.error is not None:
            e = self.error
            err = {"code": e["code"], "text": e["text"], "since_s": _age(now, e["t"]), "state": e["state"],
                   "active": e["active"]}
        return {
            "v": wire.STATUS_V, "state": st.value, "reason": self.reason, "since_s": _age(now, self._since),
            "seq": self.seq, "profile": self.profile.describe(),
            "profiles": [{"id": p.id, "name": p.name} for p in list(self.profiles.values())[:wire.PROFILES_MAX]],
            "backend": b, "identity": dict(self.identity) if self.identity else None, "error": err,
            "recovery": {"auto": bool(self.settings.auto_recover), "attempt": self._backoff_i,
                         "next_s": None if self._next_attempt is None else round(max(0.0, self._next_attempt - now), 1),
                         "exhausted": self.exhausted},
            "diag": {"init_ms": self._init_ms,
                     "pong_age_s": _age(now, self._last_pong) if st == S.READY and self._last_pong is not None else None,
                     "faults": self.faults, "recoveries": self.recoveries, "transitions": self.seq},
            "output": wire.OUTPUT,
            "history": [{"seq": h["seq"], "from": h["from"], "state": h["state"], "reason": h["reason"],
                         "code": h["code"], "ago_s": _age(now, h["t"])} for h in list(self.history)[:wire.HISTORY_MAX]],
        }

    def internal_error(self, text: str) -> None:
        """Исключение внутри подсистемы (ModuleService): канал перезапускается через FAULT (раздел 7.3)."""
        if self.state in (None, S.DISABLED):
            return
        if self.backend is not None:
            try:
                self.backend.stop()
            except Exception:  # noqa: BLE001 — остановка после внутренней ошибки не должна бросать
                pass
        self.channel = "down"
        self._fault("internal-error", MOD_INIT_FAILED if self.state == S.INITIALIZING else EMU_EXITED,
                    f"internal error: {text}")

    def close(self) -> None:
        for b, _ in self._retired:
            b.close()
        self._retired = []
        if self.backend is not None:
            self.backend.close()

    # ---------- переходы ----------
    def _transition(self, new, reason, code=""):
        old = self.state
        if new not in (INITIAL if old is None else ALLOWED[old]):
            msg = f"illegal transition {old or 'start'} -> {new} ({reason})"
            if self.strict:
                raise AssertionError(msg)
            self.log(msg)
            return False
        now = self.clock()
        self.state, self.reason, self._since = new, reason, now
        self.seq += 1
        self.history.appendleft({"seq": self.seq, "from": old.value if old else "", "state": new.value,
                                 "reason": reason, "code": code, "t": now})
        self.log(f"{old or 'start'} -> {new} ({reason}){' code=' + code if code else ''} profile={self.profile.id}")
        return True

    def _fault(self, reason, code, text):
        now = self.clock()
        if self.state == S.FAULT:                  # FAULT → FAULT не переход: обновляется последняя ошибка
            self.error = {**self.error, "code": code, "text": text[:200], "t": now, "active": True}
            self.log(f"fault {code} ({reason}): {text}")
            return
        frm = self.state
        if not self._transition(S.FAULT, reason, code):
            return
        self.error = {"code": code, "text": text[:200], "t": now, "state": frm.value, "active": True}
        self.faults += 1
        self._init_deadline = self._recover_deadline = None
        self.identity = None if code.startswith("EMU-") else self.identity
        self._schedule_recovery()

    def _schedule_recovery(self):
        if not self.settings.auto_recover or self.exhausted:
            self._next_attempt = None
            return
        steps = self.timings.recover_backoff_s
        delay = steps[min(self._backoff_i, len(steps) - 1)]
        self._backoff_i += 1
        self._next_attempt = self.clock() + delay
        self.log(f"recovery attempt {self._backoff_i} in {delay:g} s")

    def _recover(self, reason):
        """→ RECOVERING: упавший канал — перезапуск процесса (по бюджету), живой — повторное обнаружение (probe)."""
        self._next_attempt = None
        if not self._transition(S.RECOVERING, reason):
            return
        self.recoveries += 1
        self._init_deadline = None
        self._recover_deadline = self.clock() + self.timings.recover_timeout_s
        if self.channel == "down":
            if not self.budget.try_spend():
                self.exhausted = True
                t = self.timings
                self.log(f"emulator restart limit reached: {t.restart_limit} in {t.restart_window_s:g} s — "
                         "automatic restarts stopped")
                self._fault("restart-limit", EMU_UNAVAILABLE,
                            f"emulator restart limit reached: {t.restart_limit} in {t.restart_window_s:g} s")
                return
            self.log(f"emulator restart {self.budget.used()}/{self.timings.restart_limit} "
                     f"in {self.timings.restart_window_s:g} s")
            self._launch("start-failed")
        elif self.channel == "up":
            self.backend.send("probe")

    def _launch(self, reason):
        """Запуск канала (бэкенд создаётся при первом запуске); отказ фабрики — FAULT EMU-START-FAILED."""
        try:
            if self.backend is None:
                self.backend = self.factory(self.settings)
            self.channel = "starting"
            self.backend.start()
        except (ModuleError, OSError, KeyError) as e:
            self.channel = "down"
            self._fault(reason, EMU_START_FAILED, f"backend not started: {e}")

    def _begin_init(self, reason):
        if self._transition(S.INITIALIZING, reason):
            self._send_init()

    def _send_init(self):
        now = self.clock()
        self._init_id += 1
        self._init_started = now
        self._init_deadline = now + self.profile.init_timeout_s
        self.backend.send("init", id=self._init_id, profile=self.profile.id)

    def _ready(self, identity):
        now = self.clock()
        if not self._transition(S.READY, "init-ok"):
            return
        self.identity = identity
        self._init_ms = int(round((now - self._init_started) * 1000)) if self._init_started is not None else 0
        self._init_deadline = None
        self._backoff_i, self._next_attempt = 0, None
        self._last_pong, self._next_ping = now, now + self.profile.ping_interval_s
        if self.error is not None:
            self.error["active"] = False

    def _absent(self):
        if self._transition(S.DISCONNECTED, "absent"):
            self.identity = None
            self._init_deadline = self._recover_deadline = None
            if self.error is not None:
                self.error["active"] = False

    def _disable(self, reason):
        if not self._transition(S.DISABLED, reason):
            return
        if self.backend is not None:
            self.backend.stop()
        self.channel = "down"
        self.identity = None
        self._init_deadline = self._recover_deadline = self._next_attempt = None
        if self.error is not None:
            self.error["active"] = False

    def _enable(self, reason):
        if not self._transition(S.DISCONNECTED, reason):
            return
        self.budget.reset()
        self._backoff_i, self.exhausted = 0, False
        self._launch("start-failed")

    def _set_profile(self, profile):
        if profile.id == self.profile.id:
            return
        self.profile = profile
        if self.backend is not None:              # бэкенд привязан к профилю: следующий запуск — новый
            self.backend.stop()
            self._retired.append((self.backend, self.clock() + 2 * self.timings.stop_timeout_s + 1.0))
            self.backend = None

    # ---------- события и таймеры ----------
    def _on_event(self, ev):
        k, d, st = ev.kind, ev.data, self.state
        if k == "channel-up":
            self.channel = "up"
            if st in (S.DISCONNECTED, S.RECOVERING):
                self.backend.send("probe")
        elif k == "channel-down":
            self.channel = "down"
            self.identity = None
            if st == S.DISABLED:
                return
            reason, code = d.get("reason"), d.get("code")
            if reason == "start-failed":
                self._fault("start-failed", EMU_START_FAILED,
                            "emulator did not start" + (f" (code={code})" if code is not None else " (no hello)"))
            elif reason == "no-keepalive":
                self._fault("channel-down", EMU_NO_KEEPALIVE, "emulator does not respond (keepalive): process killed")
            elif reason == "protocol":
                self._fault("channel-down", EMU_EXITED, "emulator protocol violation: process killed")
            else:
                self._fault("channel-down", EMU_EXITED, f"emulator exited code={code}")
        elif k == "presence":
            if d.get("present"):
                if st in (S.DISCONNECTED, S.RECOVERING) and self._transition(S.DETECTED, "present"):
                    self._detected_tick = self._tick_no
                    self._recover_deadline = None
            elif st in (S.DETECTED, S.INITIALIZING, S.READY, S.RECOVERING):
                self._absent()
        elif k == "init":
            if st != S.INITIALIZING or d.get("id") != self._init_id:
                return                                   # опоздавший ответ прежней инициализации
            if d.get("ok"):
                ident = d.get("identity") or {}
                if self.profile.matches(ident):
                    self._ready(ident)
                else:
                    self._fault("profile-mismatch", MOD_PROFILE_MISMATCH,
                                f"module {ident.get('vendor', '?')} {ident.get('model', '?')} does not match profile "
                                f"{self.profile.id}")
            else:
                code = d.get("code") if d.get("code") in ERROR_CODES else MOD_INIT_FAILED
                self._fault("init-error", code, d.get("text") or "initialization failed")
        elif k == "pong":
            if st == S.READY:
                self._last_pong = self.clock()
        elif k == "fault":
            code = d.get("code") if d.get("code") in ERROR_CODES else MOD_IFACE_ERROR
            text = d.get("text") or "interface error reported by module"
            if st == S.READY:
                self._fault("iface-fault", code, text)
            elif st == S.INITIALIZING:
                self._fault("init-error", code, text)
        elif k == "lab":
            if not d.get("ok"):
                self.log(f"lab {d.get('scenario')}: refused by emulator: {d.get('error')}")

    def _timers(self, now):
        st, p = self.state, self.profile
        if st == S.DETECTED:
            if self.settings.auto_init and self._tick_no > self._detected_tick:     # на следующем такте
                self._begin_init("auto-init")
        elif st == S.INITIALIZING:
            if self._init_deadline is not None and now >= self._init_deadline:
                self._fault("init-timeout", MOD_INIT_TIMEOUT, f"initialization did not finish in {p.init_timeout_s:g} s")
        elif st == S.READY:
            if now - self._last_pong > p.response_timeout_s:
                self._fault("no-response", MOD_NO_RESPONSE, f"no response for {p.response_timeout_s:g} s")
            elif now >= self._next_ping:
                self._ping_id += 1
                self._next_ping = now + p.ping_interval_s
                self.backend.send("ping", id=self._ping_id)
        elif st == S.RECOVERING:
            if self._recover_deadline is not None and now >= self._recover_deadline:
                self._fault("recover-timeout", MOD_RECOVER_TIMEOUT,
                            f"recovery did not finish in {self.timings.recover_timeout_s:g} s")
        elif st == S.FAULT:
            if self._next_attempt is not None and now >= self._next_attempt:
                self._recover("recover-attempt")


def _age(now, t):
    return round(max(0.0, now - t), 1)
