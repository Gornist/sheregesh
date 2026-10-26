# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Приёмка S7 ST (tools/probe/s7_sticks_accept.py, ADR-0013 §5): суждения без плат — разбор вывода опросчика пульта,
журнала блока и выборок link-status, вердикты ST-01…ST-09, перцентили задержки, опросчик против подставного канала
пульта, сверка путей полей с настоящими SticksView/ControlReceiver, команды — в пределах правила polkit образа, план
без SSH, предпроверка без действий, переиспользование s6_jr_accept (start_guard, preflight, judge_ota).

Номера ST — критерии спринта, а не TEST-ID реестра: маркер test_id не ставится."""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "probe"))
sys.path.insert(0, str(ROOT / "tools" / "deploy" / "ota-check"))
import s7_sticks_accept as s  # noqa: E402

CH = [0, -12, 1000, 3, -1000, 0, 0, 1000]


def remote(state="ACTIVE", reason="fresh", n=10, rx=1.0, lat=15.0, ch=CH, btn=5, rate=50.0, gaps=0, fs=0):
    return {"v": 1, "state": state, "reason": reason, "since_s": 5.0, "n": n, "age_ms": 12, "rate_hz": rate,
            "gaps": gaps, "stale": 0, "bad": 0, "late_max_ms": 30, "failsafes": fs, "timeout_ms": 500, "ch": list(ch),
            "btn": btn, "dev": {"name": "OpenTX Radiomaster Boxer Joystick", "ok": True}, "output": "none",
            "rx_at": rx, "rx_age_s": 0.01, "latency_ms": lat}


def sticks(enabled=True, present=True, axes=CH, buttons=5, rem=None, support="ok", **lat):
    return {"support": support, "present": present, "enabled": enabled,
            "device": {"name": "OpenTX Radiomaster Boxer Joystick", "path": "/dev/input/event4", "axes": 8,
                       "buttons": 24},
            "axes": list(axes), "buttons": buttons, "session": "1.4", "remote": rem,
            "sender": {"n": 1, "sent": 1, "last": "queued", "off_reason": ""},
            "latency": {"samples": 300, "window_s": 30.0, "source": "client", "last_ms": 15.0, "median_ms": 15.0,
                        "p99_ms": 40.0, "shown_ms": 15.0, **lat},
            "result": "ok", "notice": "", "display": {"stateText": "Команды идут"}}


def row(t, st, op="status", result="ok"):
    return {"t0": round(t - 0.004, 4), "t": t, "op": op, "r": {"result": result, "detail": "", "sticks": st}}


# ---------------------------------------------------------------- разбор и пути полей

def test_parse_rows_and_paths():
    text = "\n".join(["Traceback (most recent call last):", json.dumps(row(1.0, sticks())), "{broken",
                      json.dumps({"t": 2.0, "r": "x"}), json.dumps(row(2.0, sticks(rem=remote())))])
    rows = s.parse_rows(text)
    assert len(rows) == 2 and s.parse_rows("") == []
    st = s.statuses(rows)
    assert s.g(st[1][1], "rstate") == "ACTIVE" and s.g(st[0][1], "rstate") is None
    assert s.g(st[0][1], "axis_count") == 8 and s.g(st[0][1], "name").startswith("OpenTX")
    assert s.get({"a": {"b": 1}}, "a.b") == 1 and s.get({"a": 1}, "a.b") is None
    assert s.statuses(rows, 1.5) == [st[1]] and s.statuses([row(1, sticks(), result="busy")]) == []
    assert s.op_row([row(1, sticks(), op="enable")], "enable")["t"] == 1


def test_percentile_nearest_rank():
    v = list(range(1, 301))                                          # 1…300
    assert s.percentile(v, 99) == 297 and s.percentile(v, 50) == 150 and s.percentile(v, 100) == 300
    assert s.percentile([7], 99) == 7 and s.percentile([], 99) is None
    assert s.percentile([5, 1, 3], 0) == 1


def test_latency_samples_one_per_rc_state():
    sts = [(0.00, sticks(rem=remote(rx=1.0, lat=10))), (0.05, sticks(rem=remote(rx=1.0, lat=10))),   # повтор
           (0.10, sticks(rem=remote(rx=1.1, lat=12))), (0.15, sticks(rem=remote(state="IDLE", rx=1.2, lat=99))),
           (0.20, sticks(rem=remote(rx=1.3, lat=None))), (0.25, sticks(rem=None)),
           (0.30, sticks(rem={**remote(n=40, lat=14), "rx_at": None})),
           (0.35, sticks(rem={**remote(n=40, lat=14), "rx_at": None}))]
    assert s.latency_samples(sts) == [10.0, 12.0, 14.0]


def test_time_to_and_remote_is():
    rows = [row(100.0, sticks(enabled=True), op="enable"), row(100.06, sticks(rem=remote("IDLE", "start"))),
            row(100.31, sticks(rem=remote()))]
    assert s.time_to(rows, 100.0, s.remote_is("ACTIVE")) == pytest.approx(0.31)
    assert s.time_to(rows, 100.0, s.remote_is("ACTIVE", "timeout")) is None
    assert s.time_to(rows, 100.5, s.remote_is("ACTIVE")) is None


JOURNAL = """1761470000.100000 pi2 python3[812]: [remote] control: IDLE -> ACTIVE (fresh) n=1
1761470030.612345 pi2 python3[812]: [remote] control: ACTIVE -> FAILSAFE (link-lost) n=0 age_ms=14
1761470031.000000 pi2 python3[812]: [remote] module: READY
1761470040.500000 pi2 python3[812]: [remote] control: FAILSAFE -> ACTIVE (fresh) n=1 age_ms=0
garbage control: x"""


def test_parse_transitions_and_failsafe_delay():
    tr = s.parse_transitions(JOURNAL)
    assert [(x["from"], x["to"], x["reason"]) for x in tr] == [("IDLE", "ACTIVE", "fresh"),
                                                               ("ACTIVE", "FAILSAFE", "link-lost"),
                                                               ("FAILSAFE", "ACTIVE", "fresh")]
    assert tr[0]["age_ms"] is None and tr[1]["age_ms"] == 14 and tr[1]["t"] == pytest.approx(1761470030.612345)
    ctl = {"state": "FAILSAFE", "reason": "timeout", "since_s": 1.2, "age_ms": 1712}
    assert s.failsafe_delay(ctl, 1000.0, tr[1]) == (14, "журнал (age_ms перехода)")
    v, src = s.failsafe_delay(ctl, 1000.0, {"t": 998.8, "age_ms": None})
    assert v == 512 and "журнала" in src                              # 998.8 − (1000 − 1.712)
    v, src = s.failsafe_delay(ctl, 1000.0)
    assert v == 512 and "since_s" in src
    assert s.failsafe_delay({"state": "FAILSAFE"}, 1000.0) == (None, "")
    assert s.failsafe_delay({"age_ms": 5}, None) == (None, "")


def test_receiver_journal_line_is_parsed():
    """Сверка с настоящим ControlReceiver (агент R): его запись о переходе разбирается, age_ms — возраст кадра."""
    rx = pytest.importorskip("sheregesh.control.receiver")
    wire = pytest.importorskip("sheregesh.control.wire")
    lines, t = [], [0.0]
    r = rx.ControlReceiver(clock=lambda: t[0], log=lambda m: lines.append(f"{1000 + t[0]:.6f} pi2 python3[1]: "
                                                                          f"[remote] control: {m}"))
    r.on_rc(wire.check_rc({"n": 1, "en": True, "ch": CH, "btn": 5, "dev": {"name": "x", "ok": True}}), 1.0)
    t[0] = 1.53
    r.tick()
    tr = s.parse_transitions("\n".join(lines))
    assert [(x["to"], x["reason"]) for x in tr] == [("ACTIVE", "fresh"), ("FAILSAFE", "timeout")]
    assert tr[1]["age_ms"] == 530 and s.failsafe_delay(r.health(2.0), 1002.0, tr[1])[0] == 530
    h = r.health(2.0)                                    # link-status: age_ms растёт, since_s — от перехода
    assert s.failsafe_delay(h, 1002.0)[0] == pytest.approx(530, abs=50)
    assert set(r.snapshot()) >= {"state", "reason", "ch", "btn", "rate_hz", "gaps", "failsafes", "output", "n"}
    assert set(h) >= {"state", "reason", "since_s", "age_ms", "rate_hz", "gaps", "failsafes", "output"}
    assert wire.OUTPUT == s.OUTPUT
    assert {"timeout", "link-lost", "device-lost", "disabled"} <= set(wire.REASONS)


def test_sticks_view_snapshot_has_paths(qapp):
    """Сверка путей P с настоящим SticksView.snapshot() (агент J) и запросов — с командой sticks локального канала."""
    st = pytest.importorskip("sheregesh_console.sticks")
    ctl = pytest.importorskip("sheregesh_console.control")

    class Src:
        def state(self):
            return {**st.empty_state(), "present": True, "name": "Boxer", "path": "/dev/input/event4", "axes": CH,
                    "buttons": 5, "axis_count": 8, "button_count": 24, "generation": 1}

    class Client:
        def snapshot(self):
            return {"phase": "connected", "session": "1.4", "counters": {"connects": 1},
                    "rc_state": remote(rx=5.0), "rc_sent": {"n": 10, "at": 4.99, "pending": False, "ring": []}}

        def send_rc(self, _body):
            return "queued"

    v = st.SticksView(source=Src(), client=lambda: Client(), clock=lambda: 5.0)
    v.update_link("CONNECTED", "1.4")
    snap = v.snapshot()
    for k in ("support", "present", "enabled", "name", "axis_count", "button_count", "axes", "buttons", "rstate",
              "rreason", "rch", "rbtn", "rate", "gaps", "failsafes", "rn", "rx_at", "rlat", "routput", "lat_samples"):
        assert s.g(snap, k) is not None, k
    assert s.g(snap, "axis_count") == 8 and s.g(snap, "button_count") == 24 and s.g(snap, "rlat") == 15.0
    assert "median_ms" in snap["latency"] and "p99_ms" in snap["latency"]
    for k in ("present", "enabled", "deviceName"):                  # ui-state: плоский ключ sticks (app.py)
        assert k in (ROOT / "software/console/sheregesh_console/app.py").read_text()
    for name, req in s.REQUESTS.items():
        assert req["cmd"] == "sticks" and req["op"] == name and req["op"] in ctl.STICKS_OPS
        assert set(req) <= {"cmd", "op", "confirmed"}
    assert s.REQUESTS["enable"]["confirmed"] is True


def test_docs_freshness_and_window():
    rows = [(10.0, 1000.0, {"time": 999.5, "control": {"state": "ACTIVE", "since_s": 1.0}}),   # записан в 9.5
            (10.25, 1000.25, {"time": 999.5, "control": {"state": "ACTIVE", "since_s": 1.0}}),
            (12.0, 1002.0, {"time": 1001.6, "control": {"state": "ACTIVE", "since_s": 3.1, "rate_hz": 49.5}}),
            (12.1, 1002.1, {"time": 1001.6, "control": {"state": "ACTIVE", "since_s": 3.1, "rate_hz": 49.5}}),
            (12.2, 1002.2, {"time": 1002.1, "control": {"state": "FAILSAFE", "since_s": 0.0}}), (12.3, 1002.3, {})]
    assert len(s.fresh_docs(rows, 9.0)) == 5 and len(s.fresh_docs(rows, 10.0)) == 3
    assert len(s.distinct(s.fresh_docs(rows, 9.0))) == 3
    w = s.window_docs(s.fresh_docs(rows, 9.0))
    assert len(w) == 1 and w[0]["control"]["rate_hz"] == 49.5
    assert s.ctl_of({"control": None}) == {} and s.ui_sticks({}) == {}


# ---------------------------------------------------------------- суждения

def test_st01_device():
    ui = {"sticks": {"present": True, "deviceName": "Boxer", "enabled": False}}
    rep = {"result": "ok", "sticks": sticks(enabled=False)}
    v, d = s.judge_device(rep, ui)
    assert v == "PASS" and "8 осей, 24 кнопок" in d
    bad = {"result": "ok", "sticks": {**sticks(), "device": {"name": "", "axes": 6, "buttons": 16}}}
    v, d = s.judge_device(bad, ui)
    assert v == "FAIL" and "осей 6" in d and "кнопок 16" in d and "имя" in d
    v, d = s.judge_device({"result": "ok", "sticks": sticks(present=False, support="no-device")}, ui)
    assert v == "FAIL" and "no-device" in d
    assert "нет sticks" in s.judge_device(rep, {})[1]
    assert s.judge_device(rep, {"sticks": {"present": False}})[0] == "FAIL"
    assert s.judge_device({"result": "bad-request", "detail": "cmd: list | select | module"}, ui)[0] == "ERROR"
    assert s.judge_device({}, ui)[0] == "ERROR"


def test_st02_default_off():
    start = {"when": "старт прогона", "enabled": False, "ui_enabled": False, "block_state": "IDLE",
             "block_reason": "start", "require_idle": True}
    after = {"when": "после перезапуска пульта", "enabled": False, "ui_enabled": False, "block_state": "FAILSAFE",
             "block_reason": "link-lost", "require_idle": False}
    assert s.judge_default_off([start, after])[0] == "PASS"         # FAILSAFE link-lost после перезапуска — допустим
    v, d = s.judge_default_off([{**start, "block_state": "FAILSAFE", "block_reason": "device-lost"}])
    assert v == "FAIL" and "ожидался IDLE" in d
    assert s.judge_default_off([{**start, "enabled": True}])[0] == "FAIL"
    assert "на экране" in s.judge_default_off([{**start, "ui_enabled": True}])[1]
    assert s.judge_default_off([start, {**after, "block_state": "ACTIVE"}])[0] == "FAIL"
    assert s.judge_default_off([{**start, "block_state": None}])[0] == "FAIL"
    assert s.judge_default_off([{"when": "x", "error": "sticks status: 'busy'"}])[0] == "ERROR"
    assert s.judge_default_off([])[0] == "ERROR"


RATE = {"enable_result": "ok", "active_s": 0.21, "window_s": 29.9, "window_req": 30, "echo_rates": [49.5, 50.0],
        "echo_gaps": [0, 0], "block_rates": [49.8, 50.0], "block_gaps": [0, 0]}


def test_st03_rate():
    assert s.judge_rate(RATE)[0] == "PASS"
    for bad, text in (({"active_s": 1.3}, "ACTIVE через 1.3"), ({"active_s": None}, "ACTIVE через None"),
                      ({"echo_rates": [50.0, 44.0]}, "эхо: rate_hz от 44.0"), ({"block_rates": []}, "блок: нет rate"),
                      ({"block_gaps": [0, 2]}, "блок: gaps выросли на 2"), ({"echo_gaps": []}, "эхо: нет gaps"),
                      ({"enable_result": "no-device", "enable_detail": "no control device"}, "no-device")):
        v, d = s.judge_rate({**RATE, **bad})
        assert v == "FAIL" and text in d, (bad, d)
    assert s.judge_rate({**RATE, "block_gaps": [3, 3]})[0] == "PASS"   # gaps прошлых сеансов: важен рост за окно
    assert s.judge_rate({**RATE, "window_s": 12.0})[0] == "ERROR"
    assert s.judge_rate({**RATE, "window_s": 9.5, "window_req": 10})[0] == "PASS"
    assert s.judge_rate({**RATE, "enable_result": None})[0] == "ERROR"


def series(axes_list, rch_shift=0, btn=5, rbtn=5, state="ACTIVE"):
    return [(i * 0.05, sticks(axes=a, buttons=btn, rem=remote(state=state, ch=[x + rch_shift for x in a], btn=rbtn,
                                                                    rx=i * 0.1)))
            for i, a in enumerate(axes_list)]


def test_st04_echo():
    still = series([CH] * 20)
    e = s.echo_compare(still)
    assert e["stable"] == 18 and e["echo_max"] == 0 and e["spread"] == 0 and e["buttons"] == [5]
    assert s.judge_echo_rest(e)[0] == "PASS"
    assert s.judge_echo_rest(s.echo_compare(series([CH] * 20, rch_shift=1)))[0] == "PASS"     # ±1
    v, d = s.judge_echo_rest(s.echo_compare(series([CH] * 20, rch_shift=2)))
    assert v == "FAIL" and "до 2" in d
    v, d = s.judge_echo_rest(s.echo_compare(series([CH] * 20, rbtn=0)))
    assert v == "FAIL" and "btn блока" in d
    # шум ±3 по одной оси: эхо сравнивается только там, где оси не менялись 3 выборки подряд
    noisy = [CH[:1] + [CH[1] + (3 if (i // 4) % 2 else 0)] + CH[2:] for i in range(24)]
    rows = [(t, {**st, "remote": {**st["remote"], "ch": noisy[max(0, i - 1)]}})       # эхо отстаёт на выборку
            for i, (t, st) in enumerate(series(noisy))]
    e = s.echo_compare(rows)
    assert e["spread"] == 3 and e["stable"] > 0 and e["echo_max"] == 0
    moving = [[x + 50 * i for x in CH] for i in range(20)]
    assert s.judge_echo_rest(s.echo_compare(series(moving)))[0] == "ERROR"            # стики трогали
    assert s.judge_echo_rest(s.echo_compare(series([CH] * 20, state="FAILSAFE")))[0] == "FAIL"
    assert s.judge_echo_rest(s.echo_compare([]))[0] == "ERROR"
    v, d = s.judge_echo(e, None, manual=False)
    assert v == "PASS" and "--manual" in d


def test_st04_manual_poses():
    base = s.echo_compare(series([CH] * 20))
    up = [CH[:2] + [-1000] + CH[3:]] * 20
    ok = {"pose": "левый стик ВВЕРХ", "echo": s.echo_compare(series(up))}
    assert s.judge_pose(ok, base)[0] == "PASS"
    switch = {"pose": "SA", "echo": s.echo_compare(series([CH] * 20, btn=7, rbtn=7))}
    assert s.judge_pose(switch, base)[0] == "PASS"
    v, d = s.judge_pose({"pose": "x", "echo": base}, base)
    assert v == "FAIL" and "поза не принята" in d
    lag = {"pose": "x", "echo": s.echo_compare(series(up, rch_shift=5))}
    assert s.judge_pose(lag, base)[0] == "FAIL"
    assert s.judge_pose({"pose": "x", "echo": {}}, base)[0] == "ERROR"
    v, d = s.judge_echo(base, [ok, switch], manual=True)
    assert v == "PASS" and "ВВЕРХ" in d
    assert s.judge_echo(base, [ok, lag], manual=True)[0] == "FAIL"


def test_st05_latency():
    good = [10.0 + (i % 10) for i in range(300)] + [80.0]
    v, d = s.judge_latency(good, {"median_ms": 14.5, "p99_ms": 19.0, "samples": 300})
    assert v == "PASS" and "медиана 15.0" in d and "пульт: медиана 14.5" in d
    slow = [35.0] * 200
    v, d = s.judge_latency(slow)
    assert v == "FAIL" and "медиана 35.0" in d
    tail = [12.0] * 290 + [150.0] * 10                             # 10 из 300 > 100 мс: p99 = 297-я — 150
    v, d = s.judge_latency(tail)
    assert v == "FAIL" and "p99 150.0" in d
    assert s.judge_latency([12.0] * 297 + [150.0] * 3)[0] == "PASS"  # 1 % хвоста — ровно в пределе p99
    assert s.judge_latency([12.0] * 300, {"median_ms": 12.0, "p99_ms": 140.0})[0] == "FAIL"
    assert s.judge_latency([12.0] * 10)[0] == "ERROR"


STOP = {"stop_rc": 0, "failsafe_s": 0.4, "state": "FAILSAFE", "reason": "link-lost", "delay_ms": 14,
        "delay_src": "журнал (age_ms перехода)", "start_rc": 0, "console_back_s": 9.0}


def test_st06_console_stop():
    assert s.judge_console_stop(STOP)[0] == "PASS"
    assert s.judge_console_stop({**STOP, "reason": "timeout", "delay_ms": 548})[0] == "PASS"
    for bad, text in (({"delay_ms": 640}, "640 мс"), ({"reason": "disabled"}, "disabled"),
                      ({"failsafe_s": None, "state": "IDLE", "reason": "disabled"}, "не наблюдался"),
                      ({"delay_ms": None}, "нечем"), ({"console_back_s": None}, "не ответил"),
                      ({"start_rc": 1}, "start")):
        v, d = s.judge_console_stop({**STOP, **bad})
        assert v == "FAIL" and text in d, (bad, d)
    assert s.judge_console_stop({**STOP, "stop_rc": 4})[0] == "ERROR"
    assert s.judge_console_stop({"error": "передача не стала ACTIVE"})[0] == "ERROR"


DIS = {"disable_result": "ok", "idle_s": 0.12, "failsafe_seen": [], "failsafes0": 1, "failsafes1": 1,
       "block_state": "IDLE", "block_reason": "disabled", "journal": ["ACTIVE -> IDLE (disabled)"]}


def test_st07_disable():
    assert s.judge_disable(DIS)[0] == "PASS"
    for bad in ({"idle_s": 1.4}, {"idle_s": None}, {"failsafe_seen": ["журнал ACTIVE -> FAILSAFE (timeout)"]},
                {"failsafes1": 2}, {"failsafes1": None}, {"block_reason": "start"}, {"block_state": "FAILSAFE"},
                {"disable_result": "failed"}):
        assert s.judge_disable({**DIS, **bad})[0] == "FAIL", bad
    assert s.judge_disable({**DIS, "disable_result": None})[0] == "ERROR"
    assert s.judge_disable({"error": "x"})[0] == "ERROR"


UNPLUG = {"active_before": True, "lost_state": "FAILSAFE", "lost_reason": "device-lost", "lost_present": False,
          "lost_enabled": False, "back_s": 3.2, "enabled_after": 0, "active_after": 0,
          "state_after": "FAILSAFE (device-lost)"}


def test_st08_unplug():
    assert s.judge_unplug(UNPLUG)[0] == "PASS"
    for bad in ({"lost_reason": "timeout"}, {"lost_enabled": True}, {"lost_present": True}, {"back_s": None},
                {"enabled_after": 3}, {"active_after": 1}):
        assert s.judge_unplug({**UNPLUG, **bad})[0] == "FAIL", bad
    assert s.judge_unplug({**UNPLUG, "active_before": False})[0] == "ERROR"


def test_st09_output():
    assert s.judge_output(["none"] * 7)[0] == "PASS"
    v, d = s.judge_output(["none", "unknown", None])
    assert v == "FAIL" and "2 из 3" in d
    assert s.judge_output([])[0] == "ERROR"


# ---------------------------------------------------------------- шаги на подставном стенде

class FakeSampler:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def since(self, t0):
        return [r for r in self.rows if r[0] >= t0]

    def last(self):
        return self.rows[-1] if self.rows else None


class FakeStand:
    def __init__(self, programs, docs=()):
        self.programs, self.sent = list(programs), []
        self.rs, self.ui = FakeSampler(docs), FakeSampler()

    def sticks(self, prog, timeout=None):
        self.sent.append(prog)
        return self.programs.pop(0) if self.programs else []

    def status(self):
        rows = self.sticks([{"op": "status"}])
        return rows[-1]["r"] if rows else {}

    def control_after(self, t, timeout):
        return s.ctl_of(self.rs.rows[-1][2]) if self.rs.rows else {}


def window_rows(seconds=32.6, lat=lambda i: 12.0 + i % 7):
    rows = [row(100.0, sticks(), op="enable"), row(100.05, sticks(rem=remote("IDLE", "start", rx=99.0)))]
    t, i = 100.25, 0
    while t < 100.25 + seconds:
        rows.append(row(round(t, 3), sticks(rem=remote(n=10 + i // 2, rx=100.2 + (i // 2) * 0.1, lat=lat(i // 2)))))
        t += 0.05
        i += 1
    return rows


def test_step_window_on_fake_stand():
    far = 1e12                                                      # «прочитано после старта шага» на любых часах Mac
    docs = [(far + k, 2000.0 + 2 * k, {"time": 2000.0 + 2 * k, "control": {"state": "ACTIVE", "since_s": 2.0 * k,
                                                                           "rate_hz": 49.9, "gaps": 0,
                                                                           "output": "none"}})
            for k in range(17)]
    st = FakeStand([window_rows()], docs)
    a = argparse.Namespace(window=30.0)
    (r3, m3), rest, (r5, m5) = s.step_window(a, st)
    assert r3[0] == "PASS", r3 and m3["active_s"] == pytest.approx(0.25)
    assert len(m3["block_rates"]) == 15                              # since_s < 2,5 с не в счёт
    assert s.judge_echo_rest(rest)[0] == "PASS"
    assert r5[0] == "PASS" and 290 <= m5["samples"] <= 330, (r5, m5)
    assert m5["console"]["p99_ms"] == 40.0
    prog = st.sent[0]
    assert prog[0] == {"op": "enable"} and prog[1]["until"] == {"remote.state": "ACTIVE"}
    assert prog[2]["period"] == s.WINDOW_PERIOD_S


def test_restore_starts_only_stopped_units_and_disables(monkeypatch):
    """finally: start только остановленной службы и только через s6 start_guard; передача выключается; блок
    возвращается в IDLE через enable → disable, если был IDLE до прогона."""
    calls, guarded = [], []
    monkeypatch.setattr(s.E, "out", lambda h, c, t=60: "inactive" if "sheregesh-console" in c else "active")
    monkeypatch.setattr(s.S6, "ssh", lambda h, c, timeout=60: calls.append((h, c)) or (0, ""))
    monkeypatch.setattr(s.S6, "start_guard", lambda h, u, record=True, **k: guarded.append((h, u, record)))
    ok = sticks(enabled=False, rem=remote("FAILSAFE", "device-lost"))
    st = FakeStand([[row(1, ok)],
                    [row(2, sticks(), op="enable"), row(2.1, sticks(rem=remote()))],
                    [row(3, sticks(enabled=False), op="disable"), row(3.1, sticks(enabled=False,
                                                                                rem=remote("IDLE", "disabled")))]],
                   [(1.0, 1.0, {"time": 1.0, "control": {"state": "FAILSAFE", "reason": "device-lost"}})])
    a = argparse.Namespace(console="pi1", remote="pi2")
    out = s.restore(a, st, "IDLE")
    assert [c for _h, c in calls] == ["systemctl --no-ask-password start sheregesh-console 2>&1"]
    assert guarded == [("pi1", "sheregesh-console", True)]
    assert out["to_idle"] is True and out["disable"] == "ok" and out["final_enabled"] is False
    assert [p[0]["op"] for p in st.sent] == ["status", "enable", "disable"]


# ---------------------------------------------------------------- опросчик против подставного канала пульта

class FakeConsole:
    """Unix-сокет с протоколом control.py (строка JSON → строка JSON): enable включает, через 3 опроса — ACTIVE."""

    def __init__(self, path):
        self.path, self.reqs, self.enabled, self.polls = path, [], False, 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(4)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                req = json.loads(conn.makefile().readline())
                self.reqs.append(req)
                if req["op"] == "enable":
                    self.enabled, self.polls = req.get("confirmed") is True, 0
                elif req["op"] == "disable":
                    self.enabled = False
                self.polls += 1
                state = "ACTIVE" if self.enabled and self.polls > 3 else "IDLE"
                st = sticks(enabled=self.enabled, rem=remote(state, "fresh" if state == "ACTIVE" else "start"),
                            recent_ms=[1.0] * 50)
                conn.sendall((json.dumps({"result": "ok", "detail": "", "sticks": st}) + "\n").encode())


def test_helper_against_fake_console_channel():
    d = tempfile.mkdtemp(prefix="s7", dir="/tmp")                     # путь сокета короче 104 байт (macOS)
    try:
        path = os.path.join(d, "c.sock")
        fake = FakeConsole(path)
        prog = [{"op": "status"}, {"op": "enable"},
                {"op": "poll", "s": 3, "period": 0.01, "until": {"remote.state": "ACTIVE"}},
                {"op": "poll", "s": 0.2, "period": 0.05}, {"op": "disable"}]
        cmd = s.helper_cmd(prog).replace(s.REL, f"C={ROOT}").replace("python3 -c", f"{sys.executable} -c")
        r = subprocess.run(["sh", "-c", cmd], env={**os.environ, "SHEREGESH_CONSOLE_CONTROL": path},
                           capture_output=True, text=True, timeout=30)
        rows = s.parse_rows(r.stdout)
        ops = [x["op"] for x in rows]
        assert ops[:2] == ["status", "enable"] and ops[-1] == "disable", r.stdout[-500:]
        assert s.time_to(rows, rows[1]["t0"], s.remote_is("ACTIVE")) is not None
        first_active = next(i for i, x in enumerate(rows) if s.g(s.sticks_of(x["r"]), "rstate") == "ACTIVE")
        assert rows[first_active + 1]["op"] == "status"               # until остановил первый опрос, второй пошёл
        assert 3 <= len(rows) - first_active - 2 <= 6                 # 0,2 с по 0,05 с
        assert all("recent_ms" not in x["r"]["sticks"]["latency"] for x in rows)
        assert fake.reqs[1] == s.REQUESTS["enable"] and fake.reqs[-1] == s.REQUESTS["disable"]
        assert all(x["t"] >= x["t0"] for x in rows)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_helper_reports_absent_console():
    cmd = s.helper_cmd([{"op": "status"}]).replace(s.REL, f"C={ROOT}").replace("python3 -c", f"{sys.executable} -c")
    r = subprocess.run(["sh", "-c", cmd], env={**os.environ, "SHEREGESH_CONSOLE_CONTROL": "/nonexistent/s7.sock"},
                       capture_output=True, text=True, timeout=30)
    rows = s.parse_rows(r.stdout)
    assert len(rows) == 1 and rows[0]["r"]["result"] == "not-connected"


# ---------------------------------------------------------------- рамки и запуск

def test_reuses_s6_guard_preflight_and_ota():
    """start_guard/systemctl/safety_start, предпроверка и сравнение OTA — импортом из s6_jr_accept, не копией."""
    src = (ROOT / "tools/probe/s7_sticks_accept.py").read_text()
    for name in ("def start_guard", "def systemctl", "def safety_start", "def board_state", "def judge_ota",
                 "def preflight", "BOARD_CMD =", "START_BUDGET"):
        assert name not in src, name
    for use in ("S6.systemctl(", "S6.safety_start(", "S6.preflight(a)", "S6.judge_ota(", "S6.boards(a)"):
        assert use in src, use
    assert s.S6.preflight.__module__ == "s6_jr_accept"


def test_commands_within_image_polkit_rules():
    """systemctl от sheregesh-ui — только stop/start пульта (ST-06) и start остановленной службы (finally), глаголы
    правила 50; OTA только читается; ни sudo, ни интерфейсов, ни удалений."""
    r50 = (ROOT / "os/layer/sheregesh-dev-access.d/50-sheregesh-dev.rules").read_text()
    run = re.search(r"var run = \[([^\]]*)\]", r50).group(1)
    src = (ROOT / "tools/probe/s7_sticks_accept.py").read_text()
    verbs = set(re.findall(r'systemctl\((?:a\.\w+|host), "(\w+)", (\w+)', src))
    assert verbs == {("stop", "CONSOLE_UNIT"), ("start", "CONSOLE_UNIT"), ("start", "unit")}, verbs
    assert all(f'"{v}"' in run for v, _u in verbs) and f'"{s.CONSOLE_UNIT}.service"' in r50
    assert "S6.safety_start(a.console, CONSOLE_UNIT" in src
    assert not re.search(r"\bsudo\s+[a-z/]", src) and "reboot" not in src
    for bad in ("reset-failed", "set-property", "systemctl enable", "ip link", "ifconfig", "nmcli", "ota-tryboot",
                "ota-confirm", "ota-install", "shdeploy.py deploy", "rm -rf", "rm -f", "pkill", "kill -"):
        assert bad not in src, bad
    assert set(re.findall(r"sheregesh-ota-client (\w+)", src)) <= {"status"}   # OTA — s6_jr_accept.BOARD_CMD


def test_plan_without_ssh(capsys, monkeypatch):
    monkeypatch.setattr(s.E, "ssh", lambda *a, **k: pytest.fail("SSH в --plan"))
    assert s.main(["--plan"]) == 0
    out = capsys.readouterr().out
    assert all(k in out for k in ("ST-01", "ST-02", "ST-03/04/05", "ST-06", "ST-07", "ST-08", "ST-09", "ST-10 до",
                                  "ST-10 после", "600 мс", "p99 ≤ 100"))
    assert "SKIP (--manual)" in out and "30 с опроса" in out
    assert s.main(["--plan", "--manual", "--window", "20"]) == 0
    out = capsys.readouterr().out
    assert "по подсказкам" in out and "20 с опроса" in out
    with pytest.raises(SystemExit):
        s.main([])                                                  # без --out — ошибка аргументов, не прогон
    with pytest.raises(SystemExit):
        s.main(["--out", "/nonexistent-x", "--only", "JR-01"])


def test_preflight_mismatch_does_nothing(monkeypatch, tmp_path):
    """Чужая плата или OTA не в покое — ERROR без единой команды пульту (sticks) и systemctl."""
    sent = []
    monkeypatch.setattr(s.E, "board_identity", lambda h: {"serial": "deadbeef", "host": h})
    monkeypatch.setattr(s.E, "out", lambda h, c, t=60: "")
    monkeypatch.setattr(s.E, "ssh", lambda h, c, t=60: sent.append(c) or (0, ""))
    monkeypatch.setattr(s, "ssh", lambda h, c, timeout=60: sent.append(c) or (0, ""))
    assert s.main(["--out", str(tmp_path)]) == 2
    summ = json.loads((tmp_path / "summary.json").read_text())
    assert summ["verdict"] == "ERROR" and any("серийный номер" in p for p in summ["preflight"])
    assert any("OTA None" in p for p in summ["preflight"])
    assert not any("base64" in c or "systemctl" in c for c in sent)


def test_preflight_old_console_does_nothing(monkeypatch, tmp_path):
    """Пульт без команды sticks (не S7) — ERROR предпроверки, без enable и systemctl."""
    sent = []
    monkeypatch.setattr(s.S6, "preflight", lambda a: ({}, {}, []))
    monkeypatch.setattr(s, "ssh", lambda h, c, timeout=60: sent.append(c) or (0, json.dumps(
        {"t0": 1.0, "t": 1.01, "op": "status",
         "r": {"result": "bad-request", "detail": "cmd: list | select | module"}})))
    assert s.main(["--out", str(tmp_path)]) == 2
    summ = json.loads((tmp_path / "summary.json").read_text())
    assert "не S7" in summ["preflight"][0] and "bad-request" in summ["preflight"][0]
    assert len(sent) == 1 and sent[0].endswith(s.shlex.quote(json.dumps([{"op": "status"}])) + " 2>&1")


def test_echo_rest_tolerates_axis_noise_not_more():
    """Прогон 2026-10-26: оси в покое дрожат на 3, эхо (10 Гц, с задержкой) расходится до 2 — это не ошибка передачи;
    расхождение больше шума оси за окно — FAIL."""
    base = {"samples": 601, "stable": 228, "spread": 3, "btn_bad": 0, "buttons": [1], "axes_ref": [0] * 8}
    assert s.judge_echo_rest({**base, "echo_max": 2})[0] == "PASS"
    assert s.judge_echo_rest({**base, "echo_max": 3})[0] == "PASS"
    assert s.judge_echo_rest({**base, "echo_max": 4})[0] == "FAIL"
    assert s.judge_echo_rest({**base, "spread": 0, "echo_max": 1})[0] == "PASS"
    assert s.judge_echo_rest({**base, "spread": 0, "echo_max": 2})[0] == "FAIL"
