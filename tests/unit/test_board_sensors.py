# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Датчики платы (sheregesh.board, REQ-PWR-007, REQ-R-007): температура SoC — из thermal_zone, питание без модуля —
«нет данных», а не модельные значения эмулятора; без датчика — эмуляторы."""
from sheregesh import board
from sheregesh_emulators.scenario import Bench

DIAG = {"temp_warning_c": 70, "temp_critical_c": 80, "battery_warning_pct": 20, "battery_critical_pct": 10}
LINK = {"heartbeat_timeout_s": 3}


def zone(tmp_path, text):
    (tmp_path / "temp").write_text(text)
    return tmp_path


def test_soc_temperature_from_thermal_zone(tmp_path):
    assert board.SocTemperature(zone(tmp_path, "66218\n")).read() == {"soc": 66.2}
    assert board.SocTemperature(zone(tmp_path, "garbage")).read() is None
    assert board.SocTemperature(tmp_path / "missing").read() is None


def test_sensors_only_on_board(tmp_path):
    assert board.sensors(tmp_path / "missing") is None
    temps, power = board.sensors(zone(tmp_path, "50000"))
    assert temps.read() == {"soc": 50.0} and power.read() is None


def test_bench_with_board_sensors_reports_real_temperature_and_no_power(tmp_path):
    b = Bench(DIAG, LINK, sensors=board.sensors(zone(tmp_path, "72500")))
    s = b.step()
    assert s.temperatures == {"soc": 72.5} and s.power is None
    active = {e.code for e in b.monitor.log.active()}
    assert {"TEMP-HIGH", "PWR-NODATA"} <= active and "TEMP-NODATA" not in active


def test_remote_status_uses_board_sensors_and_omits_power(tmp_path):
    from sheregesh_remote.agent import Agent
    a = object.__new__(Agent)
    a.alerts, a.stats, a.module, a.conn, a.services_snap = [], {}, None, None, {}
    a.now, a.video_status = (lambda: 1.0), (lambda: {})
    a.sensors = board.sensors(zone(tmp_path, "64000"))
    body = a.status_body()
    assert body["temperatures"] == {"soc": 64.0} and body["power"] is None
    from sheregesh.link.client import _sanitize_status
    assert "power" not in _sanitize_status(body)               # пульт: «нет данных», не 0 и не прежнее значение
