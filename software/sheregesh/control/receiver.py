# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Сторож свежести команд на удалённом блоке (ADR-0013, разделы 2 и 3): единственный, кто решает, свежи ли команды.

Состояния (wire.STATES):
  IDLE      команд не было (start) или оператор выключил передачу (en=false → disabled); каналы и кнопки — нули;
  ACTIVE    свежие кадры с en=true и dev.ok=true (fresh); действуют ch и btn последнего кадра;
  FAILSAFE  нет кадра дольше timeout_ms (timeout), сеанс потерян (link-lost) или устройство пропало
            (dev.ok=false → device-lost); каналы и кнопки — нули (центр).
Возврат в ACTIVE — автоматически по свежему кадру с en=true и dev.ok=true; в IDLE — по кадру с en=false. Потеря сеанса
в IDLE состояние не меняет; в FAILSAFE причина остаётся первой. Каждый переход журналируется.

Без потоков и системных часов: время — аргумент now (секунды монотонной шкалы) или clock(). Владелец вызывает
tick(now) не реже раза в 50 мс — тогда FAILSAFE наступает не позже timeout_ms + 50 мс после последнего кадра.
"""
import time
from collections import deque

from . import wire

RATE_WINDOW_S = 2.0                                  # rate_hz — за 2 с
LATE_WINDOW_S = 10.0                                 # late_max_ms — за 10 с
BAD_LOG_EVERY_S = 10.0                               # неверное тело — в журнал не чаще раза в 10 с
ZERO = (0,) * wire.CHANNELS


class ControlReceiver:
    def __init__(self, clock=time.monotonic, timeout_ms=500, log=None):
        if not 50 <= int(timeout_ms) <= 10000:
            raise ValueError("timeout_ms must be 50…10000")
        self.clock = clock
        self.timeout_ms = int(timeout_ms)
        self.log = log or (lambda _s: None)
        now = clock()
        self._state, self._reason, self._since = "IDLE", "start", now
        self._ch, self._btn = ZERO, 0                # действующие значения (не ACTIVE — нули)
        self._dev = {"name": "", "ok": False}
        self._n = 0                                  # последний принятый номер кадра в сеансе (новый сеанс — 0)
        self._last_rx = None                         # время последнего принятого кадра (возраст, сторож)
        self._prev_rx = None                         # … для интервалов: только внутри непрерывного потока en=true
        self._stream_start = None                    # первый кадр текущего потока (rate_hz в первые 2 с)
        self._rx_times = deque()                     # времена кадров за RATE_WINDOW_S
        self._intervals = deque()                    # (время, интервал) за LATE_WINDOW_S
        self._last_bad_log = None
        self.frames = self.gaps = self.stale = self.bad = self.failsafes = 0
        self.transitions = 0                         # растёт на каждом переходе: повод отправить rc-state сразу

    # ---------- входы ----------
    def on_rc(self, frame: "wire.RcFrame", now=None) -> bool:
        """Принятый кадр rc (уже проверенный wire.check_rc). False — устаревший (n ≤ последнего), отброшен."""
        now = self.clock() if now is None else now
        if frame.n <= self._n:
            self.stale += 1
            return False
        if self._n and frame.n > self._n + 1:
            self.gaps += frame.n - self._n - 1
        self._n = frame.n
        self.frames += 1
        if self._prev_rx is not None:
            self._intervals.append((now, now - self._prev_rx))
        if self._stream_start is None:
            self._stream_start = now
        self._rx_times.append(now)
        self._last_rx = now
        self._dev = {"name": frame.dev_name, "ok": frame.dev_ok}
        if not frame.en:
            self._prev_rx = self._stream_start = None   # выключено оператором: следующий кадр начинает новый поток
            self._go("IDLE", "disabled", now)
        elif not frame.dev_ok:
            self._prev_rx = now
            self._go("FAILSAFE", "device-lost", now)
        else:
            self._prev_rx = now
            self._ch, self._btn = frame.ch, frame.btn
            self._go("ACTIVE", "fresh", now)
        self._trim(now)
        return True

    def on_bad(self, text, now=None):
        """Неверное тело rc: не исполняется, счётчик bad, журнал не чаще раза в BAD_LOG_EVERY_S."""
        now = self.clock() if now is None else now
        self.bad += 1
        if self._last_bad_log is None or now - self._last_bad_log >= BAD_LOG_EVERY_S:
            self._last_bad_log = now
            self.log(f"bad rc frame: {text} (bad={self.bad})")

    def on_link_lost(self, now=None):
        """Сеанс закрыт (любым путём) или заменён новым: ACTIVE → FAILSAFE (link-lost); нумерация кадров — заново."""
        now = self.clock() if now is None else now
        self._n = 0
        self._prev_rx = self._stream_start = None
        if self._state == "ACTIVE":
            self._go("FAILSAFE", "link-lost", now)

    def tick(self, now=None):
        """Сторож: в ACTIVE нет кадра дольше timeout_ms — FAILSAFE (timeout)."""
        now = self.clock() if now is None else now
        if self._state == "ACTIVE" and self._last_rx is not None and (now - self._last_rx) * 1000 > self.timeout_ms:
            self._stream_start = None                # интервал до следующего кадра — «опоздание» (late_max_ms)
            self._go("FAILSAFE", "timeout", now)
        self._trim(now)

    # ---------- выходы ----------
    def state(self) -> str:
        return self._state

    def outputs(self):
        """(ch, btn), действующие на блоке: в IDLE и FAILSAFE — нули."""
        return self._ch, self._btn

    def deadline(self):
        """Срок действия команд ACTIVE (шкала clock): время последнего принятого кадра + timeout_ms; вне ACTIVE —
        None. Растёт только с принятым кадром: tick(), устаревший или неверный кадр срок не продлевают."""
        if self._state != "ACTIVE" or self._last_rx is None:
            return None
        return self._last_rx + self.timeout_ms / 1000

    def snapshot(self, now=None) -> dict:
        """Тело rc-state (раздел 3.2)."""
        now = self.clock() if now is None else now
        self._trim(now)
        return {"v": wire.STATE_V, "state": self._state, "reason": self._reason,
                "since_s": round(max(0.0, now - self._since), 1), "n": self._n,
                "age_ms": None if self._last_rx is None else int(max(0.0, now - self._last_rx) * 1000),
                "rate_hz": self._rate(now), "gaps": self.gaps, "stale": self.stale, "bad": self.bad,
                "late_max_ms": int(max((i for _t, i in self._intervals), default=0.0) * 1000),
                "failsafes": self.failsafes, "timeout_ms": self.timeout_ms, "ch": list(self._ch), "btn": self._btn,
                "dev": dict(self._dev), "output": wire.OUTPUT}

    def health(self, now=None) -> dict:
        """Сжато для link-status.json: без значений каналов."""
        s = self.snapshot(now)
        out = {k: s[k] for k in ("state", "reason", "since_s", "n", "age_ms", "rate_hz", "gaps", "stale", "bad",
                                 "late_max_ms", "failsafes", "timeout_ms", "output")}
        out.update(frames=self.frames, transitions=self.transitions, device=s["dev"]["name"], device_ok=s["dev"]["ok"])
        return out

    # ---------- внутреннее ----------
    def _go(self, state, reason, now):
        if state == self._state and reason == self._reason:
            return
        old = self._state
        if state != "ACTIVE":
            self._ch, self._btn = ZERO, 0
        if state == "FAILSAFE" and old != "FAILSAFE":
            self.failsafes += 1
        elif state == "FAILSAFE":
            return                                    # уже FAILSAFE: причина остаётся первой
        self._state, self._reason, self._since = state, reason, now
        self.transitions += 1
        age = "" if self._last_rx is None else f" age_ms={int(max(0.0, now - self._last_rx) * 1000)}"
        self.log(f"{old} -> {state} ({reason}) n={self._n}{age}")

    def _rate(self, now):
        times = self._rx_times
        if not times:
            return 0.0
        start = self._stream_start
        if start is not None and start > now - RATE_WINDOW_S:
            span = now - start                         # поток моложе окна: частота по его фактической длительности
            k = sum(1 for t in times if t >= start)
            return round((k - 1) / span, 1) if span > 0 and k > 1 else 0.0
        return round(len(times) / RATE_WINDOW_S, 1)

    def _trim(self, now):
        while self._rx_times and self._rx_times[0] <= now - RATE_WINDOW_S:
            self._rx_times.popleft()
        while self._intervals and self._intervals[0][0] <= now - LATE_WINDOW_S:
            self._intervals.popleft()
