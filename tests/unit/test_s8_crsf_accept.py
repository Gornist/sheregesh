# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Приёмка S8 CR (tools/probe/s8_crsf_accept.py, ADR-0014 §4) и пошаговое включение Nomad (tools/probe/nomad_bringup.py,
§3.4) без плат: разбор ответов опросчика и журнала блока, вердикты CR-01…CR-09, пересчёт каналов в отсчёты CRSF (как
sheregesh.crsf.frames), пути полей против настоящего wire.sanitize_module, опросчик против подставного канала пульта
(включая modulectl), команды — в пределах правила polkit образа, план без SSH, предпроверка без действий,
переиспользование s6/s7; шаги nomad_bringup на ядре эмулятора ELRS TX вместо UART.

Номера CR — критерии спринта, а не TEST-ID реестра: маркер test_id не ставится."""
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
import nomad_bringup as nb  # noqa: E402
import s8_crsf_accept as s  # noqa: E402

AX = [0, -12, 1000, 3, -1000, 0, 0, 1000]


def tel(state="READY", output="neutral", backend="crsf-pty-lab", emu_ch=None, armed=False, rate=50.0, crc=0,
        jitter=300, synced=True, period=20000, frames=100, name=s.EMU_NAME, emu_crc=0, crsf=True):
    t = {"v": 1, "state": state, "seq": 5, "output": output, "settings": {"backend": backend},
         "profile": {"id": "radiomaster-nomad", "name": "RadioMaster Nomad"}, "history": []}
    if crsf:
        t["crsf"] = {"mode": output, "period_us": period, "synced": synced, "sync_age_ms": 40, "frames_sent": frames,
                     "jitter_us_p99": jitter, "crc_errors": crc, "collisions": 0, "link": None,
                     "device": {"name": name, "sw": "4.1.0", "serial": "ELRS"}, "last_rx_ms": 5,
                     "emu": {"state": "", "ch": list(emu_ch if emu_ch is not None else [0] * 16), "armed": armed,
                             "frames": frames, "rate_hz": rate, "jitter_us_p99": 120, "crc_errors": emu_crc}}
    return t


def mod(t, support="ok"):
    return {"result": "ok", "detail": "", "module": {"support": support, "stale": False, "session": "1.4",
                                                    "telemetry": t}}


def row(t, r, op="mod"):
    return {"t0": round(t - 0.004, 4), "t": t, "op": op, "r": r}


def sticks(axes=AX, enabled=True):
    return {"result": "ok", "sticks": {"support": "ok", "present": True, "enabled": enabled, "axes": list(axes),
                                       "buttons": 5, "remote": {"state": "ACTIVE"}}}


def live_ch(axes=AX):
    """Каналы, как их видит пульт в crsf.emu.ch: отсчёт эмулятора → шкала −1000…1000 (frames.crsf_to_axis)."""
    F = pytest.importorskip("sheregesh.crsf.frames")
    return [F.crsf_to_axis(F.axis_to_crsf(v)) for v in axes] + [0] * 8


# ---------------------------------------------------------------- разбор, пути, пересчёт

def test_to_crsf_matches_frames_and_roundtrip():
    F = pytest.importorskip("sheregesh.crsf.frames")
    for v in range(-1000, 1001):
        assert s.to_crsf(v) == F.axis_to_crsf(v), v
    for c in range(F.CH_MIN, F.CH_MAX + 1):                         # emu.ch (ось) → отсчёт: без потерь
        assert s.to_crsf(F.crsf_to_axis(c)) == c, c
    assert (s.CENTER, s.CH_MIN, s.CH_MAX, s.SPAN) == (F.CH_CENTER, F.CH_MIN, F.CH_MAX, F.CH_SCALE)


def test_paths_against_real_sanitize_module():
    """Пути P и crsf.emu — против настоящего wire.sanitize_module (агент H): то, что видит пульт."""
    wire = pytest.importorskip("sheregesh.module.wire")
    if "backend" not in wire.CONFIGURE_KEYS:
        pytest.skip("wire без S8")
    raw = tel(output="live", emu_ch=live_ch(), armed=True)
    raw["settings"]["backend"] = "crsf-pty-lab"
    clean = wire.sanitize_module(raw)
    r = mod(clean)
    assert s.g(r, "state") == "READY" and s.g(r, "output") == "live" and s.backend_of(r) == "crsf-pty-lab"
    assert s.mode_of(r) == "live" and s.device_name(r) == s.EMU_NAME
    e = s.emu_of(r)
    assert e["armed"] is True and e["rate_hz"] == 50.0 and e["crc_errors"] == 0
    assert s.emu_channels(e)[:8] == [s.to_crsf(v) for v in AX] and s.emu_channels(e)[8:] == [992] * 8
    assert {s.BACKEND_LAB, s.BACKEND_S6, s.BACKEND_UART} <= set(wire.BACKENDS)
    assert {"silent", "heal"} <= set(wire.LAB_SCENARIOS) and set(wire.OUTPUTS) == {"none", "neutral", "live"}


def test_real_emulator_state_reaches_console_as_crsf_counts():
    """Цепочка без плат: кадр 0x16 → эмулятор ELRS TX (агент K) → строка state → module/crsf._emu_view (агент H) →
    wire → emu_channels: те же отсчёты 11 бит, что упаковал хост; взведение и центр в нейтрали."""
    import time
    elrs = pytest.importorskip("sheregesh_emulators.elrs_tx")
    F = pytest.importorskip("sheregesh.crsf.frames")
    crsf = pytest.importorskip("sheregesh.module.crsf")
    wire = pytest.importorskip("sheregesh.module.wire")
    if not hasattr(wire, "_crsf_emu"):
        pytest.skip("wire без crsf.emu")
    emu = elrs.ElrsTxEmulator(time.monotonic)
    for axes, armed in ((AX, True), ([0] * 8, False)):
        emu.feed(F.pack_channels(list(axes) + [0] * 8, armed))
        r = mod({"crsf": {"emu": wire._crsf_emu(crsf._emu_view(emu.state()))}})
        e = s.emu_of(r)
        assert s.emu_channels(e)[:8] == [F.axis_to_crsf(v) for v in axes] and e["armed"] is armed
    assert set(s.emu_channels(e)) == {s.CENTER} and not s.raw_scale_hint(e)


def test_no_response_s_matches_profile():
    nomad = pytest.importorskip("sheregesh.module.profiles.radiomaster_nomad").NOMAD
    if not hasattr(nomad, "no_response_s"):
        pytest.skip("профиль без S8")
    assert s.LIMITS["no_response_s"] == nomad.no_response_s
    assert nomad.crsf_name == nb.NAME_PREFIX and nomad.baud == nb.BAUD
    assert s.no_response_s(mod(tel())) == nomad.no_response_s


def test_emulator_name_matches():
    elrs = pytest.importorskip("sheregesh_emulators.elrs_tx")
    assert elrs.NAME == s.EMU_NAME and elrs.PERIOD_US == s.LIMITS["period_us"]
    assert s.ELRS_MARK == "sheregesh_emulators." + "elrs_tx"


def test_mode_and_mods_filter():
    rows = [row(1.0, mod(tel())), row(1.1, mod(tel(), support="no-link")), row(1.2, {"result": "error"}),
            row(1.3, sticks(), op="sticks"), row(1.4, mod(tel(output="live")))]
    assert [t for t, _r in s.mods(rows)] == [1.0, 1.4]
    bad = tel(output="live")
    bad["crsf"]["mode"] = "neutral"
    assert s.mode_of(mod(bad)) == "live/neutral"                       # расхождение видно в вердикте
    assert s.mode_of(mod(tel(output="none", crsf=False))) == "none"
    assert s.time_to(rows, 0.9, lambda r: s.mode_of(r) == "live") == pytest.approx(0.5)


def test_journal_lines_from_real_service_and_manager():
    """Строки журнала pi2 в формате агента ([remote] module: …): переходы менеджера и выхода CRSF."""
    text = "\n".join([
        "1000.100000 pi2 python3[1]: [remote] module: configure (req 7): backend=crsf-pty-lab — saving",
        "1000.300000 pi2 python3[1]: [remote] module: DISABLED -> DISCONNECTED (cmd-configure) profile=x",
        "1001.900000 pi2 python3[1]: [remote] module: INITIALIZING -> READY (init-ok) profile=radiomaster-nomad",
        "1002.000000 pi2 python3[1]: [remote] module: crsf: none -> neutral (module READY, control IDLE)",
        "1010.000000 pi2 python3[1]: [remote] control: ACTIVE -> FAILSAFE (link-lost) n=900 age_ms=505",
        "1010.010000 pi2 python3[1]: [remote] module: crsf: live -> neutral (module READY, control FAILSAFE)",
        "1011.000000 pi2 python3[1]: [elrs_tx] host uart-disconnected"])
    mt = s.parse_module_transitions(text)
    assert [(x["from"], x["to"]) for x in mt] == [("DISABLED", "DISCONNECTED"), ("INITIALIZING", "READY")]
    ct = s.parse_crsf_transitions(text)
    assert [(x["from"], x["to"]) for x in ct] == [("none", "neutral"), ("live", "neutral")]
    fs = s.S7.parse_transitions(text)[0]
    assert s.neutral_delay(fs, ct[1]) == 515
    assert s.neutral_delay(None, ct[1]) is None and s.neutral_delay({**fs, "age_ms": None}, ct[1]) is None
    assert s.first_line(text, r"configure \(req \d+\):.*backend=crsf-pty-lab")[0] == 1000.1
    assert s.first_line(text, "uart-disconnected", 1010.5)[0] == 1011.0
    assert s.first_line(text, "uart-disconnected", 1011.5) == (None, None)


def test_service_logs_crsf_transition_in_parsed_format():
    """Сверка с настоящим ModuleService (агент H): запись о смене режима выхода разбирается parse_crsf_transitions."""
    svc = pytest.importorskip("sheregesh.module.service")
    src = Path(svc.__file__).read_text()
    if "crsf: {self.output} -> {out[0]}" not in src:
        pytest.skip("ModuleService без журнала режима выхода")
    line = "5.000000 pi2 python3[1]: [remote] module: crsf: neutral -> live (module READY, control ACTIVE)"
    assert s.parse_crsf_transitions(line)[0]["to"] == "live"


# ---------------------------------------------------------------- вердикты

def test_cr01_enable():
    ok = {"result": "ok", "ready_s": 3.1, "ready_block_s": 1.6, "backend": "crsf-pty-lab", "device": s.EMU_NAME}
    assert s.judge_enable(ok)[0] == "PASS" and "журнал pi2" in s.judge_enable(ok)[1]
    assert s.judge_enable({**ok, "ready_block_s": None})[0] == "PASS"
    assert s.judge_enable({**ok, "ready_block_s": None, "ready_s": 6.0})[0] == "FAIL"
    assert s.judge_enable({**ok, "ready_s": 6.0})[0] == "PASS"            # по часам блока ≤ 5 с — верхняя оценка шире
    assert s.judge_enable({**ok, "ready_block_s": 5.5})[0] == "FAIL"
    assert s.judge_enable({**ok, "device": "RM Nomad X-Band"})[0] == "FAIL"
    assert s.judge_enable({**ok, "result": "bad-request"})[0] == "FAIL"
    assert s.judge_enable({**ok, "result": None})[0] == "ERROR"
    assert s.judge_enable({**ok, "ready_s": None, "ready_block_s": None})[0] == "FAIL"


def test_cr02_sync():
    snaps = [{"synced": False, "period_us": 4000}] + [{"synced": True, "period_us": 20000, "sync_age_ms": 50}] * 3
    assert s.judge_sync(snaps)[0] == "PASS"
    assert s.judge_sync(snaps[:-1] + [{"synced": True, "period_us": 20250}])[0] == "FAIL"
    assert s.judge_sync(snaps[:-1] + [{"synced": True, "period_us": 20190}])[0] == "PASS"
    assert s.judge_sync([{"synced": False, "period_us": 4000}])[0] == "FAIL"
    assert s.judge_sync(snaps + [{"synced": False, "period_us": 4000}])[0] == "FAIL"
    assert s.judge_sync([])[0] == "ERROR"


def test_cr03_neutral():
    smp = [(10.0, mod(tel(output="live", emu_ch=live_ch(), armed=True)))] + \
        [(10.2 + 0.1 * i, mod(tel())) for i in range(20)]
    assert s.judge_neutral(smp)[0] == "PASS"                             # live до первого neutral не в счёт
    off = smp + [(13.0, mod(tel(emu_ch=[0, 5] + [0] * 14)))]
    assert s.judge_neutral(off)[0] == "FAIL"
    assert s.judge_neutral(smp + [(13.0, mod(tel(armed=True)))])[0] == "FAIL"
    assert s.judge_neutral(smp + [(13.0, mod(tel(output="live")))])[0] == "FAIL"
    assert s.judge_neutral([(1.0, mod(tel(output="none")))])[0] == "FAIL"
    assert s.judge_neutral([])[0] == "ERROR"
    raw = [(1.0 + 0.1 * i, mod(tel(emu_ch=[992] * 16))) for i in range(20)]
    v, d = s.judge_neutral(raw)
    assert v == "FAIL" and "сырые отсчёты 11 бит" in d                   # расхождение шкал emu.ch видно сразу
    no_ch = [(1.0 + 0.1 * i, mod(tel(emu_ch=[]))) for i in range(20)]
    assert "не сообщает" in s.judge_neutral(no_ch)[1]


def window(axes=AX, noise=0, emu=None, armed=True, mode="live", n=100):
    sts, ms = [], []
    for i in range(n):
        a = [v + (noise if i % 2 and v < 1000 else 0) for v in axes]
        sts.append((20.0 + 0.1 * i, sticks(a)))
        ms.append((20.05 + 0.1 * i, mod(tel(output=mode, emu_ch=emu or live_ch(axes), armed=armed,
                                            frames=100 + 5 * i))))
    return sts, ms


def test_cr04_live_echo():
    sts, ms = window()
    e = s.echo_check(sts, s.distinct_emu(ms))
    assert e["diff_max"] == 0 and e["tol"] == 1 and e["emu"] >= 1 and e["tail_bad"] == 0 and e["tail"] >= 1
    m = {"enable_result": "ok", "live_s": 0.8, "echo": e, "armed": [True], "not_live": []}
    assert s.judge_live(m)[0] == "PASS" and "9–16 = 992" in s.judge_live(m)[1]
    sts8, ms8 = window(emu=live_ch()[:8])                               # блок S8 (module/crsf._emu_view): только 1–8
    e8 = s.echo_check(sts8, s.distinct_emu(ms8))
    assert e8["tail"] == 0 and s.judge_live({**m, "echo": e8})[0] == "PASS"
    assert "только оси 1–8" in s.judge_live({**m, "echo": e8})[1]
    sts, ms = window(noise=3)                                            # шум осей в покое: допуск 1 + ceil(3·0,82)
    e = s.echo_check(sts, s.distinct_emu(ms))
    assert e["tol"] == 4 and s.judge_live({**m, "echo": e})[0] == "PASS"
    wrong = live_ch([0, -12, 1000, 3, -1000, 0, 0, 900])
    sts, ms = window(emu=wrong)
    e = s.echo_check(sts, s.distinct_emu(ms))
    assert e["diff_max"] >= 8 and s.judge_live({**m, "echo": e})[0] == "FAIL"
    tail = live_ch()[:8] + [0] * 7 + [100]
    sts, ms = window(emu=tail)
    assert s.judge_live({**m, "echo": s.echo_check(sts, s.distinct_emu(ms))})[0] == "FAIL"
    assert s.judge_live({**m, "armed": [False]})[0] == "FAIL"
    assert s.judge_live({**m, "armed": []})[0] == "FAIL"
    assert s.judge_live({**m, "not_live": ["neutral"]})[0] == "FAIL"
    assert s.judge_live({**m, "live_s": 3.5})[0] == "FAIL"
    assert s.judge_live({**m, "echo": {**m["echo"], "spread": 300}})[0] == "ERROR"     # стики трогали
    assert s.judge_live({**m, "enable_result": None})[0] == "ERROR"


def test_cr05_rate():
    m = {"emu_rates": [49.6, 50.0, 50.4], "emu_jitter": [150], "out_jitter": [800, 1200], "out_crc": 0, "emu_crc": 0,
         "frames_sent": 500}
    assert s.judge_rate(m)[0] == "PASS"
    assert s.judge_rate({**m, "emu_rates": [48.5, 50.0]})[0] == "FAIL"
    assert s.judge_rate({**m, "emu_rates": [51.2]})[0] == "FAIL"
    assert s.judge_rate({**m, "out_jitter": [2500]})[0] == "FAIL"
    assert s.judge_rate({**m, "emu_jitter": [2100]})[0] == "FAIL"
    assert s.judge_rate({**m, "out_crc": 2})[0] == "FAIL"
    assert s.judge_rate({**m, "emu_crc": 1})[0] == "FAIL"
    assert s.judge_rate({**m, "emu_rates": []})[0] == "ERROR"
    assert s.judge_rate({**m, "out_jitter": []})[0] == "FAIL"
    assert s.judge_rate({"error": "нет окна"})[0] == "ERROR"


def test_step_window_on_fake_stand():
    sts, ms = window()
    rows = [row(19.0, sticks(enabled=True), op="enable"), row(19.3, mod(tel(output="neutral"))),
            row(19.6, mod(tel(output="live", emu_ch=live_ch(), armed=True)))]
    rows += [row(t, r, op="sticks") for t, r in sts] + [row(t, r) for t, r in ms]
    rows.sort(key=lambda x: x["t"])
    st = FakeStand([rows])
    (r4, m4), (r5, m5) = s.step_window(argparse.Namespace(window=10.0), st)
    assert r4[0] == "PASS", r4 and m4["live_s"] == pytest.approx(0.604)
    assert r5[0] == "PASS", r5 and m5["frames_sent"] > 0
    prog = st.sent[0]
    assert prog[0] == {"op": "enable"} and prog[1]["until"] == {"mod:module.telemetry.output": "live"}
    assert prog[2]["ask"] == ["sticks", "mod"]


def test_cr06_failsafe():
    ok = {"stop_rc": 0, "journal_ok": True, "failsafe": {"reason": "timeout", "t": 1.0, "age_ms": 505},
          "neutral": {"t": 1.01}, "delay_ms": 515, "start_rc": 0, "console_back_s": 20.0, "center": ("PASS", "ok")}
    assert s.judge_failsafe(ok)[0] == "PASS"
    assert s.judge_failsafe({**ok, "delay_ms": 650})[0] == "FAIL"
    assert s.judge_failsafe({**ok, "neutral": None})[0] == "FAIL"
    assert s.judge_failsafe({**ok, "failsafe": {"reason": "device-lost", "age_ms": 1}})[0] == "FAIL"
    assert s.judge_failsafe({**ok, "center": ("FAIL", "каналы не центр")})[0] == "FAIL"
    assert s.judge_failsafe({**ok, "console_back_s": None})[0] == "FAIL"
    assert s.judge_failsafe({**ok, "journal_ok": False})[0] == "ERROR"
    assert s.judge_failsafe({**ok, "stop_rc": 1})[0] == "ERROR"
    assert s.judge_failsafe({"error": "не live"})[0] == "ERROR"


def test_cr07_silent():
    ok = {"silent_result": "ok", "no_response_s": 2.0, "fault_block_s": 2.3, "fault_s": 3.4,
          "fault_modes": ["none", "none"], "heal_result": "ok", "ready_s": 4.0}
    assert s.judge_silent(ok)[0] == "PASS"
    assert s.judge_silent({**ok, "fault_block_s": 3.2})[0] == "FAIL"
    assert s.judge_silent({**ok, "fault_block_s": None})[0] == "FAIL"           # верхняя оценка пульта 3,4 > 3
    assert s.judge_silent({**ok, "fault_block_s": None, "fault_s": 2.9})[0] == "PASS"
    assert s.judge_silent({**ok, "fault_modes": ["neutral"]})[0] == "FAIL"
    assert s.judge_silent({**ok, "fault_modes": []})[0] == "FAIL"
    assert s.judge_silent({**ok, "heal_result": "forbidden"})[0] == "FAIL"
    assert s.judge_silent({**ok, "ready_s": None})[0] == "FAIL"
    assert s.judge_silent({**ok, "silent_result": "bad-request"})[0] == "FAIL"


def test_cr08_restart():
    fr = {"journal_ok": True, "disc_s": 1.05, "ready_s": 3.0}
    rs = {"rc": 0, "ready_s": 12.0, "mode": "neutral", "backend": "crsf-pty-lab"}
    v, d = s.judge_restart({"freeze": fr, "restart": rs})
    assert v == "PASS" and "uart-disconnected через 1.05" in d
    assert s.judge_restart({"freeze": {**fr, "disc_s": 1.7}, "restart": rs})[0] == "FAIL"
    assert s.judge_restart({"freeze": {**fr, "disc_s": None}, "restart": rs})[0] == "FAIL"
    assert s.judge_restart({"freeze": {**fr, "ready_s": None}, "restart": rs})[0] == "FAIL"
    assert s.judge_restart({"freeze": {**fr, "journal_ok": False}, "restart": rs})[0] == "ERROR"
    assert s.judge_restart({"freeze": {"error": "SIGSTOP"}, "restart": rs})[0] == "ERROR"
    assert s.judge_restart({"freeze": fr, "restart": {**rs, "mode": "none"}})[0] == "FAIL"
    assert s.judge_restart({"freeze": fr, "restart": {**rs, "backend": "lab-emulator"}})[0] == "FAIL"
    assert s.judge_restart({"freeze": fr, "restart": {**rs, "ready_s": 31}})[0] == "FAIL"
    assert s.judge_restart({"freeze": fr, "restart": {**rs, "rc": 1}})[0] == "ERROR"
    v, d = s.judge_restart({"freeze": None, "restart": rs})
    assert v == "PASS" and "--no-freeze" in d


def test_cr09_back():
    ok = {"result": "ok", "ready_s": 4.0, "backend": "lab-emulator", "modes": ["none", "none"], "jr": 1, "elrs": []}
    assert s.judge_back(ok)[0] == "PASS"
    assert s.judge_back({**ok, "modes": ["neutral"]})[0] == "FAIL"
    assert s.judge_back({**ok, "modes": []})[0] == "FAIL"
    assert s.judge_back({**ok, "jr": 0})[0] == "FAIL"
    assert s.judge_back({**ok, "elrs": [123]})[0] == "FAIL"
    assert s.judge_back({**ok, "backend": "crsf-pty-lab"})[0] == "FAIL"
    assert s.judge_back({**ok, "ready_s": None})[0] == "FAIL"
    assert s.judge_back({**ok, "result": "invalid"})[0] == "FAIL"
    assert s.judge_back({**ok, "result": None})[0] == "ERROR"


def test_elrs_pids_from_ps():
    snap = s.S6.parse_procs("MainPID=10 NRestarts=0 ActiveState=active\n10 1 python3 -m sheregesh_remote.agent\n"
                            "20 10 python3 -s -m sheregesh_emulators.elrs_tx --tty /dev/pts/3\n"
                            "30 10 python3 -s -m sheregesh_emulators.jrmodule --profile radiomaster-nomad\n")
    assert s.elrs_pids(snap) == [20] and s.S6.emulator_pids(snap) == [30]


# ---------------------------------------------------------------- подставной стенд

class FakeSampler:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def since(self, t0):
        return [r for r in self.rows if r[0] >= t0]

    def last(self):
        return self.rows[-1] if self.rows else None


class FakeStand:
    def __init__(self, programs):
        self.programs, self.sent = list(programs), []
        self.rs, self.ui = FakeSampler(), FakeSampler()

    def ask(self, prog, timeout=None):
        self.sent.append(prog)
        return self.programs.pop(0) if self.programs else []

    def mod(self):
        rows = self.ask([{"op": "mod"}])
        return rows[-1]["r"] if rows else {}


def test_restore_sigcont_sticks_backend_disable(monkeypatch):
    """finally: SIGCONT блоку (только если замораживали), s7 restore (службы, передача выключена, IDLE), backend
    lab-emulator, последним — sticks disable."""
    calls = []
    monkeypatch.setattr(s, "kill_main", lambda a, sig: calls.append(sig) or (0, ""))
    monkeypatch.setattr(s.S7, "restore", lambda a, st, init: calls.append(("s7", init)) or {"disable": "ok"})
    st = FakeStand([[row(1.0, mod(tel(backend="crsf-pty-lab")))],
                    [row(2.0, {"result": "ok", "rc": 0}, op="ctl"), row(3.0, mod(tel(backend="lab-emulator",
                                                                                   output="none", crsf=False)))],
                    [row(4.0, sticks(enabled=False), op="disable"), row(4.1, mod(tel(backend="lab-emulator"))),
                     row(4.2, sticks(enabled=False), op="sticks")]])
    out = s.restore(argparse.Namespace(), st, "IDLE", froze=True)
    assert calls == ["SIGCONT", ("s7", "IDLE")]
    assert st.sent[1][0] == {"op": "ctl", "args": "configure backend=lab-emulator --wait 30"}
    assert [x["op"] for x in st.sent[2]] == ["disable", "mod", "sticks"]
    assert out["configure"] == "ok" and out["final_backend"] == "lab-emulator" and out["final_enabled"] is False
    calls.clear()
    st = FakeStand([[row(1.0, mod(tel(backend="lab-emulator")))], [row(4.0, sticks(enabled=False), op="disable")]])
    out = s.restore(argparse.Namespace(), st, "IDLE", froze=False)
    assert calls == [("s7", "IDLE")] and "configure" not in out and len(st.sent) == 2


# ---------------------------------------------------------------- опросчик против подставного канала пульта

class FakeConsole:
    """Unix-сокет с протоколом пульта: sticks (status/enable/disable) и module (status/configure/lab)."""

    def __init__(self, path):
        self.path, self.reqs, self.enabled, self.backend, self.polls = path, [], False, "lab-emulator", 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(8)
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
                if req["cmd"] == "sticks":
                    if req["op"] == "enable":
                        self.enabled = req.get("confirmed") is True
                    elif req["op"] == "disable":
                        self.enabled = False
                    out = sticks(enabled=self.enabled)
                else:
                    if req["op"] == "configure":
                        self.backend, self.polls = req["args"]["backend"], 0
                    self.polls += 1
                    ready = self.polls > 3
                    out = mod(tel(state="READY" if ready else "INITIALIZING", backend=self.backend,
                                  output=("live" if self.enabled else "neutral") if ready else "none"))
                conn.sendall((json.dumps(out) + "\n").encode())


def helper_run(prog, path):
    cmd = s.helper_cmd(prog).replace(s.REL, f"C={ROOT}").replace("python3 -c", f"{sys.executable} -c")
    return subprocess.run(["sh", "-c", cmd], env={**os.environ, "SHEREGESH_CONSOLE_CONTROL": path},
                          capture_output=True, text=True, timeout=60)


def test_helper_against_fake_console_channel():
    wire = pytest.importorskip("sheregesh.module.wire")
    if "backend" not in wire.CONFIGURE_KEYS:
        pytest.skip("modulectl без configure backend (S8)")
    d = tempfile.mkdtemp(prefix="s8", dir="/tmp")                     # путь сокета короче 104 байт (macOS)
    try:
        path = os.path.join(d, "c.sock")
        fake = FakeConsole(path)
        prog = [{"op": "ctl", "args": "configure backend=crsf-pty-lab --wait 30"},
                {"op": "poll", "s": 3, "period": 0.01, "until": {"mod:" + s.P["state"]: "READY"}},
                {"op": "enable"},
                {"op": "poll", "s": 0.2, "period": 0.05, "ask": ["sticks", "mod"]}, {"op": "disable"}]
        r = helper_run(prog, path)
        rows = s.parse_rows(r.stdout)
        ops = [x["op"] for x in rows]
        assert ops[0] == "ctl" and ops[-1] == "disable", r.stdout[-800:]
        ctl = rows[0]["r"]
        assert ctl["result"] == "ok" and ctl["rc"] == 0 and ctl["via"] == "console"
        assert fake.reqs[0] == {"cmd": "module", "op": "configure", "wait": 30.0, "args": {"backend": "crsf-pty-lab"}}
        assert s.time_to(rows, rows[0]["t0"], s.is_ready("crsf-pty-lab")) is not None
        first_ready = next(i for i, x in enumerate(rows) if x["op"] == "mod" and s.g(x["r"], "state") == "READY")
        assert rows[first_ready + 1]["op"] == "enable"                  # until остановил опрос
        win = rows[first_ready + 2:-1]
        assert {x["op"] for x in win} == {"sticks", "mod"} and 6 <= len(win) <= 12
        assert all("profiles" not in x["r"]["module"]["telemetry"] for x in rows if x["op"] == "mod")
        assert fake.reqs[-1] == s.REQUESTS["disable"] and all(x["t"] >= x["t0"] for x in rows)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_helper_reports_absent_console():
    r = helper_run([{"op": "mod"}, {"op": "ctl", "args": "lab silent --wait 15"}], "/nonexistent/s8.sock")
    rows = s.parse_rows(r.stdout)
    assert [x["op"] for x in rows] == ["mod", "ctl"]
    assert rows[0]["r"]["result"] == "not-connected" and rows[1]["r"]["result"] == "not-connected"


# ---------------------------------------------------------------- рамки и запуск

def test_reuses_s6_s7():
    src = (ROOT / "tools/probe/s8_crsf_accept.py").read_text()
    for name in ("def start_guard", "def systemctl", "def safety_start", "def board_state", "def judge_ota",
                 "def preflight", "BOARD_CMD =", "START_BUDGET", "def parse_transitions"):
        assert name not in src, name
    for use in ("S6.systemctl(", "S6.safety_start(", "S6.preflight(a)", "S6.judge_ota(", "S6.boards(a)",
                "S7.restore(", "S7.parse_transitions(", "class Stand(S7.Stand)"):
        assert use in src, use
    assert "socket_path()" in s.HELPER and "sheregesh.link.modulectl" in s.HELPER and '"--via", "console"' in s.HELPER


def test_commands_within_image_polkit_rules():
    """systemctl от sheregesh-ui — глаголы правила 50 для юнитов пульта и блока; SIGSTOP/SIGCONT — только главному
    процессу блока через systemctl kill (эмулятор не трогается); OTA только читается; ни sudo, ни pkill, ни
    удалений, ни интерфейсов."""
    r50 = (ROOT / "os/layer/sheregesh-dev-access.d/50-sheregesh-dev.rules").read_text()
    run = re.search(r"var run = \[([^\]]*)\]", r50).group(1)
    src = (ROOT / "tools/probe/s8_crsf_accept.py").read_text()
    verbs = set(re.findall(r'systemctl\((?:a\.\w+|host), "(\w+)", (\w+)', src))
    assert verbs == {("stop", "CONSOLE_UNIT"), ("start", "CONSOLE_UNIT"), ("restart", "REMOTE_UNIT")}, verbs
    kills = re.findall(r"systemctl --no-ask-password kill --kill-whom=main -s (\S+)", src)
    assert kills and all(k in ("{sig}", "SIGSTOP", "SIGCONT") for k in kills), kills
    assert all(f'"{v}"' in run for v in ("stop", "start", "restart", "kill"))
    assert f'"{s.CONSOLE_UNIT}.service"' in r50 and f'"{s.REMOTE_UNIT}.service"' in r50
    assert "S6.safety_start(a.console, CONSOLE_UNIT" in src
    assert not re.search(r"\bsudo\s+[a-z/]", src) and "reboot" not in src
    for bad in ("reset-failed", "set-property", "systemctl enable", "ip link", "ifconfig", "nmcli", "ota-tryboot",
                "ota-confirm", "ota-install", "shdeploy.py deploy", "rm -rf", "rm -f", "pkill", "kill -STOP",
                "kill -CONT", "kill -9", "crsf-uart --wait"):
        assert bad not in src, bad
    assert set(re.findall(r"sheregesh-ota-client (\w+)", src)) <= {"status"}
    assert s.LIMITS["freeze_s"] < 15                                   # WatchdogSec=15 у sheregesh-remote-net


def test_plan_without_ssh(capsys, monkeypatch):
    monkeypatch.setattr(s.E, "ssh", lambda *a, **k: pytest.fail("SSH в --plan"))
    assert s.main(["--plan"]) == 0
    out = capsys.readouterr().out
    for k in ("CR-01", "CR-02", "CR-03", "CR-04/05", "CR-06", "CR-07", "CR-08", "CR-09", "CR-10 до", "CR-10 после",
              "СТИКИ НЕ ТРОГАТЬ", "600 мс", "20 000", "crsf-pty-lab", "lab-emulator", "SIGSTOP"):
        assert k in out, k
    assert s.main(["--plan", "--no-freeze", "--window", "20"]) == 0
    out = capsys.readouterr().out
    assert "--no-freeze" in out and "20 с опроса" in out
    with pytest.raises(SystemExit):
        s.main([])
    with pytest.raises(SystemExit):
        s.main(["--out", "/nonexistent-x", "--only", "ST-01"])


def test_preflight_mismatch_does_nothing(monkeypatch, tmp_path):
    """Чужая плата или OTA не в покое — ERROR без единой команды пульту и systemctl."""
    sent = []
    monkeypatch.setattr(s.E, "board_identity", lambda h: {"serial": "deadbeef", "host": h})
    monkeypatch.setattr(s.E, "out", lambda h, c, t=60: "")
    monkeypatch.setattr(s.E, "ssh", lambda h, c, t=60: sent.append(c) or (0, ""))
    monkeypatch.setattr(s, "ssh", lambda h, c, timeout=60: sent.append(c) or (0, ""))
    assert s.main(["--out", str(tmp_path)]) == 2
    summ = json.loads((tmp_path / "summary.json").read_text())
    assert summ["verdict"] == "ERROR" and any("серийный номер" in p for p in summ["preflight"])
    assert not any("base64" in c or "systemctl" in c for c in sent)


def test_preflight_old_console_does_nothing(monkeypatch, tmp_path):
    """Пульт не отвечает module status — ERROR предпроверки, без configure и systemctl."""
    sent = []
    monkeypatch.setattr(s.S6, "preflight", lambda a: ({}, {}, []))
    monkeypatch.setattr(s, "ssh", lambda h, c, timeout=60: sent.append(c) or (0, json.dumps(
        {"t0": 1.0, "t": 1.01, "op": "mod", "r": {"result": "bad-request", "detail": "cmd: list | select"}})))
    monkeypatch.setattr(s.S7, "ssh", lambda h, c, timeout=60: sent.append(c) or (0, ""))
    assert s.main(["--out", str(tmp_path)]) == 2
    summ = json.loads((tmp_path / "summary.json").read_text())
    assert "module status" in summ["preflight"][0]
    assert not any("configure" in c or "systemctl" in c for c in sent)


# ---------------------------------------------------------------- nomad_bringup

def test_bringup_judges():
    assert nb.judge_idle(0, [], 0)[0] is True
    ok, t = nb.judge_idle(12, [], 12)
    assert not ok and "GND" in t
    assert nb.judge_idle(30, ["0x3A"], 0)[0] is False
    dev = {"name": "RM Nomad X-Band", "sw": "4.1.0", "serial": "ELRS"}
    stats = {"tx_bytes": 100, "echo_ok": True}
    assert nb.judge_ping({"device": dev}, stats, "ping") == (True, nb.judge_ping({"device": dev}, stats, "ping")[1])
    assert "вместе с нейтральными" in nb.judge_ping({"device": dev}, stats, "ping+neutral")[1]
    assert nb.judge_ping({"device": {**dev, "name": "BETAFPV"}}, stats, "ping")[0] is False
    ok, t = nb.judge_ping({"device": None, "garbage_bytes": 40}, stats, None)
    assert not ok and "скорость или полярность" in t
    ok, t = nb.judge_ping({"device": dev}, {"tx_bytes": 100, "echo_ok": False}, "ping")
    assert not ok and "эхо" in t
    first = {"frames_sent": 0, "crc_errors": 0, "collisions": 0, "overruns": 0}
    last = {"frames_sent": 500, "crc_errors": 0, "collisions": 0, "overruns": 0, "synced": True, "period_us": 20000,
            "jitter_us_p99": 400, "link": {"rssi": -40, "lq": 100, "tx_power": 100}, "device": dev}
    ok, t, summ = nb.judge_neutral(first, last, 10.0, 0.3)
    assert ok and summ["rate_hz"] == 50.0 and "20000" in t
    assert nb.judge_neutral(first, {**last, "synced": False}, 10.0, None)[0] is False
    assert nb.judge_neutral(first, {**last, "crc_errors": 3}, 10.0, 0.3)[0] is False
    assert nb.judge_neutral(first, {**last, "device": None}, 10.0, 0.3)[0] is False
    ok, t, _ = nb.judge_neutral(first, {**last, "link": None, "jitter_us_p99": 3000}, 10.0, 0.3)
    assert ok and "link statistics" in t and "джиттера" in t


def test_bringup_holders_and_backend(tmp_path):
    assert nb.agent_backend({"module": {"backend": "crsf-uart"}}) == "crsf-uart"
    assert nb.agent_backend({"module": {}}) is None and nb.agent_backend(None) is None
    proc = tmp_path / "proc"
    dev = tmp_path / "ttyAMA3"
    dev.write_text("")
    (proc / "42" / "fd").mkdir(parents=True)
    (proc / "42" / "fd" / "5").symlink_to(dev)
    (proc / "42" / "cmdline").write_bytes(b"python3\0-m\0sheregesh_remote.agent\0")
    (proc / "43" / "fd").mkdir(parents=True)
    assert nb.holders(str(dev), str(proc)) == [(42, "python3 -m sheregesh_remote.agent")]


def test_bringup_remote_and_modulectl_commands():
    cmd = nb.remote_cmd("print('x')", ["--step", "1", "--device", "auto"])
    assert cmd.startswith(nb.REL) and "base64 -d" in cmd and "'--step' '1'" in cmd and "python3 -u -c" in cmd
    m = nb.modulectl_cmd("configure backend=crsf-uart --wait 30")
    assert "sheregesh.link.modulectl configure backend=crsf-uart" in m and "--via console" in m
    text = 'modulectl: …\n{\n "result": "ok",\n "module": {"telemetry": {"state": "READY"}}\n}\n'
    assert nb.parse_json(text)["result"] == "ok" and nb.parse_json("нет") == {}
    assert nb.result_of("x\n" + nb.RESULT + '{"ok": true, "step": 1}\n') == {"ok": True, "step": 1}
    assert nb.result_of("обрыв") is None


def test_bringup_step4_and_confirmations(monkeypatch, capsys):
    """Шаг 4 только с --switch-backend и подтверждением; отказ — код 2, configure не уходит; шаг 2 спрашивает про
    антенны."""
    sent = []

    def fake_ssh(host, cmd, timeout=120, stream=False):
        sent.append((host, cmd))
        if "modulectl" in cmd:
            return 0, json.dumps({"result": "ok", "module": {"telemetry": {
                "state": "READY", "output": "neutral", "settings": {"backend": "crsf-uart"},
                "crsf": {"device": {"name": "RM Nomad X-Band"}}}}})
        return 0, nb.RESULT + json.dumps({"ok": True, "step": int(cmd.split("'--step' '")[1][0]), "text": "ok"})

    monkeypatch.setattr(nb, "ssh", fake_ssh)
    monkeypatch.setattr(nb, "own_source", lambda: "print(1)")
    assert nb.main(["--host", "pi2", "--steps", "1"], prompt=lambda q: pytest.fail("лишний вопрос")) == 0
    assert len(sent) == 1 and sent[0][0] == "pi2"
    sent.clear()
    assert nb.main(["--host", "pi2", "--steps", "1,2"], prompt=lambda q: "нет") == 2
    assert len(sent) == 1                                              # шаг 2 не выполнен без антенн
    sent.clear()
    answers = iter(["да", "нет"])
    assert nb.main(["--host", "pi2", "--switch-backend"], prompt=lambda q: next(answers)) == 2
    assert not any("modulectl" in c for _h, c in sent)
    sent.clear()
    assert nb.main(["--host", "pi2", "--switch-backend", "--yes", "--console", "pi1"]) == 0
    assert sent[-1][0] == "pi1" and "configure backend=crsf-uart" in sent[-1][1] and "--expect READY" in sent[-1][1]
    assert "Вернуть лабораторный" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        nb.main(["--steps", "1"])                                       # ни --host, ни --local
    with pytest.raises(SystemExit):
        nb.main(["--local", "--steps", "4"])


def test_bringup_stops_on_failed_step(monkeypatch):
    sent = []

    def fake_ssh(host, cmd, timeout=120, stream=False):
        sent.append(cmd)
        return 0, nb.RESULT + json.dumps({"ok": False, "step": 1, "text": "мусор в линии"})

    monkeypatch.setattr(nb, "ssh", fake_ssh)
    monkeypatch.setattr(nb, "own_source", lambda: "print(1)")
    assert nb.main(["--host", "pi2", "--yes"]) == 1 and len(sent) == 1


class EmuUart:
    """UartPort для шагов nomad_bringup поверх ядра эмулятора ELRS TX (как будто модуль на линии, эхо вычтено)."""
    emu = None

    def __init__(self, path, baud=400000, half_duplex=True):
        elrs = pytest.importorskip("sheregesh_emulators.elrs_tx")
        import time as _t
        if EmuUart.emu is None:
            EmuUart.emu = elrs.ElrsTxEmulator(_t.monotonic)
        self.inner, self.tx, self.termios2 = elrs.LoopbackPort(EmuUart.emu), 0, True
        self.echo_bytes = 0

    def write(self, data):
        self.tx += len(data)
        self.echo_bytes += len(data)
        return self.inner.write(data)

    def read(self):
        return self.inner.read()

    def stats(self):
        return {"tx_bytes": self.tx, "rx_bytes": 0, "collisions": 0, "echo_missing": 0, "write_errors": 0}

    def close(self):
        pass


def test_bringup_steps_on_emulator(monkeypatch, tmp_path):
    """Шаги 1–3 на ядре эмулятора: линия молчит, device info «RM Nomad X-Band (emu)», синхронизация 20 000 мкс."""
    frames = pytest.importorskip("sheregesh.crsf.frames")
    output = pytest.importorskip("sheregesh.crsf.output")
    pytest.importorskip("sheregesh_emulators.elrs_tx")
    dev = tmp_path / "ttyAMA3"
    dev.write_text("")
    port_mod = argparse.Namespace(UartPort=EmuUart, find_uart3=lambda: str(dev))
    monkeypatch.setattr(nb, "load_crsf", lambda: (frames, port_mod, output))
    monkeypatch.setattr(nb, "LINK_STATUS", str(tmp_path / "none.json"))
    monkeypatch.setattr(nb, "LISTEN_S", 0.2)
    monkeypatch.setattr(nb, "NEUTRAL_S", 1.5)
    monkeypatch.setattr(nb, "WARMUP_S", 3.0)
    EmuUart.emu = None
    a = argparse.Namespace(device="auto", baud=400000)
    r1 = nb.step1(a)
    assert r1["ok"] and r1["data"]["bytes"] == 0, r1
    r2 = nb.step2(a)
    assert r2["ok"] and r2["data"]["device"]["name"] == "RM Nomad X-Band (emu)", r2
    r3 = nb.step3(a)
    assert r3["ok"] and r3["data"]["period_us"] == 20000 and r3["data"]["synced"], r3
    (tmp_path / "ls.json").write_text(json.dumps({"module": {"backend": "crsf-uart"}}))
    monkeypatch.setattr(nb, "LINK_STATUS", str(tmp_path / "ls.json"))
    r = nb.step1(a)
    assert not r["ok"] and "backend crsf-uart" in r["text"]
    monkeypatch.setattr(port_mod, "find_uart3", lambda: None)
    r = nb.step1(a)
    assert not r["ok"] and "dtoverlay=uart3" in r["text"]
