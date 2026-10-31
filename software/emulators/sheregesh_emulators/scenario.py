# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Стенд из эмуляторов и детерминированные сценарии неисправностей."""
from dataclasses import dataclass, field

from sheregesh.monitor import SystemMonitor

from .devices import (EventGenerator, FanEmulator, IndicatorEmulator, PeerEmulator, PowerEmulator, SimClock,
                      ThermalEmulator)

TICK_S = 0.5


class Bench:
    """Эмулированный стенд пульта: все провайдеры + монитор на модельных часах."""

    def __init__(self, diagnostics: dict, link: dict, peer_factory=None, services=None):
        """peer_factory(clock) → внешний Peer (например, RemotePeer к процессу удалённого блока); по умолчанию — эмулятор."""
        self.clock = SimClock()
        self.power = PowerEmulator()
        self.fan = FanEmulator()
        self.thermal = ThermalEmulator(self.fan)
        self.peer = peer_factory(self.clock) if peer_factory else PeerEmulator(self.clock)
        self.indicator = IndicatorEmulator(self.clock)
        self.monitor = SystemMonitor(self.clock, self.power, self.thermal, self.fan, self.peer, self.indicator,
                                     diagnostics, link, services=services)
        self.events = EventGenerator(self.monitor.log, self.clock)
        self.snapshot = self.monitor.tick()

    def target(self, name):
        return {"power": self.power, "fan": self.fan, "thermal": self.thermal, "peer": self.peer,
                "events": self.events, "monitor": self.monitor}[name]

    def step(self, dt=TICK_S):
        self.clock.advance(dt)
        for dev in (self.power, self.thermal, self.peer):
            if hasattr(dev, "step"):
                dev.step(dt)
        self.snapshot = self.monitor.tick()
        return self.snapshot


@dataclass(frozen=True)
class Step:
    t: float                 # модельное время, с
    target: str              # power | fan | thermal | peer | events
    action: str              # имя метода эмулятора
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    label: str | dict = ""      # строка или {"ru": ..., "en": ...}


@dataclass
class Scenario:
    name: str
    steps: list
    duration: float
    checkpoints: dict = field(default_factory=dict)   # метка → модельное время

    def __post_init__(self):
        self.steps = sorted(self.steps, key=lambda s: s.t)

    def runner(self, bench: Bench):
        return ScenarioRunner(self, bench)


class ScenarioRunner:
    """Выполняет сценарий по шагам модельного времени; GUI вызывает advance() по таймеру, тесты — run_until()."""

    def __init__(self, scenario: Scenario, bench: Bench):
        self.scenario, self.bench = scenario, bench
        self._next = 0
        self.log = []          # (t, метка) выполненных шагов

    @property
    def done(self):
        return self.bench.clock() >= self.scenario.duration

    def _fire_due(self):
        steps = self.scenario.steps
        while self._next < len(steps) and steps[self._next].t <= self.bench.clock() + 1e-9:
            s = steps[self._next]
            target = self.bench.target(s.target)
            # С внешним удалённым блоком его неисправности задаёт сам процесс блока (сценарий агента).
            if hasattr(target, s.action):
                getattr(target, s.action)(*s.args, **s.kwargs)
            self.log.append((self.bench.clock(), s.label or f"{s.target}.{s.action}"))
            self._next += 1

    def advance(self, dt=TICK_S):
        if self.bench.clock() == 0 and self._next == 0:
            self._fire_due()
        snap = self.bench.step(dt)
        self._fire_due()
        return snap

    def run_until(self, t, on_tick=None):
        while self.bench.clock() < t - 1e-9:
            snap = self.advance()
            if on_tick:
                on_tick(snap)
        return self.bench.snapshot
