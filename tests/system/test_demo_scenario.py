# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Демонстрационный сценарий S1 целиком (L3: эмуляторы + конфигурация + монитор, без GUI)."""
import yaml

from sheregesh.config import ConfigStore, Schema, console_defaults
from sheregesh_emulators import Bench
from sheregesh_emulators.demo import DEMO


def make_bench(store):
    data = store.active().data
    return Bench(data["diagnostics.yaml"], data["link.yaml"])


def test_demo_checkpoints(tmp_path):
    store = ConfigStore(tmp_path / "cfg", Schema.load())
    store.initialize(console_defaults())
    store.recover()
    bench = make_bench(store)
    runner = DEMO.runner(bench)
    seen = {}

    def at(label):
        return runner.run_until(DEMO.checkpoints[label])

    # 1–2. старт и штатное состояние
    s = at("start")
    assert s.overall == "normal" and s.active == [] and s.peer_state == "connected"
    seen["start"] = s.temperatures["soc"]
    # 3. показания синтетических датчиков меняются
    s = at("heating")
    assert s.temperatures["soc"] > seen["start"] + 15
    assert s.fan_duty > 80 and s.power.source == "battery"
    # 4–5. диагностическое событие и предупреждение
    s = at("warning")
    assert [e.code for e in s.active] == ["TEMP-HIGH"] and s.indicator == "warning"
    s = at("peer-lost")
    assert {"TEMP-HIGH", "PEER-LOST"} == {e.code for e in s.active} and s.peer_state == "lost"
    # 6. восстановление нормального состояния
    s = at("recovered")
    assert s.active == [] and s.overall == "normal" and s.indicator == "normal" and s.power.source == "external"
    log = [(e.code, e.active) for e in bench.monitor.log.events]
    assert ("TEMP-HIGH", False) in log and ("PEER-LOST", False) in log and ("EMU-TEST", False) in log
    assert [st for _, st in bench.indicator.history] == ["boot", "normal", "warning", "normal", "warning", "normal"]

    # 7. сохранение и повторная загрузка настроек
    files = dict(store.active().files)
    files["system.yaml"] = files["system.yaml"].replace("language: ru", "language: en")
    files["diagnostics.yaml"] = files["diagnostics.yaml"].replace("temp_warning_c: 70", "temp_warning_c: 65")
    assert store.apply(files, lambda rev, services: True).ok
    reloaded = ConfigStore(store.root, Schema.load())
    reloaded.recover()
    data = reloaded.active().data
    assert data["system.yaml"]["language"] == "en" and data["diagnostics.yaml"]["temp_warning_c"] == 65
    # новый порог действует на монитор, перезапущенный с перезагруженной конфигурацией
    b2 = make_bench(reloaded)
    r2 = DEMO.runner(b2)
    hits = []
    r2.run_until(90, lambda s: hits.append(s.t) if "TEMP-HIGH" in [e.code for e in s.active] and not hits else None)
    assert hits and hits[0] < 53.5


def test_demo_is_deterministic():
    def trace():
        d = console_defaults()
        b = Bench(yaml.safe_load(d["diagnostics.yaml"]), yaml.safe_load(d["link.yaml"]))
        out = []
        DEMO.runner(b).run_until(DEMO.duration, lambda s: out.append(
            (s.t, s.temperatures["soc"], s.fan_duty, s.power.percent, tuple(e.code for e in s.active))))
        return out
    assert trace() == trace()
