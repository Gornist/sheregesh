# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S8 (ADR-0014, раздел 2): CrsfOutput на модельных часах (SimClock, внедрённое ожидание) и фейковом порте —
период 4 мс до синхронизации и период модуля после, границы 1…50 мс, протухание sync за 2 с, поправка фазы,
режимы none/neutral/live, ping до device info, служебная очередь, джиттер и опоздания, снимок."""
import time

import pytest

from sheregesh.crsf import frames as F
from sheregesh.crsf.output import CrsfOutput, module_period_us
from sheregesh_emulators.devices import SimClock

CH = [100, -200, 1000, -1000, 0, 0, 0, 500]


class FakePort:
    collisions = 0

    def __init__(self, clock):
        self.clock = clock
        self.sent = []                  # (время, кадр)
        self.inbox = bytearray()

    def write(self, data):
        self.sent.append((self.clock(), bytes(data)))
        return len(data)

    def read(self):
        out, self.inbox = bytes(self.inbox), bytearray()
        return out

    def types(self):
        return [F.decode(b).type for _, b in self.sent]


class Rig:
    def __init__(self, mode="neutral", ch=CH, wait_extra=0.0):
        self.clock = SimClock(50.0)
        self.port = FakePort(self.clock)
        self.mode, self.ch = mode, list(ch)
        self.deadline = None                # None — владелец обновляет срок live при каждом чтении (свежий RC)
        self.neutral = None
        self.logs = []
        self.extra = wait_extra
        self.out = CrsfOutput(self.port, self.source, clock=self.clock, log=self.logs.append, wait=self.wait)

    def source(self):
        if self.mode != "live":
            return self.mode, self.ch, 0
        deadline = self.clock() + 0.5 if self.deadline is None else self.deadline
        return self.mode, self.ch, 0, deadline, self.neutral

    def wait(self, deadline):
        self.clock.advance(max(0.0, deadline - self.clock()) + self.extra)

    def identify(self):
        self.port.inbox += F.device_info_frame("RM Nomad X-Band (emu)")

    def sync(self, interval_us=20000, offset_us=0):
        self.port.inbox += F.timing_frame(interval_us, offset_us)

    def intervals(self, kind=None):
        ts = [t for t, b in self.port.sent if kind is None or b[2] == kind]
        return [round((b - a) * 1e6) for a, b in zip(ts, ts[1:])]


def test_period_4ms_before_sync_then_module_period():
    r = Rig()
    r.identify()
    r.out.run(11)
    assert set(r.intervals()) == {4000}
    s = r.out.snapshot()
    assert (s["period_us"], s["synced"], s["sync_age_ms"]) == (4000, False, None)
    r.sync(20000, 0)
    r.out.run(6)
    assert r.intervals()[-5:] == [20000] * 5
    s = r.out.snapshot()
    assert s["period_us"] == 20000 and s["synced"] is True and s["sync_age_ms"] == 120
    assert any("synced" in x for x in r.logs)


def test_sync_stale_after_2s_back_to_4ms():
    r = Rig()
    r.identify()
    r.sync(10000)
    r.out.run(1)
    r.out.run(202)                                  # больше 2 с без новых sync, затем период 4 мс
    assert r.intervals()[-1] == 4000 and r.out.snapshot()["synced"] is False
    assert r.out.snapshot()["period_us"] == 4000 and any("sync lost" in x for x in r.logs)


@pytest.mark.parametrize("interval,expected", [(20000, 20000), (4000, 4000), (500, 1000), (300, 1200),
                                               (60000, 50000), (1000, 1000), (0, 4000)])
def test_period_bounds(interval, expected):
    assert module_period_us(interval) == expected


def test_phase_offset_spent_once():
    r = Rig()
    r.identify()
    r.sync(20000, 0)
    r.out.run(3)
    r.sync(20000, 3000)                             # кадры приходят на 3 мс рано: следующий период длиннее
    r.out.run(4)
    assert r.intervals()[-3:] == [23000, 20000, 20000]
    r.sync(20000, -12000)                           # −12 мс ≡ +8 мс по модулю периода; ≤ ¼ периода за цикл
    r.out.run(4)
    assert r.intervals()[-3:] == [25000, 23000, 20000]
    r.sync(20000, 19900)                            # почти период «рано» = 100 мкс поздно
    r.out.run(3)
    assert r.intervals()[-2:] == [19900, 20000]


def test_modes_and_frames():
    r = Rig(mode="none")
    r.out.run(100)                                  # 0,4 с: только ping раз в 100 мс
    assert set(r.port.types()) == {F.T_PING} and len(r.port.sent) == 4
    assert r.out.snapshot()["frames_sent"] == 0 and r.out.snapshot()["armed"] is None
    r.identify()
    r.mode = "neutral"
    r.out.run(3)
    raw, armed = F.unpack_channels(F.decode(r.port.sent[-1][1]).payload)
    assert armed is False and raw == [F.axis_to_crsf(v) for v in CH] + [992] * 8
    assert len(r.port.sent[-1][1]) == 27
    r.mode = "live"
    r.out.run(1)
    raw, armed = F.unpack_channels(F.decode(r.port.sent[-1][1]).payload)
    assert armed is True and raw[:8] == [1074, 828, 1811, 172, 992, 992, 992, 1402]
    s = r.out.snapshot()
    assert s["mode"] == "live" and s["armed"] is True and s["ch"] == CH
    r.mode = "none"
    n = len(r.port.sent)
    r.out.run(50)
    assert r.port.types()[n:] == [F.T_PING] * 2   # снова только ping раз в 100 мс


def _last_channels(r):
    raw, armed = F.unpack_channels(F.decode(r.port.sent[-1][1]).payload)
    return [F.crsf_to_axis(v) for v in raw[:8]], armed


def test_live_snapshot_expires_at_deadline():
    """Срок live проверяется в каждом цикле: до deadline включительно — каналы со взведением, позже — нейтраль
    снимка без взведения; повторное чтение того же снимка срок не продлевает, live возвращает только новый срок."""
    r = Rig(mode="live")
    r.identify()
    fs = [0, 0, -1000, 0, 0, 0, 0, 0]
    r.neutral = fs
    r.out.run(1)
    r.deadline = r.clock() + 0.098                  # последний RC: срок между 25-м и 26-м периодом по 4 мс
    r.out.run(25)
    assert r.port.sent[-1][0] < r.deadline < r.clock()
    ch, armed = _last_channels(r)
    assert armed is True and all(abs(a - b) <= 2 for a, b in zip(ch, CH))
    r.out.run(1)                                    # первый цикл после срока
    ch, armed = _last_channels(r)
    assert armed is False and ch == fs
    assert r.port.sent[-1][0] - r.deadline <= 0.004 + 1e-9  # граница: deadline плюс один период отправителя
    r.out.run(100)                                  # тот же снимок 0,4 с: нейтраль, live не возвращается
    assert all(F.unpack_channels(F.decode(b).payload)[1] is False for _, b in r.port.sent[-100:])
    s = r.out.snapshot()
    assert s["mode"] == "neutral" and s["armed"] is False and s["ch"] == fs and s["expired"] == 101
    assert [x for x in r.logs if "expired" in x] == [r.logs[-1]] and "past deadline" in r.logs[-1]
    r.deadline = r.clock() + 0.1                    # новый RC — новый срок
    r.out.run(1)
    assert _last_channels(r)[1] is True
    assert r.out.snapshot()["mode"] == "live"


def test_live_without_deadline_or_neutral_is_center():
    """live без срока (старый трёхэлементный source) не взводит модуль; нейтраль без failsafe — центр."""
    r = Rig(mode="live")
    r.identify()
    r.out.source = lambda: ("live", CH, 0)
    r.out.run(3)
    ch, armed = _last_channels(r)
    assert armed is False and ch == [0] * 8
    assert r.out.snapshot()["expired"] == 3 and any("no deadline" in x for x in r.logs)


def test_ping_until_device_info_then_channels():
    r = Rig()
    r.out.run(60)                                   # 240 мс без ответа: ping в 0, 100, 200 мс вместо каналов
    t = r.port.types()
    assert t.count(F.T_PING) == 3 and t[0] == F.T_PING and t.count(F.T_CHANNELS) == 57
    r.identify()
    r.out.run(100)
    assert r.port.types()[60:] == [F.T_CHANNELS] * 100
    s = r.out.snapshot()
    assert s["device"] == {"name": "RM Nomad X-Band (emu)", "sw": "4.1.0", "serial": "ELRS"}
    r.out.request_ping()
    r.out.run(2)
    assert r.port.types()[-2:] == [F.T_PING, F.T_CHANNELS]
    r.out.run(600)                                  # 2,4 с молчания модуля: device забыт, ping возобновлён
    assert r.out.snapshot()["device"] is None and r.port.types()[-26:].count(F.T_PING) >= 1
    assert r.out.snapshot()["last_rx_ms"] >= 2000


def test_service_queue_replaces_channel_frame():
    r = Rig()
    r.identify()
    r.out.run(2)
    special = F.build(0x2C, bytes((0xEE, 0xEA, 1, 0)))
    r.out.queue(special)
    r.out.run(2)
    assert r.port.sent[-2][1] == special and r.port.types()[-1] == F.T_CHANNELS
    assert r.out.snapshot()["service_sent"] == 1


def test_source_error_is_neutral():
    r = Rig()
    r.identify()

    def broken():
        raise RuntimeError("receiver gone")

    r.out.source = broken
    r.out.run(2)
    raw, armed = F.unpack_channels(F.decode(r.port.sent[-1][1]).payload)
    assert raw == [992] * 16 and armed is False
    s = r.out.snapshot()
    assert s["source_errors"] == 2 and s["mode"] == "neutral"
    r.out.source = lambda: ("weird", CH, 0)
    r.out.run(1)
    assert r.out.snapshot()["source_errors"] == 3


def test_link_stats_crc_and_snapshot_shape():
    r = Rig()
    r.identify()
    r.port.inbox += F.link_stats_frame(rssi1=-55, lq=93, snr=7, rf_mode=5, power_idx=2)
    bad = bytearray(F.timing_frame(20000, 0))
    bad[-1] ^= 1
    r.port.inbox += bad
    r.out.run(2)
    s = r.out.snapshot()
    assert s["link"] == {"rssi": -55, "lq": 93, "snr": 7, "rf_mode": 5, "tx_power": 25}
    assert s["crc_errors"] >= 1 and s["synced"] is False and s["rx_frames"] == 2
    for key in ("mode", "period_us", "synced", "sync_age_ms", "frames_sent", "jitter_us_p99", "crc_errors",
                "collisions", "link", "device", "last_rx_ms"):
        assert key in s
    r.out.run(600)
    assert r.out.snapshot()["link"] is None                         # старше 2 с


def test_jitter_and_overrun():
    r = Rig(wait_extra=0.0003)                      # каждое пробуждение на 300 мкс позже срока
    r.identify()
    r.out.run(50)
    assert set(r.intervals()[1:]) == {4000}         # расписание абсолютное: опоздание не накапливается
    assert 299 <= r.out.snapshot()["jitter_us_p99"] <= 301
    r.extra = 0.0
    r.clock.advance(0.05)                           # остановка потока на 50 мс
    r.out.run(3)
    s = r.out.snapshot()
    assert s["overruns"] == 1 and r.intervals()[-2:] == [4000, 4000]


def test_thread_start_stop_real_clock():
    port = FakePort(time.monotonic)
    out = CrsfOutput(port, lambda: ("neutral", [0] * 8, 0))
    out.start()
    time.sleep(0.1)
    out.stop()
    assert not out.running and len(port.sent) >= 5
    n = len(port.sent)
    time.sleep(0.03)
    assert len(port.sent) == n
