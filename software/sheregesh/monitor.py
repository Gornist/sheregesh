# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Монитор состояния: опрос провайдеров, пороги из конфигурации, журнал событий, управление
собственным вентилятором и индикатором (REQ-P-005, PWR-005/007, SW-004, UI-003/004).

Монитор только наблюдает и управляет охлаждением и индикацией своего устройства. Выключение
системы в S1 не выполняется — формируется событие SYS-SHUTDOWN-REQ.
"""
from dataclasses import dataclass, field

from . import PROTOCOL_VERSION
from .faults import CATALOG, ERROR, SEVERITY_ORDER, WARNING
from .hal import PeerStatus, PowerReading
from .protocol import negotiate

HYSTERESIS_C = 2.0
FAN_MIN, FAN_MAX = 30.0, 100.0
FAN_T0, FAN_T1 = 45.0, 70.0           # линейная кривая: 45 °C → 30 %, 70 °C → 100 %
LOG_LIMIT = 500


@dataclass
class Event:
    id: int
    t: float
    code: str
    severity: str
    params: dict
    active: bool
    resolved_t: float | None = None


class EventLog:
    def __init__(self):
        self.events: list[Event] = []
        self._active: dict[str, Event] = {}
        self._seq = 0

    def _add(self, t, code, params, active):
        self._seq += 1
        ev = Event(self._seq, t, code, CATALOG[code].severity, dict(params), active)
        self.events.append(ev)
        del self.events[:-LOG_LIMIT]
        return ev

    def raise_(self, t, code, **params):
        """Активирует состояние (если ещё не активно) или пишет разовое событие. Возвращает новое событие или None."""
        if not CATALOG[code].latched:
            return self._add(t, code, params, False)
        if code in self._active:
            self._active[code].params.update(params)
            return None
        ev = self._add(t, code, params, True)
        self._active[code] = ev
        return ev

    def resolve(self, t, code):
        ev = self._active.pop(code, None)
        if ev:
            ev.active, ev.resolved_t = False, t
        return ev

    def is_active(self, code):
        return code in self._active

    def active(self):
        return sorted(self._active.values(), key=lambda e: (-SEVERITY_ORDER[e.severity], e.id))


@dataclass
class Snapshot:
    t: float
    power: PowerReading | None
    temperatures: dict | None
    fan_duty: float
    fan_rpm: int | None
    fan_nominal: int
    peer: PeerStatus
    peer_state: str              # connecting | connected | lost | incompatible | auth-failed
    indicator: str               # boot | normal | warning | fault
    overall: str                 # normal | warning | fault
    active: list = field(default_factory=list)
    shutdown_requested: str | None = None


class SystemMonitor:
    def __init__(self, clock, power, temperatures, fan, peer, indicator, diagnostics: dict, link: dict):
        self.clock, self.power, self.temps, self.fan, self.peer, self.indicator = \
            clock, power, temperatures, fan, peer, indicator
        self.log = EventLog()
        self.set_thresholds(diagnostics, link)
        self._last_source = None
        self._shutdown = None
        self._duty = FAN_MAX
        self._t_start = clock()
        self._peer_seen = False
        self.indicator.set_state("boot")

    def set_thresholds(self, diagnostics: dict, link: dict):
        self.diag = dict(diagnostics)
        self.link = dict(link)

    # ---- политика вентилятора (REQ-P-005) ----
    @staticmethod
    def fan_curve(temp_c):
        x = (temp_c - FAN_T0) / (FAN_T1 - FAN_T0)
        return max(FAN_MIN, min(FAN_MAX, FAN_MIN + x * (FAN_MAX - FAN_MIN)))

    def _request_shutdown(self, t, reason):
        """reason — код причины (не зависит от языка интерфейса)."""
        if self._shutdown is None:
            self._shutdown = reason
            self.log.raise_(t, "SYS-SHUTDOWN-REQ", reason=reason)

    def _thermal(self, t, temps):
        if not temps:
            self.log.raise_(t, "TEMP-NODATA")
            duty, hottest = FAN_MAX, None
        else:
            self.log.resolve(t, "TEMP-NODATA")
            sensor, hottest = max(temps.items(), key=lambda kv: kv[1])
            warn, crit = self.diag["temp_warning_c"], self.diag["temp_critical_c"]
            if hottest >= crit:
                self.log.raise_(t, "TEMP-CRIT", sensor=sensor, value=hottest, limit=crit)
                self._request_shutdown(t, "TEMP-CRIT")
            elif hottest < crit - HYSTERESIS_C:
                self.log.resolve(t, "TEMP-CRIT")
            if hottest >= warn:
                self.log.raise_(t, "TEMP-HIGH", sensor=sensor, value=hottest, limit=warn)
            elif hottest < warn - HYSTERESIS_C:
                self.log.resolve(t, "TEMP-HIGH")
            duty = self.fan_curve(hottest)

        rpm = self.fan.read_rpm()
        nominal = self.fan.nominal_rpm
        if rpm is None:
            self.log.raise_(t, "FAN-TACH")
            duty = FAN_MAX                                    # отказобезопасно: нет обратной связи → 100 %
        else:
            self.log.resolve(t, "FAN-TACH")
            commanded = self._duty
            if rpm >= 0.9 * nominal and commanded <= 60:
                self.log.raise_(t, "FAN-CTRL", rpm=rpm, duty=commanded)
            elif rpm < 0.9 * nominal or commanded > 60:
                self.log.resolve(t, "FAN-CTRL")
            if rpm == 0 and commanded >= FAN_MIN:
                self.log.raise_(t, "FAN-STALL", duty=commanded)
            elif rpm > 0:
                self.log.resolve(t, "FAN-STALL")
        if self.log.is_active("FAN-STALL") and hottest is not None and hottest >= self.diag["temp_warning_c"]:
            self._request_shutdown(t, "FAN-STALL")
        self._duty = duty
        self.fan.set_duty(duty)
        return rpm

    def _power(self, t, reading: PowerReading | None):
        if reading is None:
            self.log.raise_(t, "PWR-NODATA")
            return
        self.log.resolve(t, "PWR-NODATA")
        if self._last_source is not None and reading.source != self._last_source:
            self.log.raise_(t, "PWR-SOURCE", source=reading.source)
        self._last_source = reading.source
        on_battery = reading.source == "battery"
        if on_battery and reading.percent <= self.diag["battery_critical_pct"]:
            self.log.resolve(t, "PWR-LOW")
            self.log.raise_(t, "PWR-CRIT", percent=reading.percent)
        elif on_battery and reading.percent <= self.diag["battery_warning_pct"]:
            self.log.resolve(t, "PWR-CRIT")
            self.log.raise_(t, "PWR-LOW", percent=reading.percent)
        else:
            self.log.resolve(t, "PWR-CRIT")
            self.log.resolve(t, "PWR-LOW")
        if on_battery and reading.low_battery:
            self._request_shutdown(t, "PWR-LOW-SIGNAL")

    def _peer(self, t, st: PeerStatus):
        drain = getattr(self.peer, "drain_link_events", None)
        for code, params in (drain() if drain else []):
            self.log.raise_(t, code, **params)
        timeout = self.link["heartbeat_timeout_s"]
        if not st.link_up and (st.detail or "").startswith("auth"):
            self.log.raise_(t, "LINK-AUTH-FAIL", reason=st.detail.split(":", 1)[-1].strip())
            return "auth-failed"            # причина — сертификат/идентичность, а не обрыв связи
        age = None if st.last_heartbeat is None else t - st.last_heartbeat
        alive = st.link_up and age is not None and age <= timeout
        if alive:
            self._peer_seen = True
        elif not self._peer_seen and t - self._t_start <= timeout:
            return "connecting"            # первый таймаут после старта: связь ещё устанавливается
        if not alive:
            self.log.resolve(t, "PEER-SLOW")
            self.log.raise_(t, "PEER-LOST", timeout=timeout)
            return "lost"
        self.log.resolve(t, "PEER-LOST")
        self.log.resolve(t, "LINK-AUTH-FAIL")
        if age > timeout / 2:
            self.log.raise_(t, "PEER-SLOW", age=age, timeout=timeout)
        elif age < timeout / 3:
            self.log.resolve(t, "PEER-SLOW")
        remote = (st.telemetry or {}).get("active_events") or []
        if remote:
            self.log.raise_(t, "REMOTE-ALERT", codes=", ".join(remote))
        else:
            self.log.resolve(t, "REMOTE-ALERT")
        n = negotiate(PROTOCOL_VERSION, st.protocol, st.software, st.hardware)
        if not n.compatible:
            self.log.raise_(t, "PEER-INCOMPAT", peer=st.protocol, local=PROTOCOL_VERSION, reason=n.reason)
            return "incompatible"
        self.log.resolve(t, "PEER-INCOMPAT")
        return "connected"

    def tick(self) -> Snapshot:
        t = self.clock()
        temps = self.temps.read()
        rpm = self._thermal(t, temps)
        reading = self.power.read()
        self._power(t, reading)
        peer = self.peer.status()
        peer_state = self._peer(t, peer)
        active = self.log.active()
        worst = max((SEVERITY_ORDER[e.severity] for e in active), default=0)
        overall = "fault" if worst >= SEVERITY_ORDER[ERROR] else "warning" if worst >= SEVERITY_ORDER[WARNING] else "normal"
        self.indicator.set_state(overall)
        return Snapshot(t, reading, temps, self._duty, rpm, self.fan.nominal_rpm, peer, peer_state,
                        overall, overall, active, self._shutdown)
