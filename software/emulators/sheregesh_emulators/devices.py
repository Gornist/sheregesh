# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Эмуляторы интерфейсов sheregesh.hal. Состояние меняется только в step(dt) и методах сценария,
поэтому одинаковый сценарий всегда даёт одинаковый результат."""
from sheregesh.hal import PeerStatus, PowerReading


class SimClock:
    """Модельное время в секундах; монитор и эмуляторы не читают системные часы."""

    def __init__(self, t=0.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t = round(self.t + dt, 6)


class PowerEmulator:
    """Готовый модуль питания: источник, заряд, сигнал низкого заряда, отказ телеметрии."""

    def __init__(self, percent=85.0, source="external", discharge_pct_h=18.0, charge_pct_h=30.0, low_signal_pct=5.0):
        self.percent, self.source = float(percent), source
        self.discharge_pct_h, self.charge_pct_h, self.low_signal_pct = discharge_pct_h, charge_pct_h, low_signal_pct
        self.available = True
        self.undervoltage = False

    def step(self, dt):
        if self.source == "battery":
            self.percent = max(0.0, self.percent - self.discharge_pct_h * dt / 3600)
        else:
            self.percent = min(100.0, self.percent + self.charge_pct_h * dt / 3600)

    # сценарий
    def switch_source(self, source):
        assert source in ("external", "battery")
        self.source = source

    def set_percent(self, percent):
        self.percent = float(percent)

    def set_available(self, available):
        self.available = bool(available)

    def set_undervoltage(self, flag):
        self.undervoltage = bool(flag)

    def read(self):
        if not self.available:
            return None
        return PowerReading(self.source, round(self.percent, 2), self.percent <= self.low_signal_pct, self.undervoltage)


class FanEmulator:
    """4-проводной вентилятор: скважность → обороты; неисправности тахосигнала, управления, остановка."""

    def __init__(self, nominal_rpm=5000):
        self.nominal_rpm = nominal_rpm
        self.duty = 100.0
        self.fault = None   # None | "tach" | "pwm" | "stall"

    def set_duty(self, percent):
        self.duty = max(0.0, min(100.0, float(percent)))

    def set_fault(self, fault):
        assert fault in (None, "tach", "pwm", "stall")
        self.fault = fault

    def airflow(self):
        """Фактический поток воздуха 0…1. При потере PWM вентилятор выходит на полные обороты (спецификация 4-wire)."""
        if self.fault == "stall":
            return 0.0
        if self.fault == "pwm":
            return 1.0
        return self.duty / 100

    def read_rpm(self):
        if self.fault == "tach":
            return None
        return int(round(self.nominal_rpm * self.airflow()))


class ThermalEmulator:
    """Модель первого порядка: C·dT/dt = P − (k0 + kf·поток)·(T − Tокр). Датчики: soc и board."""

    def __init__(self, fan: FanEmulator, ambient=25.0, heat_w=4.0, start_c=45.0, capacity=8.0, k0=0.08, kf=0.25):
        self.fan, self.ambient, self.heat_w, self.t_soc = fan, float(ambient), float(heat_w), float(start_c)
        self.capacity, self.k0, self.kf = capacity, k0, kf
        self.available = True
        self._ramp = None   # (параметр, цель, скорость в ед./с)

    def step(self, dt):
        if self._ramp:
            name, target, rate = self._ramp
            cur = getattr(self, name)
            nxt = min(target, cur + rate * dt) if target > cur else max(target, cur - rate * dt)
            setattr(self, name, nxt)
            if nxt == target:
                self._ramp = None
        k = self.k0 + self.kf * self.fan.airflow()
        self.t_soc += dt * (self.heat_w - k * (self.t_soc - self.ambient)) / self.capacity

    def set_load(self, heat_w):
        self.heat_w = float(heat_w)

    def set_ambient(self, ambient):
        self.ambient = float(ambient)

    def ramp_ambient(self, target, rate_c_s):
        self._ramp = ("ambient", float(target), float(rate_c_s))

    def set_available(self, available):
        self.available = bool(available)

    def read(self):
        if not self.available:
            return None
        board = self.ambient + 0.6 * (self.t_soc - self.ambient)
        return {"soc": round(self.t_soc, 2), "board": round(board, 2)}


class PeerEmulator:
    """Состояние удалённого блока: связь, heartbeat раз в секунду, версии. Обмен данными не эмулируется (S1)."""

    def __init__(self, clock, protocol="1.0", software="0.1.0.dev0", hardware="HW-1.0 Rev A (emulator)",
                 device_id="remote-emu-01"):
        self.clock, self.protocol, self.software, self.hardware, self.device_id = \
            clock, protocol, software, hardware, device_id
        self.link_up = True
        self.responding = True
        self.last_heartbeat = clock()
        self._acc = 0.0

    def step(self, dt):
        self._acc += dt
        if self._acc >= 1.0:
            self._acc = 0.0
            if self.link_up and self.responding:
                self.last_heartbeat = self.clock()

    def disconnect(self):
        self.link_up = False

    def reconnect(self):
        self.link_up = True
        self.last_heartbeat = self.clock()

    def set_responding(self, flag):
        self.responding = bool(flag)

    def set_protocol(self, version):
        self.protocol = version

    def status(self):
        return PeerStatus(self.link_up, self.last_heartbeat if self.link_up else None, self.protocol,
                          self.software, self.hardware, self.device_id, detail="ok" if self.link_up else "closed",
                          session=self.protocol)


class IndicatorEmulator:
    """RGB-индикатор: хранит историю состояний для сверки с таблицей (REQ-UI-004)."""
    COLORS = {"boot": ("blue", "blink"), "normal": ("green", "steady"), "warning": ("amber", "blink"),
              "fault": ("red", "steady")}

    def __init__(self, clock):
        self.clock = clock
        self.state = None
        self.history = []

    def set_state(self, state):
        assert state in self.COLORS, state
        if state != self.state:
            self.state = state
            self.history.append((self.clock(), state))

    def color(self):
        return self.COLORS.get(self.state)


class EventGenerator:
    """Генератор диагностических событий: вызывает любые коды каталога по сценарию (REQ-UI-003)."""

    def __init__(self, log, clock):
        self.log, self.clock = log, clock

    def inject(self, code, **params):
        return self.log.raise_(self.clock(), code, **params)

    def clear(self, code):
        return self.log.resolve(self.clock(), code)
