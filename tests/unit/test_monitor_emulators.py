# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Эмуляторы и монитор: воспроизводимые сценарии неисправностей (L2/L3 на эмуляторах).

Проверяется логика ПО. Электрические и тепловые свойства реального оборудования здесь не проверяются.
"""
import pytest
import yaml

from sheregesh import PROTOCOL_VERSION
from sheregesh.config import console_defaults
from sheregesh.faults import CATALOG, render
from sheregesh.protocol import negotiate
from sheregesh_emulators import Bench, Scenario, Step

DEFAULTS = console_defaults()
DIAG = yaml.safe_load(DEFAULTS["diagnostics.yaml"])
LINK = yaml.safe_load(DEFAULTS["link.yaml"])


@pytest.fixture
def bench():
    b = Bench(DIAG, LINK)
    for _ in range(4):
        b.step()
    return b


def run(bench, seconds):
    for _ in range(int(seconds / 0.5)):
        bench.step()
    return bench.snapshot


def codes(snap):
    return [e.code for e in snap.active]


def first_time(bench, code, limit_s):
    for _ in range(int(limit_s / 0.5)):
        snap = bench.step()
        if code in codes(snap):
            return snap.t
    return None


def test_nominal_state(bench):
    snap = run(bench, 30)
    assert snap.overall == "normal" and snap.active == []
    assert snap.peer_state == "connected"
    assert 30 <= snap.fan_duty < 60
    assert bench.indicator.state == "normal"


# ---------------- вентилятор (REQ-P-005, логика) ----------------
@pytest.mark.test_id("TEST-P-005")
def test_fan_speed_follows_temperature(bench):
    low = run(bench, 20)
    bench.thermal.set_load(12.0)
    bench.thermal.set_ambient(35.0)
    high = run(bench, 60)
    assert high.temperatures["soc"] > low.temperatures["soc"]
    assert high.fan_duty > low.fan_duty
    assert high.fan_rpm > low.fan_rpm


@pytest.mark.test_id("TEST-P-005")
def test_tach_loss_warns_within_10s_and_forces_full_speed(bench):
    t0 = bench.clock()
    bench.fan.set_fault("tach")
    t = first_time(bench, "FAN-TACH", 10)
    assert t is not None and t - t0 <= 10
    assert bench.snapshot.fan_duty == 100 and bench.fan.airflow() == 1.0
    bench.fan.set_fault(None)
    assert "FAN-TACH" not in codes(run(bench, 2))


@pytest.mark.test_id("TEST-P-005")
def test_pwm_loss_detected_and_fan_runs_full(bench):
    bench.fan.set_fault("pwm")
    snap = run(bench, 3)
    assert "FAN-CTRL" in codes(snap)
    assert snap.fan_rpm >= 0.9 * snap.fan_nominal


@pytest.mark.test_id("TEST-P-005")
def test_stalled_fan_and_overheat_request_shutdown(bench):
    bench.fan.set_fault("stall")
    bench.thermal.set_load(12.0)
    at_request = None
    for _ in range(240):
        snap = bench.step()
        if snap.shutdown_requested and at_request is None:
            at_request = snap.temperatures["soc"]
    assert "FAN-STALL" in codes(snap)
    assert any(e.code == "SYS-SHUTDOWN-REQ" for e in bench.monitor.log.events)
    # В эмуляции устройство после запроса не выключается и продолжает греться; важен момент запроса.
    assert at_request is not None and at_request < 85, "выключение должно быть запрошено до 85 °C"


def test_missing_temperature_data_is_failsafe(bench):
    bench.thermal.set_available(False)
    snap = run(bench, 1)
    assert snap.temperatures is None and "TEMP-NODATA" in codes(snap) and snap.fan_duty == 100


def test_critical_temperature(bench):
    bench.fan.set_fault("pwm")      # обороты максимальны, но нагрев сильнее
    bench.thermal.set_load(20.0)
    bench.thermal.set_ambient(40.0)
    snap = run(bench, 120)
    assert {"TEMP-HIGH", "TEMP-CRIT"} <= set(codes(snap)) and snap.overall == "fault"


# ---------------- питание (REQ-PWR-005/007, логика) ----------------
@pytest.mark.test_id("TEST-PWR-007")
def test_unavailable_power_telemetry_shows_no_data(bench):
    bench.power.set_available(False)
    snap = run(bench, 1)
    assert snap.power is None, "нет выдуманных значений"
    assert "PWR-NODATA" in codes(snap)
    bench.power.set_available(True)
    snap = run(bench, 1)
    assert snap.power is not None and "PWR-NODATA" not in codes(snap)


@pytest.mark.test_id("TEST-PWR-007")
def test_power_values_match_emulator(bench):
    bench.power.set_percent(42.0)
    snap = run(bench, 0.5)
    assert abs(snap.power.percent - bench.power.percent) < 0.01


def test_source_change_logged(bench):
    bench.power.switch_source("battery")
    run(bench, 1)
    assert [e.params["source"] for e in bench.monitor.log.events if e.code == "PWR-SOURCE"] == ["battery"]


@pytest.mark.test_id("TEST-PWR-005")
def test_battery_thresholds_and_low_signal(bench):
    bench.power.switch_source("battery")
    bench.power.set_percent(15)
    assert "PWR-LOW" in codes(run(bench, 1))
    bench.power.set_percent(8)
    snap = run(bench, 1)
    assert "PWR-CRIT" in codes(snap) and "PWR-LOW" not in codes(snap)
    assert snap.shutdown_requested is None
    bench.power.set_percent(4)      # сигнал модуля «низкий заряд»
    snap = run(bench, 1)
    assert snap.shutdown_requested == "PWR-LOW-SIGNAL"
    bench.power.switch_source("external")
    assert not {"PWR-LOW", "PWR-CRIT"} & set(codes(run(bench, 1)))


# ---------------- второе устройство (REQ-SW-003/004, SYS-002 логика) ----------------
@pytest.mark.test_id("TEST-SW-004")
def test_cable_disconnect_detected_within_5s_and_recovers(bench):
    t0 = bench.clock()
    bench.peer.disconnect()
    t = first_time(bench, "PEER-LOST", 5)
    assert t is not None and t - t0 <= 5
    t1 = bench.clock()
    bench.peer.reconnect()
    for _ in range(20):
        if bench.step().peer_state == "connected":
            break
    assert bench.snapshot.peer_state == "connected" and bench.clock() - t1 <= 10


@pytest.mark.test_id("TEST-SW-004")
def test_silent_peer_detected_by_heartbeat_timeout(bench):
    t0 = bench.clock()
    bench.peer.set_responding(False)
    t = first_time(bench, "PEER-LOST", 10)
    assert t is not None and LINK["heartbeat_timeout_s"] <= t - t0 <= 5


MATRIX = {"protocol": {"supported": {1: {"min_peer_minor": 1}}}, "software": {"rejected": ["0.0.9"]},
          "hardware": {"accepted_prefixes": ["HW-1.0"]}}


@pytest.mark.test_id("TEST-SW-003")
@pytest.mark.parametrize("local,peer,sw,hw,ok,session,reason", [
    ("1.0", "1.1", None, None, True, "1.0", "ok"), ("1.2", "1.1", None, None, True, "1.1", "ok"),
    ("1.1", "1.5", None, None, True, "1.1", "ok"), ("1.3", "1.3", "0.1.0", "HW-1.0 Rev A", True, "1.3", "ok"),
    ("1.1", "1.0", None, None, False, None, "minor"), ("1.0", "2.1", None, None, False, None, "major"),
    ("2.1", "2.1", None, None, False, None, "major"), ("1.1", "0.9", None, None, False, None, "major"),
    ("1.1", "1.1", "0.0.9", None, False, None, "software"), ("1.1", "1.1", None, "HW-2.0", False, None, "hardware"),
    ("1.1", "", None, None, False, None, "bad-version"), ("1.1", None, None, None, False, None, "bad-version"),
    ("1.1", "1", None, None, False, None, "bad-version"), ("1.1", "1.1.0", None, None, False, None, "bad-version"),
    ("1.1", "a.b", None, None, False, None, "bad-version"), ("x", "1.1", None, None, False, None, "bad-version")])
def test_protocol_negotiation_rule(local, peer, sw, hw, ok, session, reason):
    n = negotiate(local, peer, sw, hw, matrix=MATRIX)
    assert (n.compatible, n.session, n.reason) == (ok, session, reason)


def test_shipped_matrix_accepts_current_protocol():
    assert negotiate(PROTOCOL_VERSION, PROTOCOL_VERSION, "0.1.0.dev0", "HW-1.0 Rev A (эмулятор)").compatible


@pytest.mark.test_id("TEST-SW-003")
def test_incompatible_peer_raises_error(bench):
    bench.peer.set_protocol("2.0")
    snap = run(bench, 1)
    assert snap.peer_state == "incompatible" and "PEER-INCOMPAT" in codes(snap)
    ev = next(e for e in snap.active if e.code == "PEER-INCOMPAT")
    assert ev.params == {"peer": "2.0", "local": PROTOCOL_VERSION, "reason": "major"}


# ---------------- индикатор и каталог (REQ-UI-003/004, логика) ----------------
@pytest.mark.test_id("TEST-UI-004")
def test_indicator_follows_worst_active_event(bench):
    assert bench.indicator.history[0][1] == "boot"
    assert bench.indicator.state == "normal"
    bench.events.inject("EMU-TEST", detail="x")
    run(bench, 0.5)
    assert bench.indicator.state == "warning" and bench.indicator.color() == ("amber", "blink")
    bench.peer.set_protocol("2.0")
    run(bench, 0.5)
    assert bench.indicator.state == "fault" and bench.indicator.color() == ("red", "steady")
    bench.peer.set_protocol("1.0")
    bench.events.clear("EMU-TEST")
    run(bench, 0.5)
    assert bench.indicator.state == "normal"


@pytest.mark.test_id("TEST-UI-003")
@pytest.mark.parametrize("code", sorted(CATALOG))
def test_every_catalog_entry_has_ru_en_text_and_action(code):
    f = CATALOG[code]
    for lang in ("ru", "en"):
        assert f.text[lang].strip() and f.action[lang].strip()
    params = {"sensor": "soc", "value": 71.2, "limit": 70, "rpm": 5000, "duty": 40, "percent": 15, "source": "battery",
              "timeout": 3, "peer": "2.0", "local": "1.0", "reason": "major", "age": 2.0, "count": 1, "code": "TEMP-HIGH", "codes": "TEMP-HIGH", "detail": "x", "revision": "r000002", "active": "r000001", "reason": "x"}
    for lang in ("ru", "en"):
        text, action = render(code, lang, params)
        assert "{" not in text and "{" not in action


@pytest.mark.test_id("TEST-UI-003")
@pytest.mark.parametrize("code", sorted(c for c, f in CATALOG.items() if f.latched))
def test_every_latched_fault_can_be_injected_and_cleared(bench, code):
    bench.events.inject(code)
    assert code in codes(run(bench, 0.5)) or code in {e.code for e in bench.monitor.log.events}
    bench.events.clear(code)


# ---------------- детерминированность ----------------
def test_same_scenario_gives_identical_results():
    sc = Scenario("faults", duration=60, steps=[
        Step(5, "fan", "set_fault", args=("tach",)), Step(10, "thermal", "set_load", args=(10.0,)),
        Step(20, "peer", "disconnect"), Step(30, "power", "set_available", args=(False,)),
        Step(40, "fan", "set_fault", args=(None,)), Step(45, "peer", "reconnect")])

    def trace():
        b = Bench(DIAG, LINK)
        out = []
        sc.runner(b).run_until(60, lambda s: out.append((s.t, s.temperatures and s.temperatures["soc"], s.fan_duty,
                                                          tuple(codes(s)), s.indicator)))
        return out, [(e.t, e.code, e.active) for e in b.monitor.log.events]

    assert trace() == trace()


def test_emulators_contain_no_out_of_scope_functionality():
    """Эмуляторы не реализуют управление аппаратами, радио и внешними исполнительными устройствами.

    Перечень запрещённых признаков берётся из сканера (единый источник); дополнительно — отсутствие
    сетевых и последовательных интерфейсов: эмуляторы работают только в памяти процесса.
    """
    import ast
    import inspect
    import sys
    from pathlib import Path

    import sheregesh_emulators.devices as dev
    import sheregesh_emulators.scenario as scn
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/security"))
    import scan
    for module in (dev, scn):
        src = inspect.getsource(module)
        assert not scan.SCOPE_PATTERNS.search(src)
        imported = {n.names[0].name.split(".")[0] for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import)}
        imported |= {(n.module or "").split(".")[0] for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom)}
        assert not imported & {"socket", "serial", "ssl", "http", "urllib", "subprocess", "gpiozero", "smbus", "spidev"}
