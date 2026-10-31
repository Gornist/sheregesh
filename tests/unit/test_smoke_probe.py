# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Smoke Test (tools/probe/smoke.py, TEST-SMOKE-P/R/S): протокол по пунктам реестра, ручные пункты — только от
оператора, итог набора PASS лишь при всех пунктах PASS. Без плат: ssh и файлы состояния пульта подменены."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "probe"))
import smoke  # noqa: E402

PEER = {"proto": "1.5", "software": "0.1.0.dev0", "hardware": "HW-1.0 Rev A (Raspberry Pi 4 Model B Rev 1.5)",
        "device_id": "remote-100000009d497e91"}
INFO = {"package": "0.1.0.dev0", "hardware": PEER["hardware"], "device_id": PEER["device_id"]}
SERVICES = [{"side": s, "name": n, "state": "active", "restarts": 0}
            for s, names in smoke.MANDATORY.items() for n in names]


@pytest.fixture
def stand(monkeypatch):
    st = {"t": 1000.0, "soc": 61.0, "sensor": "62300", "services": list(SERVICES), "system": "running",
          "failed": "0", "events": [], "stopped": False, "fps": 56.6}
    monkeypatch.setattr(smoke.time, "time", lambda: st["t"])
    monkeypatch.setattr(smoke.time, "monotonic", lambda: st["t"])
    monkeypatch.setattr(smoke.time, "sleep", lambda s: st.__setitem__("t", st["t"] + s))
    monkeypatch.setattr(smoke.cvl.time, "monotonic", lambda: st["t"])
    monkeypatch.setattr(smoke.cvl.time, "sleep", lambda s: st.__setitem__("t", st["t"] + s))

    def ssh(host, cmd, timeout=120):
        if cmd.startswith("systemctl is-system-running"):
            return f"{st['system']}\n3600\n{st['failed']}"
        if cmd.startswith("systemctl is-active sheregesh-console"):
            return "active\ncage"
        if cmd == "sheregesh-info --json":
            return json.dumps(INFO)
        if "thermal_zone0" in cmd:
            return st["sensor"]
        if "MainPID" in cmd:
            return "4242"
        if "kill -STOP" in cmd:
            st["stopped"] = True
            st["events"].append({"id": 900, "code": "PEER-LOST", "active": True})
            return f"{st['t']}\nok"
        if "kill -CONT" in cmd:
            st["stopped"] = False
            return f"{st['t']}\nok"
        if cmd.startswith("readlink"):
            return "releases/x"
        return ""

    def jread(host, path):
        if path == smoke.UI:
            return {"time": st["t"] - 0.4, "services": st["services"], "events": st["events"],
                    "video": {"mode": "remote-srtp", "state": "playing", "decoded_fps_5s": st["fps"]}}
        return {"phase": "lost" if st["stopped"] else "connected", "session": "1.5", "peer": PEER,
                "telemetry": {"temperatures": {"soc": st["soc"]}, "video": {"source": {"input": "hdmi"}}}}
    monkeypatch.setattr(smoke.cvl, "ssh", ssh)
    monkeypatch.setattr(smoke.cvl, "jread", jread)
    monkeypatch.setattr(smoke.cvl, "cv", lambda ch: jread(ch, smoke.UI)["video"])
    monkeypatch.setattr(smoke.cvl, "sc_telemetry", lambda ch, rh, window: {
        "verdict": "PASS", "metrics": {"seconds": window, "messages_per_s": 2.2, "seq_gaps": 0, "events_sent": 9,
                                       "event_loss": 0}})
    return st


def test_auto_items_pass_and_manual_items_stay_manual(stand):
    res = smoke.run("c", "r", ["P", "R", "S"], {}, exchange_s=300)
    auto = {i for i, _, _, how in smoke.ITEMS if how == "auto"}
    assert all(res[i]["verdict"] == "PASS" for i in auto), {i: res[i] for i in auto}
    assert all(res[i]["verdict"] == "MANUAL" for i, _, _, how in smoke.ITEMS if how != "auto")
    by = smoke.summary(res, ["P", "R", "S"])
    assert all(v["verdict"] == "FAIL" for v in by.values())                   # без оператора — не PASS
    assert by["TEST-SMOKE-P"]["total"] == 7 and by["TEST-SMOKE-R"]["total"] == 6 and by["TEST-SMOKE-S"]["total"] == 5


def test_operator_answers_complete_the_protocol(stand):
    answers = {i: "PASS" for i, _, _, how in smoke.ITEMS if how != "auto"}
    res = smoke.run("c", "r", ["P", "R", "S"], answers)
    by = smoke.summary(res, ["P", "R", "S"])
    assert all(v["verdict"] == "PASS" for v in by.values()), by
    answers["P3"] = "FAIL: Esc не закрывает диалог"
    res = smoke.run("c", "r", ["P"], answers)
    assert res["P3"]["verdict"] == "FAIL" and "Esc" in res["P3"]["detail"]
    assert smoke.summary(res, ["P"])["TEST-SMOKE-P"]["verdict"] == "FAIL"


def test_auto_fail_is_not_overridden_by_operator(stand):
    stand["system"] = "degraded"
    res = smoke.run("c", "r", ["P"], {"P1": "PASS"})
    assert res["P1"]["verdict"] == "FAIL" and res["P1"]["operator"]["verdict"] == "PASS"


def test_remote_temperature_must_match_board_sensor(stand):
    stand["soc"] = 55.0                      # эмулированное значение при датчике 62,3 °C
    r = smoke.check_r4("c", "r")
    assert r["verdict"] == "FAIL" and "температура SoC" in r["detail"]
    stand["soc"] = 60.1
    assert smoke.check_r4("c", "r")["verdict"] == "PASS"


def test_service_down_on_remote_fails_r2(stand):
    stand["services"] = [s if s["name"] != "video-tx" else {**s, "state": "failed"} for s in SERVICES]
    r = smoke.check_r2("c", "r")
    assert r["verdict"] == "FAIL" and "video-tx: failed" in r["detail"]
    assert smoke.check_p4("c", "r")["verdict"] == "PASS"


def test_low_fps_fails_s3(stand):
    stand["fps"] = 19.0
    assert smoke.check_s3("c", "r")["verdict"] == "FAIL"


def test_fault_injection_resumes_remote_and_sees_event(stand):
    r = smoke.check_s5("c", "r")
    assert r["verdict"] == "PASS" and not stand["stopped"]


def test_protocol_markdown_lists_every_item(stand, tmp_path):
    res = smoke.run("c", "r", ["P", "R", "S"], {})
    md = smoke.protocol_md({"time": "t", "console": "c", "remote": "r"}, res, smoke.summary(res, ["P", "R", "S"]))
    assert all(f"| {i} |" in md for i, _, _, _ in smoke.ITEMS)
    assert "## TEST-SMOKE-P: FAIL" in md
