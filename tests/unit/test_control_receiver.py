# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S7 (ADR-0013, разделы 2 и 3): сторож свежести команд ControlReceiver на модельных часах (SimClock).

IDLE → ACTIVE по свежим кадрам; прекращение кадров → FAILSAFE (timeout) не позже timeout + 100 мс при такте 50 мс;
потеря сеанса → FAILSAFE (link-lost) сразу; en=false → IDLE (disabled), не FAILSAFE; dev.ok=false → FAILSAFE
(device-lost); возврат в ACTIVE по свежим кадрам; n ≤ последнего — stale; пропуски номеров — gaps; неверное тело —
bad и журнал не чаще раза в 10 с; rate_hz за 2 с, late_max_ms за 10 с; в IDLE/FAILSAFE каналы и кнопки — нули;
output всегда "none"."""
import pytest

from sheregesh.control import wire
from sheregesh.control.receiver import ControlReceiver
from sheregesh_emulators.devices import SimClock

CH = (0, -12, 1000, 3, -1000, 0, 0, 1000)
TICK_S = 0.05                                       # такт сторожа агента — не реже 50 мс (ADR-0013, 4.1)


def frame(n, en=True, ok=True, ch=CH, btn=5):
    return wire.RcFrame(n, en, tuple(ch), btn, "OpenTX Radiomaster Boxer Joystick", ok)


class Rig:
    def __init__(self, timeout_ms=500):
        self.clock = SimClock(100.0)
        self.logs = []
        self.r = ControlReceiver(self.clock, timeout_ms=timeout_ms, log=self.logs.append)
        self.n = 0

    def stream(self, seconds, hz=50, en=True, ok=True, ch=CH):
        """Кадры с частотой hz и такт сторожа каждые TICK_S (общая сетка 10 мс)."""
        step = 0.01
        every, tick_every = round(1 / hz / step), round(TICK_S / step)
        for i in range(round(seconds / step)):
            if i % every == 0:
                self.n += 1
                self.r.on_rc(frame(self.n, en, ok, ch))
            if i % tick_every == 0:
                self.r.tick()
            self.clock.advance(step)

    def idle(self, seconds):
        """Кадров нет, только такт сторожа; → время первого состояния FAILSAFE (или None)."""
        end, first = self.clock() + seconds, None
        while self.clock() < end - 1e-9:
            self.clock.advance(TICK_S)
            self.r.tick()
            if first is None and self.r.state() == "FAILSAFE":
                first = self.clock()
        return first


def test_start_idle_zeros():
    rig = Rig()
    s = rig.r.snapshot()
    assert (s["state"], s["reason"], s["n"], s["age_ms"], s["rate_hz"]) == ("IDLE", "start", 0, None, 0.0)
    assert s["ch"] == [0] * 8 and s["btn"] == 0 and s["output"] == "none" and s["timeout_ms"] == 500
    assert s["dev"] == {"name": "", "ok": False} and rig.logs == []
    rig.idle(5)
    assert rig.r.state() == "IDLE"                    # без команд сторож не переходит в FAILSAFE


def test_active_rate_and_echo():
    rig = Rig()
    rig.stream(0.02)
    assert rig.r.state() == "ACTIVE" and rig.logs == ["IDLE -> ACTIVE (fresh) n=1 age_ms=0"]
    rig.stream(3)
    s = rig.r.snapshot()
    assert s["state"] == "ACTIVE" and s["reason"] == "fresh" and s["ch"] == list(CH) and s["btn"] == 5
    assert 49 <= s["rate_hz"] <= 51 and s["gaps"] == 0 and s["stale"] == 0 and s["late_max_ms"] == 20
    assert s["age_ms"] <= 20 and s["n"] == rig.n and s["failsafes"] == 0
    assert rig.r.outputs() == (CH, 5)


def test_rate_first_second_is_real_rate():
    rig = Rig()
    rig.stream(1.0)
    assert 49 <= rig.r.snapshot()["rate_hz"] <= 51     # поток моложе окна 2 с: не занижается вдвое


@pytest.mark.parametrize("timeout_ms", [500, 200, 1000])
def test_frames_stop_failsafe_within_timeout_plus_100(timeout_ms):
    rig = Rig(timeout_ms)
    rig.stream(2)
    t_last = rig.clock() - rig.r.snapshot()["age_ms"] / 1000      # время последнего кадра
    t_fs = rig.idle(2)
    assert t_fs is not None
    dt_ms = (t_fs - t_last) * 1000
    assert timeout_ms < dt_ms <= timeout_ms + 100, dt_ms
    s = rig.r.snapshot()
    assert (s["state"], s["reason"], s["failsafes"]) == ("FAILSAFE", "timeout", 1)
    assert s["ch"] == [0] * 8 and s["btn"] == 0 and rig.r.outputs() == ((0,) * 8, 0) and s["output"] == "none"
    assert rig.logs[-1].startswith("ACTIVE -> FAILSAFE (timeout) n=")


def test_exact_timeout_boundary():
    rig = Rig(500)
    rig.r.on_rc(frame(1))
    rig.clock.advance(0.5)
    rig.r.tick()
    assert rig.r.state() == "ACTIVE"                  # ровно timeout — ещё свежо
    rig.clock.advance(0.001)
    rig.r.tick()
    assert rig.r.state() == "FAILSAFE"


def test_resume_after_timeout_back_to_active():
    rig = Rig()
    rig.stream(1)
    rig.idle(1)
    assert rig.r.state() == "FAILSAFE"
    rig.stream(0.5)
    s = rig.r.snapshot()
    assert (s["state"], s["reason"]) == ("ACTIVE", "fresh") and s["ch"] == list(CH)
    assert s["late_max_ms"] >= 1000 and s["failsafes"] == 1 and s["gaps"] == 0
    assert [ln.split(" n=")[0] for ln in rig.logs] == ["IDLE -> ACTIVE (fresh)", "ACTIVE -> FAILSAFE (timeout)",
                                                     "FAILSAFE -> ACTIVE (fresh)"]


def test_link_lost_immediate_and_numbering_restarts():
    rig = Rig()
    rig.stream(1)
    rig.r.on_link_lost()
    s = rig.r.snapshot()
    assert (s["state"], s["reason"], s["failsafes"]) == ("FAILSAFE", "link-lost", 1) and s["ch"] == [0] * 8
    assert s["n"] == 0
    rig.idle(1)                                       # причина первая: timeout её не заменяет
    assert rig.r.snapshot()["reason"] == "link-lost" and rig.r.snapshot()["failsafes"] == 1
    rig.n = 0                                         # новый сеанс — номера с 1
    rig.stream(0.2)
    s = rig.r.snapshot()
    assert s["state"] == "ACTIVE" and s["n"] == rig.n and s["stale"] == 0 and s["gaps"] == 0


def test_link_lost_in_idle_stays_idle():
    rig = Rig()
    rig.r.on_link_lost()
    assert rig.r.state() == "IDLE" and rig.r.snapshot()["reason"] == "start" and rig.logs == []
    rig.stream(0.5)
    rig.stream(0.04, en=False)
    rig.r.on_link_lost()
    assert (rig.r.state(), rig.r.snapshot()["reason"]) == ("IDLE", "disabled")


def test_disabled_is_idle_not_failsafe():
    rig = Rig()
    rig.stream(1)
    rig.stream(0.02, en=False)
    s = rig.r.snapshot()
    assert (s["state"], s["reason"], s["failsafes"]) == ("IDLE", "disabled", 0) and s["ch"] == [0] * 8
    rig.idle(3)                                       # после выключения кадров нет — сторож не срабатывает
    assert rig.r.state() == "IDLE" and rig.r.snapshot()["failsafes"] == 0
    rig.stream(0.5)                                   # снова включено оператором
    s = rig.r.snapshot()
    assert s["state"] == "ACTIVE" and s["late_max_ms"] <= 20      # пауза выключения — не «опоздание»
    assert rig.logs[1].startswith("ACTIVE -> IDLE (disabled)")


def test_disabled_from_failsafe():
    rig = Rig()
    rig.stream(0.5)
    rig.idle(1)
    rig.stream(0.02, en=False)
    assert (rig.r.state(), rig.r.snapshot()["reason"]) == ("IDLE", "disabled")


def test_device_lost_failsafe_then_recovery():
    rig = Rig()
    rig.stream(0.5)
    rig.stream(0.02, ok=False)
    s = rig.r.snapshot()
    assert (s["state"], s["reason"], s["failsafes"]) == ("FAILSAFE", "device-lost", 1)
    assert s["ch"] == [0] * 8 and s["btn"] == 0 and s["dev"]["ok"] is False
    rig.idle(1)
    assert rig.r.snapshot()["reason"] == "device-lost"
    rig.stream(0.02, en=False)
    assert rig.r.state() == "IDLE"
    rig.stream(0.2)
    assert rig.r.state() == "ACTIVE" and rig.r.snapshot()["dev"]["ok"] is True


def test_stale_and_gaps():
    rig = Rig()
    r = rig.r
    assert r.on_rc(frame(1)) and r.on_rc(frame(2))
    assert not r.on_rc(frame(2)) and not r.on_rc(frame(1))
    assert r.on_rc(frame(5)) and r.on_rc(frame(6))
    s = r.snapshot()
    assert s["stale"] == 2 and s["gaps"] == 2 and s["n"] == 6
    assert not r.on_rc(frame(3, ch=(1000,) * 8))      # устаревший кадр не исполняется
    assert r.outputs() == (CH, 5)


def test_bad_counter_and_log_rate_limit():
    rig = Rig()
    for _ in range(30):
        rig.r.on_bad("n must be an integer 1…2^31-1")
        rig.clock.advance(0.5)
    assert rig.r.snapshot()["bad"] == 30
    bad = [ln for ln in rig.logs if ln.startswith("bad rc frame")]
    assert len(bad) == 2 and bad[0] == "bad rc frame: n must be an integer 1…2^31-1 (bad=1)" and "(bad=21)" in bad[1]
    assert rig.r.state() == "IDLE"


def test_late_max_window_10s_and_rate_decays():
    rig = Rig()
    rig.stream(1)
    rig.clock.advance(0.3)                            # одна пауза 300 мс (меньше таймаута)
    rig.stream(1)
    assert 300 <= rig.r.snapshot()["late_max_ms"] <= 320 and rig.r.state() == "ACTIVE"
    rig.stream(10)
    assert rig.r.snapshot()["late_max_ms"] == 20     # окно 10 с
    rig.idle(1)
    assert 20 <= rig.r.snapshot()["rate_hz"] <= 26
    rig.idle(1.1)
    assert rig.r.snapshot()["rate_hz"] == 0.0


def test_since_s_and_health():
    rig = Rig()
    rig.stream(1)
    rig.clock.advance(0.3)
    h = rig.r.health()
    assert h["state"] == "ACTIVE" and h["frames"] == 50 and h["transitions"] == 1 and h["output"] == "none"
    assert h["device"] == "OpenTX Radiomaster Boxer Joystick" and h["device_ok"] is True and "ch" not in h
    assert 1.2 <= rig.r.snapshot()["since_s"] <= 1.4


def test_output_none_always_and_timeout_bounds():
    rig = Rig()
    for kw in ({}, {"en": False}, {"ok": False}):
        rig.stream(0.1, **kw)
        assert rig.r.snapshot()["output"] == "none"
    for bad in (0, 49, 10001):
        with pytest.raises(ValueError):
            ControlReceiver(SimClock(), timeout_ms=bad)


def test_deadline_from_last_accepted_frame_only():
    """deadline() — срок команд ACTIVE: последний принятый кадр + timeout_ms. tick(), устаревший и неверный кадр его
    не продлевают; вне ACTIVE (start, disabled, device-lost, link-lost, timeout) — None."""
    g = Rig(timeout_ms=500)
    r = g.r
    assert r.deadline() is None
    g.n += 1
    r.on_rc(frame(g.n))
    t_rx = g.clock()
    assert r.deadline() == pytest.approx(t_rx + 0.5)
    g.clock.advance(0.2)
    r.tick()
    assert r.on_rc(frame(g.n)) is False             # повтор номера — stale
    r.on_bad("junk")
    assert r.deadline() == pytest.approx(t_rx + 0.5)
    g.n += 1
    r.on_rc(frame(g.n))
    assert r.deadline() == pytest.approx(g.clock() + 0.5)
    g.clock.advance(0.51)
    r.tick()
    assert r.state() == "FAILSAFE" and r.deadline() is None
    for kw in ({"en": False}, {"ok": False}):
        g.n += 1
        r.on_rc(frame(g.n))
        assert r.deadline() is not None
        g.n += 1
        r.on_rc(frame(g.n, **kw))
        assert r.deadline() is None
    g.n += 1
    r.on_rc(frame(g.n))
    r.on_link_lost()
    assert r.deadline() is None
