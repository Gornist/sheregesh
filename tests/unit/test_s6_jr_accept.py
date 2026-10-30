# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Сокращённая приёмка S6 JR (tools/probe/s6_jr_accept.py, ADR-0012 §11): суждения без плат — разбор вывода
modulectl, ps и снимков плат, вердикты JR-01…JR-10 и отказов §12.4 (F-01…F-04), инвариант output = none,
сравнение OTA «до/после» на настоящих ответах агента OTA, запрет перезагрузки вне покоя OTA, команды — в пределах
правила polkit образа, план без SSH, предпроверка без действий.

Номера JR — критерии спринта, а не TEST-ID реестра: маркер test_id не ставится (ADR-0012, «Затронутые требования»)."""
import argparse
import copy
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "probe"))
sys.path.insert(0, str(ROOT / "tools" / "deploy" / "ota-check"))
import s6_jr_accept as s  # noqa: E402

RETRY = ROOT / "tests/fixtures/ota-pi1-cycle2-retry"   # снимки pi1, OTA E2E-13 цикл 2 (docs/acceptance/ARCHIVE.md)
TRIAL = json.loads((RETRY / "ota-status-trial.json").read_text())
COMMITTED = json.loads(json.loads((RETRY / "pi1-final.json").read_text())["fields"]["ota.status"])


# ---------------------------------------------------------------- разбор вывода

def test_parse_cli_tolerates_log_lines_and_pretty_json():
    flat = 'modulectl: via console\n{"result": "ok", "detail": "", "via": "console", "module": {"support": "ok"}}\n'
    assert s.parse_cli(flat)["module"]["support"] == "ok"
    pretty = 'warn {x}\n' + json.dumps({"result": "timeout", "module": {"telemetry": {"state": "FAULT"}}}, indent=2)
    d = s.parse_cli(pretty)
    assert d["result"] == "timeout" and s.telemetry(d)["state"] == "FAULT"
    assert s.parse_cli("") == {} and s.parse_cli("ssh: connect to host … refused") == {}
    assert s.parse_cli('{"no": "result"}') == {}                       # не ответ modulectl
    assert s.telemetry({"result": "ok", "module": {"telemetry": None}}) == {}


def ps_text(main=1200, rows=()):
    head = f"MainPID={main} NRestarts=0 ActiveState=active "
    base = [(1, 0, "/sbin/init"), (main, 1, "/usr/bin/python3 -s -m sheregesh_remote --link-config x")]
    return "\n".join([head] + [f"{p} {pp} {a}" for p, pp, a in base + list(rows)])


def test_parse_procs_and_emulator_ancestry():
    snap = s.parse_procs(ps_text(rows=[(1300, 1200, "python3 -s -m sheregesh_emulators.jrmodule --profile "
                                                    "radiomaster-nomad")]))
    assert snap["mainpid"] == 1200 and snap["nrestarts"] == 0 and snap["active"] == "active"
    assert s.emulator_pids(snap) == [1300] and s.ancestors(1300, snap) == [1200]
    # без setpriv (машина разработчика): между агентом и эмулятором сторож pdeath.py — он сам не эмулятор
    mac = s.parse_procs(ps_text(rows=[(1250, 1200, "python3 -I /x/pdeath.py --parent 1200 -- sh -c trap sh "
                                                   "python3 -s -m sheregesh_emulators.jrmodule"),
                                      (1300, 1250, "python3 -s -m sheregesh_emulators.jrmodule --profile p")]))
    assert s.emulator_pids(mac) == [1300] and 1200 in s.ancestors(1300, mac)
    assert s.parse_procs("")["mainpid"] is None and s.parse_procs("garbage\nx y z")["procs"] == []


HEALTH = {"state": "READY", "seq": 5, "since_s": 3.0, "profile": "radiomaster-nomad", "backend_state": "running",
          "pid": 1300, "restarts": 0, "error_code": "", "settings_revision": "r000001", "settings_source": "store"}
EMU = (1300, 1200, "python3 -s -m sheregesh_emulators.jrmodule --profile radiomaster-nomad")


def test_jr01_emulator_judgement():
    ok = s.parse_procs(ps_text(rows=[EMU]))
    v, d = s.judge_emulator(ok, HEALTH)
    assert v == "PASS" and "родитель" in d and "1300" in d
    v, d = s.judge_emulator(s.parse_procs(ps_text(rows=[EMU, (1400, 1200, EMU[2])])), HEALTH)
    assert v == "FAIL" and "процессов эмулятора 2" in d
    v, d = s.judge_emulator(s.parse_procs(ps_text(rows=[(1300, 1, EMU[2])])), HEALTH)    # сирота у init
    assert v == "FAIL" and "не потомок MainPID" in d
    assert s.judge_emulator(ok, {**HEALTH, "backend_state": "unavailable"})[0] == "FAIL"
    assert "pid 999" in s.judge_emulator(ok, {**HEALTH, "pid": 999})[1]
    v, d = s.judge_emulator(ok, {})
    assert v == "FAIL" and "нет module" in d
    assert s.judge_emulator(s.parse_procs(ps_text(rows=[]).replace("active ", "failed ")), HEALTH)[0] == "FAIL"


def cli(state="READY", support="ok", **tel):
    t = {"v": 1, "state": state, "seq": 7, "output": "none",
         "profile": {"id": "radiomaster-nomad", "name": "RadioMaster Nomad", "family": "elrs",
                     "interface": "uart-crsf"}, **tel}
    return {"result": "ok", "detail": "", "via": "console",
            "module": {"support": support, "stale": False, "session": "1.3", "telemetry": t}}


UI = {"support": "ok", "state": "READY", "seq": 7, "stale": False, "stateText": "Готов",
      "profileText": "RadioMaster Nomad (radiomaster-nomad)", "errorText": "Последняя ошибка: нет",
      "emulatorText": "Эмулятор: работает · перезапусков 0 из 3 · PID 1300", "labBanner": True, "faultEvent": False}


def test_jr02_profile():
    assert s.judge_profile(cli(), {"module": UI})[0] == "PASS"
    v, d = s.judge_profile(cli(support="old-remote"), {"module": UI})
    assert v == "FAIL" and "old-remote" in d
    assert s.judge_profile({}, {"module": UI})[0] == "ERROR"
    v, d = s.judge_profile(cli(), {"module": {**UI, "labBanner": False}})
    assert v == "FAIL" and "labBanner" in d
    v, d = s.judge_profile(cli(), {})
    assert v == "FAIL" and "нет module" in d
    other = cli()
    other["module"]["telemetry"]["profile"] = {"id": "crossfire-test", "name": "X"}
    assert s.judge_profile(other, {"module": UI})[0] == "FAIL"


def H(seq, frm, to, reason, code=""):
    return {"seq": seq, "from": frm, "state": to, "reason": reason, "code": code, "ago_s": 1.0}


REINIT = [H(9, "DETECTED", "INITIALIZING", "auto-init"), H(10, "INITIALIZING", "READY", "init-ok"),
          H(8, "RECOVERING", "DETECTED", "present"), H(7, "READY", "RECOVERING", "cmd-reinit"),
          H(6, "INITIALIZING", "READY", "init-ok")]


def test_history_helpers():
    after = s.history_after(REINIT + [{"seq": "x"}, None], 6)          # мусор второй стороны отбрасывается
    assert [h["seq"] for h in after] == [7, 8, 9, 10]
    assert s.has_transition(after, "INITIALIZING", "READY", "init-ok")
    assert not s.has_transition(after, "INITIALIZING", "READY", "other")
    hang = [H(3, "READY", "FAULT", "no-response", "MOD-NO-RESPONSE"), H(4, "FAULT", "RECOVERING", "recover-attempt"),
            H(7, "INITIALIZING", "FAULT", "init-timeout", "MOD-INIT-TIMEOUT")]
    assert s.first_fault_code(s.history_after(hang, 0)) == "MOD-NO-RESPONSE"
    assert s.first_fault_code([]) is None


def test_jr03_jr04_init_and_gui():
    m = {"rc": 0, "result": "ok", "history": s.history_after(REINIT, 6), "init_ms": 1003, "ready_s": 3.1}
    assert s.judge_init(m)[0] == "PASS"
    assert "init_ms" in s.judge_init({**m, "init_ms": 5200})[1]
    assert s.judge_init({**m, "history": s.history_after(REINIT, 10)})[0] == "FAIL"   # READY не после команды
    assert s.judge_init({**m, "rc": 1, "result": "timeout"})[0] == "FAIL"
    assert s.judge_init({**m, "ready_s": None})[0] == "FAIL"
    assert s.judge_gui_ready({"rs_ready_s": 3.0, "ui_ready_s": 4.4})[0] == "PASS"
    assert s.judge_gui_ready({"rs_ready_s": 3.0, "ui_ready_s": 2.8})[0] == "PASS"    # выборки неодновременны
    v, d = s.judge_gui_ready({"rs_ready_s": 3.0, "ui_ready_s": 5.5})
    assert v == "FAIL" and "2.5 с" in d
    assert s.judge_gui_ready({"rs_ready_s": None, "ui_ready_s": 1})[0] == "ERROR"
    assert s.judge_gui_ready({"rs_ready_s": 1.0, "ui_ready_s": None, "timeout_s": 17})[0] == "FAIL"


def test_jr05_detach():
    m = {"detach_result": "ok", "attach_result": "ok", "absent_s": 1.4, "ready_s": 2.6}
    assert s.judge_detach(m)[0] == "PASS"
    assert s.judge_detach({**m, "absent_s": 5.3})[0] == "FAIL"
    assert s.judge_detach({**m, "ready_s": None})[0] == "FAIL"
    assert "not-running" in s.judge_detach({**m, "detach_result": "not-running"})[1]


def test_jr06_fault_shown():
    iface = {"scenario": "iface-error", "result": "ok", "fault_s": 1.2, "state_text": "Ошибка интерфейса",
             "fault_event_s": 1.3}
    assert s.judge_fault_shown(iface, "iface")[0] == "PASS"
    assert s.judge_fault_shown({**iface, "state_text": "Ошибка"}, "iface")[0] == "PASS"   # §4.1 и §11 расходятся
    assert s.judge_fault_shown({**iface, "state_text": "Готов"}, "iface")[0] == "FAIL"
    assert "MODULE-FAULT" in s.judge_fault_shown({**iface, "fault_event_s": None}, "iface")[1]
    assert s.judge_fault_shown({**iface, "fault_s": 6.5}, "iface")[0] == "FAIL"
    hang = {"scenario": "hang", "result": "ok", "first_code": "MOD-NO-RESPONSE", "fault_s": 3.6, "ui_fault_s": 4.1}
    assert s.judge_fault_shown(hang, "hang")[0] == "PASS"
    v, d = s.judge_fault_shown({**hang, "first_code": "MOD-INIT-TIMEOUT"}, "hang")
    assert v == "FAIL" and "MOD-NO-RESPONSE" in d
    assert s.judge_fault_shown({**hang, "ui_fault_s": None}, "hang")[0] == "FAIL"


def test_jr07_recovered():
    hist = [H(12, "FAULT", "RECOVERING", "recover-attempt"), H(13, "RECOVERING", "DETECTED", "present"),
            H(14, "DETECTED", "INITIALIZING", "auto-init"), H(15, "INITIALIZING", "READY", "init-ok")]
    err = {"code": "MOD-IFACE-ERROR", "text": "x", "since_s": 9.0, "state": "READY", "active": False}
    m = {"result": "ok", "ready_s": 4.2, "ui_ready_s": 4.9, "history": hist, "error": err,
         "error_text": "Последняя ошибка: Ошибка интерфейса в 14:02:31"}
    assert s.judge_recovered(m, {"MOD-IFACE-ERROR"})[0] == "PASS"
    assert "active" in s.judge_recovered({**m, "error": {**err, "active": True}}, {"MOD-IFACE-ERROR"})[1]
    v, d = s.judge_recovered({**m, "history": hist + [H(11, "READY", "RECOVERING", "cmd-reinit")]},
                             {"MOD-IFACE-ERROR"})
    assert v == "FAIL" and "cmd-reinit" in d
    assert s.judge_recovered({**m, "history": hist[1:]}, {"MOD-IFACE-ERROR"})[0] == "FAIL"
    to = {**m, "error": {**err, "code": "MOD-INIT-TIMEOUT"}, "error_text": "Последняя ошибка: Инициализация не "
                                                                          "завершилась за 5 с в 14:03:00"}
    assert s.judge_recovered(to, {"MOD-NO-RESPONSE", "MOD-INIT-TIMEOUT"})[0] == "PASS"
    assert "errorText" in s.judge_recovered({**to, "error_text": "нет"}, {"MOD-INIT-TIMEOUT"})[1]
    assert s.judge_recovered({**m, "ready_s": 21}, {"MOD-IFACE-ERROR"})[0] == "FAIL"


def test_jr08_crash_unavailable_restart():
    c = {"fault_code": "EMU-EXITED", "ready_s": 4.0, "pid0": 1300, "pid1": 1450, "restarts0": 0, "restarts1": 1,
         "emulators": 1}
    assert s.judge_crash(c)[0] == "PASS"
    assert s.judge_crash({**c, "pid1": 1300})[0] == "FAIL"
    assert s.judge_crash({**c, "restarts1": 0})[0] == "FAIL"
    assert s.judge_crash({**c, "fault_code": "EMU-NO-KEEPALIVE"})[0] == "FAIL"
    assert s.judge_crash({**c, "emulators": 2})[0] == "FAIL"
    assert s.judge_crash({"error": "pkill не нашёл"})[0] == "ERROR"
    u = {"unavail_s": 9.5, "hold_s": 30, "procs_during": [], "restarts_grew": False, "running_seen": False,
         "journal_ok": True, "journal_started": 0, "available_result": "ok", "reset_result": "ok", "ready_s": 4.0}
    assert s.judge_unavailable(u)[0] == "PASS"
    assert "журнал службы недоступен" in s.judge_unavailable({**u, "journal_ok": False, "journal_started": None})[1]
    for bad in ({"procs_during": [1500]}, {"restarts_grew": True}, {"running_seen": True}, {"journal_started": 1},
                {"unavail_s": None}, {"reset_result": "disabled"}, {"ready_s": 16.0}):
        assert s.judge_unavailable({**u, **bad})[0] == "FAIL", bad
    r = {"rc": 0, "mainpid0": 1200, "mainpid1": 2200, "nolink_s": 0.8, "ready_s": 9.0, "emulators": 1,
         "parent_ok": True, "old_alive": False, "emu_pid0": 1300, "nrestarts_remote0": 0, "nrestarts_remote1": 0,
         "nrestarts_console0": 0, "nrestarts_console1": 0}
    assert s.judge_service_restart(r)[0] == "PASS"
    assert s.judge_service_restart({**r, "mainpid1": 1200})[0] == "ERROR"
    assert s.judge_service_restart({**r, "rc": 1})[0] == "ERROR"
    for bad in ({"nolink_s": None}, {"old_alive": True}, {"parent_ok": False}, {"nrestarts_remote1": 1},
                {"nrestarts_console1": 2}, {"ready_s": 31.0}):
        assert s.judge_service_restart({**r, **bad})[0] == "FAIL", bad


def test_jr09_settings():
    ev = {"event": "systemctl restart sheregesh-remote-net", "disabled_s": 6.0, "state": "DISABLED", "nolink_s": 0.4,
          "ui_disabled_s": 7.1,
          "revision": "r000003", "source": "store", "emulators": [], "timeout_s": 30}
    m = {"off_result": "ok", "revision": "r000003", "events": [ev], "on_result": "ok", "ready_s": 5.0}
    assert s.judge_settings(m)[0] == "PASS"
    assert s.judge_settings({**m, "off_result": "not-saved"})[0] == "ERROR"
    for bad in ({"revision": "r000001"}, {"source": "factory"}, {"emulators": [1500]}, {"state": "READY"},
                {"disabled_s": None}, {"nolink_s": None, "ui_disabled_s": None}):
        assert s.judge_settings({**m, "events": [{**ev, **bad}]})[0] == "FAIL", bad
    blocked = {"event": "systemctl reboot pi2", "verdict": "BLOCKED", "error": "перезагрузка запрещена: tryboot"}
    v, d = s.judge_settings({**m, "events": [ev, blocked]})
    assert v == "BLOCKED" and "tryboot" in d
    assert s.judge_settings({**m, "events": [ev, {**ev, "event": "systemctl reboot pi2"}]})[0] == "PASS"
    assert s.judge_settings({**m, "ready_s": None})[0] == "FAIL"


def test_reboot_only_at_rest():
    assert s.reboot_allowed(s.P2.ota_summary(COMMITTED))[0] is True
    ok, why = s.reboot_allowed(s.P2.ota_summary(TRIAL))
    assert ok is False and "tryboot_now" in why and "COMMITTED" in why
    assert s.reboot_allowed(None)[0] is False
    odd = copy.deepcopy(COMMITTED)
    odd["slots"]["active"] = "A"                                   # загружен не подтверждённый слот
    assert s.reboot_allowed(s.P2.ota_summary(odd))[0] is False


def board_text(ota=COMMITTED, deploy="/var/lib/sheregesh-deploy/releases/0.1.0~dev0+gabc", boot="b-1",
               commit="bd74c8c4", failed=""):
    return "\n".join(["== ota", json.dumps(ota), "== deploy", deploy, "== boot", boot, "== build",
                      json.dumps({"commit": commit, "ota_version": "0.1.1"}), "== failed", failed])


def test_jr10_board_state_and_ota_unchanged():
    b = s.board_state(board_text())
    assert b["ota"]["state"] == "COMMITTED" and b["boot_id"] == "b-1" and b["build_commit"] == "bd74c8c4"
    assert b["failed"] == [] and b["deploy"].endswith("gabc")
    assert s.board_state("")["ota"] is None
    before = {"pi1": b, "pi2": s.board_state(board_text(boot="b-2"))}
    assert s.judge_ota(before, copy.deepcopy(before)) == ("PASS", [])
    after = copy.deepcopy(before)
    after["pi2"] = s.board_state(board_text(boot="b-3"))
    v, p = s.judge_ota(before, after)
    assert v == "FAIL" and any("pi2 перезагружался" in x for x in p)
    assert s.judge_ota(before, after, rebooted=True)[0] == "PASS"            # --reboot: только pi2
    after["pi1"] = s.board_state(board_text(boot="b-9"))
    assert s.judge_ota(before, after, rebooted=True)[0] == "FAIL"
    trial = copy.deepcopy(before)
    trial["pi2"] = s.board_state(board_text(ota=TRIAL, boot="b-2"))
    v, p = s.judge_ota(before, trial)
    assert v == "FAIL" and any("ota.state" in x for x in p) and any("tryboot_now" in x for x in p)
    dev = copy.deepcopy(before)
    dev["pi1"] = s.board_state(board_text(deploy="factory"))
    assert any("deploy" in x for x in s.judge_ota(before, dev)[1])
    failed = copy.deepcopy(before)
    failed["pi2"] = s.board_state(board_text(boot="b-2", failed="x.service loaded failed failed X"))
    assert s.judge_ota(before, failed)[0] == "FAIL"
    assert s.judge_ota(before, {"pi1": b, "pi2": s.board_state("")})[0] == "ERROR"


def test_failure_checks_12_4():
    gap = {"stop_rc": 0, "nolink_s": 0.6, "ready_during_gap": 0, "gap_docs": 30, "start_rc": 0, "ready_s": 8.0}
    assert s.judge_no_link_gap(gap)[0] == "PASS"
    assert s.judge_no_link_gap({**gap, "ready_during_gap": 3})[0] == "FAIL"
    assert s.judge_no_link_gap({**gap, "nolink_s": None})[0] == "FAIL"
    assert s.judge_no_link_gap({**gap, "stop_rc": 1})[0] == "ERROR"
    uf = {"console_stop_rc": 0, "remote_restart_rc": 0, "console_start_rc": 0, "unit_ready_s": 7.0,
          "ui_ready_s": 25.0, "ready_without_link": 0, "history_ok": True, "history_states": ["READY", "INITIALIZING"]}
    assert s.judge_unit_first(uf)[0] == "PASS"
    for bad in ({"unit_ready_s": 16.0}, {"ui_ready_s": None}, {"ready_without_link": 1}, {"history_ok": False}):
        assert s.judge_unit_first({**uf, **bad})[0] == "FAIL", bad
    assert s.judge_unit_first({**uf, "console_start_rc": 4})[0] == "ERROR"
    ok = {"want": "factory", "ready_s": 7.0, "source": "factory", "notice": ""}
    assert s.judge_settings_case(ok)[0] == "PASS"
    assert s.judge_settings_case({**ok, "source": "store"})[0] == "FAIL"
    rec = {"want": "recovered", "ready_s": 7.0, "source": "recovered", "notice": "CFG-STATE-CORRUPT,CFG-STATE-REBUILT"}
    assert s.judge_settings_case(rec)[0] == "PASS"
    assert s.judge_settings_case({**rec, "notice": ""})[0] == "FAIL"
    rst = {"want": "reset", "ready_s": 7.0, "source": "reset", "notice": "CFG-RESET",
           "corrupt_dirs": ["/home/sheregesh-ui/.config/sheregesh/remote-config.corrupt-20261026T120000"]}
    assert s.judge_settings_case(rst)[0] == "PASS"
    assert s.judge_settings_case({**rst, "corrupt_dirs": []})[0] == "FAIL"
    back = {"want": "store", "ready_s": 7.0, "source": "store", "revision": "r000004", "revision_want": "r000004"}
    assert s.judge_settings_case(back)[0] == "PASS"
    assert s.judge_settings_case({**back, "revision": "r000001"})[0] == "FAIL"
    assert s.judge_settings_case({"error": "stop: rc 4"})[0] == "ERROR"
    unit = {"seq": 40, "pid": 1450, "restarts": 0, "state": "READY"}
    eth = {"nolink_s": 2.1, "ready_s": 6.0, "unit_before": unit, "unit_after": dict(unit)}
    assert s.judge_ethernet(eth)[0] == "PASS"
    assert s.judge_ethernet({**eth, "unit_after": {**unit, "seq": 44}})[0] == "FAIL"   # блок «переходил»
    assert s.judge_ethernet({**eth, "unit_after": {**unit, "restarts": 1}})[0] == "FAIL"
    assert s.judge_ethernet({**eth, "unit_before": {**unit, "restarts": 2}})[0] == "PASS"  # окно 60 с сдвинулось
    assert s.judge_ethernet({**eth, "nolink_s": 6.0})[0] == "FAIL"
    assert s.judge_ethernet({**eth, "unit_after": {}})[0] == "FAIL"


def test_output_invariant_and_journal_probe():
    assert s.judge_output(["none"] * 5)[0] == "PASS"
    v, d = s.judge_output(["none", "unknown", None])
    assert v == "FAIL" and "2 из 3" in d
    assert s.judge_output([])[0] == "ERROR"
    assert s.journal_usable("[remote] module: READY -> FAULT (iface-fault)")
    assert not s.journal_usable("No journal files were found.")
    assert not s.journal_usable("Hint: You are currently not seeing messages from other users and the system.")
    assert not s.journal_usable("")


def test_predicates_on_samples():
    ui = {"module": UI}
    assert s.ui_state_is("READY", "Готов")(ui) and not s.ui_state_is("READY", "Ошибка")(ui)
    assert not s.ui_state_is("READY")({"module": {**UI, "stale": True}})       # данные до потери связи
    assert not s.ui_state_is("READY")({"module": {**UI, "support": "no-link"}})
    rs = {"time": 100.0, "module": HEALTH}
    assert s.rs_state_is("READY", seq_gt=4)(rs) and not s.rs_state_is("READY", seq_gt=5)(rs)
    assert s.rs_state_is("READY", after_epoch=99)(rs) and not s.rs_state_is("READY", after_epoch=100)(rs)
    assert s.rs_state_is("FAULT", code="EMU-UNAVAILABLE")({"module": {"state": "FAULT",
                                                                       "error_code": "EMU-UNAVAILABLE"}})
    rows = [(1.0, 0, {}), (2.0, 0, {"module": {"state": "FAULT"}}), (3.5, 0, rs)]
    assert s.first_row(rows, 1.5, s.rs_state_is("READY")) == (2.0, rs)
    assert s.first_row(rows, 4.0, s.rs_state_is("READY")) == (None, None)


def test_constants_match_wire_contract():
    wire = pytest.importorskip("sheregesh.module.wire")
    assert set(s.CODE_TEXT) == set(wire.ERROR_CODES)
    assert set(s.STATE_TEXT) == set(wire.STATES)
    for sc in ("attach", "detach", "iface-error", "hang", "heal", "crash", "emu-unavailable", "emu-available"):
        assert sc in wire.LAB_SCENARIOS
    assert wire.OUTPUT == "none"


# ---------------------------------------------------------------- стенд без плат

def test_modulectl_via_running_console(monkeypatch):
    sent = []
    monkeypatch.setattr(s, "ssh", lambda h, c, timeout=60: sent.append((h, c)) or
                        (0, 'modulectl: …\n' + json.dumps(cli())))
    a = argparse.Namespace(console="pi1", remote="pi2")
    st = object.__new__(s.Stand)
    st.a, st.calls, st.outputs = a, [], []
    code, d, _ = st.ctl("lab detach --wait 15")
    host, cmd = sent[0]
    assert host == "pi1" and code == 0 and d["result"] == "ok"
    assert "-m sheregesh.link.modulectl lab detach --wait 15 --via console" in cmd and "PYTHONPATH=$C/software" in cmd
    assert st.outputs == ["none"] and st.calls[0]["result"] == "ok"


def test_reboot_event_refuses_out_of_rest(monkeypatch):
    """JR-09 --reboot на пробной загрузке: ни одной команды reboot, подшаг BLOCKED."""
    sent = []
    monkeypatch.setattr(s.E, "out", lambda h, c, t=60: json.dumps(TRIAL) if "ota-client status" in c else "")
    monkeypatch.setattr(s, "ssh", lambda h, c, timeout=60: sent.append(c) or (0, ""))
    ev = s.reboot_event(argparse.Namespace(console="c", remote="r"), None)
    assert ev["verdict"] == "BLOCKED" and "запрещена" in ev["error"] and not any("reboot" in c for c in sent)


def test_preflight_mismatch_does_nothing(monkeypatch, tmp_path):
    """Чужая плата или OTA не в покое — ERROR без единой команды modulectl и systemctl."""
    sent = []
    monkeypatch.setattr(s.E, "board_identity", lambda h: {"serial": "deadbeef", "host": h})
    monkeypatch.setattr(s.E, "out", lambda h, c, t=60: board_text() if "== ota" in c else "")
    monkeypatch.setattr(s.E, "ssh", lambda h, c, t=60: sent.append(c) or (0, ""))
    monkeypatch.setattr(s, "ssh", lambda h, c, timeout=60: sent.append(c) or (0, ""))
    assert s.main(["--out", str(tmp_path)]) == 2
    summ = json.loads((tmp_path / "summary.json").read_text())
    assert summ["verdict"] == "ERROR" and any("серийный номер" in p for p in summ["preflight"])
    assert not any("modulectl" in c or "systemctl" in c for c in sent)


def test_commands_within_image_polkit_rules():
    """systemctl от sheregesh-ui — только глаголы и юниты правила 50 (без sudo, enable, set-property); OTA только
    читается; интерфейсы плат не трогаются (обрыв Ethernet — ручной шаг F-04)."""
    r50 = (ROOT / "os/layer/sheregesh-dev-access.d/50-sheregesh-dev.rules").read_text()
    for unit in (s.REMOTE_UNIT, s.CONSOLE_UNIT):
        assert f'"{unit}.service"' in r50
    run = re.search(r"var run = \[([^\]]*)\]", r50).group(1)
    src = (ROOT / "tools/probe/s6_jr_accept.py").read_text()
    verbs = set(re.findall(r'systemctl\((?:a\.\w+|host), "(\w+)", (\w+)', src))
    assert verbs and all(f'"{v}"' in run for v, _u in verbs), verbs
    assert {u for _v, u in verbs} <= {"REMOTE_UNIT", "CONSOLE_UNIT", "unit"}
    assert "org.freedesktop.login1.reboot" in r50 and src.count("systemctl --no-ask-password reboot") == 1
    assert not re.search(r"\bsudo\s+[a-z/]", src)
    for bad in ("reset-failed", "set-property", "systemctl enable", "ask-password enable", "ip link", "ifconfig",
                "nmcli", "ota-tryboot", "ota-confirm", "ota-install", "ota-abort", "shdeploy.py deploy", "rm -rf",
                "rm -f"):
        assert bad not in src, bad
    assert set(re.findall(r"sheregesh-ota-client (\w+)", src)) == {"status"}


def test_plan_without_ssh(capsys):
    assert s.main(["--plan"]) == 0
    out = capsys.readouterr().out
    assert all(k in out for k in ("JR-01", "JR-05", "JR-08c", "JR-09", "JR-10 до", "JR-10 после", "F-01", "F-03",
                                  "F-04", "OUT"))
    assert "перезагрузка pi2: нет" in out and "ручной шаг" in out and "без удалений" in out
    assert s.main(["--plan", "--reboot", "--manual-ethernet", "--hold", "10"]) == 0
    out = capsys.readouterr().out
    assert "tryboot_now=false" in out and "по подсказкам" in out and "10 с без процессов" in out
    with pytest.raises(SystemExit):
        s.main([])                                                  # без --out — ошибка аргументов, не прогон
    with pytest.raises(SystemExit):
        s.main(["--out", "/nonexistent-x", "--only", "VID-01"])


def test_start_guard_keeps_within_unit_start_limit(monkeypatch):
    """Прогон 2026-10-26: шестой запуск блока за 300 с дал start-limit-hit. Пятый запуск в окне (с учётом запусков из
    журнала платы) ждёт выхода старейшего из окна; stop ждёт так же, но запуском не считается; другие службы и платы
    считаются отдельно."""
    t, slept = [1000.0], []
    now = lambda: t[0]                                               # noqa: E731

    def sleep(d):
        slept.append(d)
        t[0] += d

    def guard(host, unit, record=True):
        s.start_guard(host, unit, record, now, sleep, seed=lambda h, u: [100.0] if (h, u) == ("pi2", "u") else [])

    monkeypatch.setattr(s, "_starts", {})
    for _ in range(s.START_BUDGET - 1):                              # + один запуск из журнала 100 с назад
        guard("pi2", "u")
        t[0] += 10
    guard("pi1", "u")
    guard("pi2", "other")
    assert slept == []
    guard("pi2", "u", record=False)                                  # stop перед пятым запуском — ждёт
    assert slept == [pytest.approx(s.START_WINDOW_S - 130 + 2)]
    guard("pi2", "u")                                                # сам запуск после паузы — без ожидания
    assert len(slept) == 1
    assert len([x for x in s._starts[("pi2", "u")] if t[0] - x < s.START_WINDOW_S]) == s.START_BUDGET


def test_board_starts_parses_journal(monkeypatch):
    out = ("1000.5\n990.5 host systemd[1]: Starting x.service - X...\n700.0 host systemd[1]: Starting x.service\n")
    monkeypatch.setattr(s, "ssh", lambda host, cmd, timeout=60: (0, out))
    assert s.board_starts("pi2", "x.service") == [pytest.approx(10.0), pytest.approx(300.5)]
    monkeypatch.setattr(s, "ssh", lambda host, cmd, timeout=60: (255, ""))
    assert s.board_starts("pi2", "x.service") == []
