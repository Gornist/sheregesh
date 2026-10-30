# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Приёмка S4 (tools/probe/s4_accept.py): суждение без плат — slot_guard для pi1 на B, потеря/возврат, ресурсы,
рендеринг, журнал, план."""
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "probe"))
sys.path.insert(0, str(ROOT / "tools" / "deploy" / "ota-check"))
import s4_accept as s  # noqa: E402

RETRY = ROOT / "tests/fixtures/ota-pi1-cycle2-retry"   # снимки pi1, OTA E2E-13 цикл 2 (docs/acceptance/ARCHIVE.md)


def chk(cid, status="PASS", detail=""):
    return {"id": cid, "status": status, "detail": detail}


# ---------------------------------------------------------------- VID-09


def _real(board):
    import slot_guard as g
    snap = json.loads((RETRY / f"{board}-final.json").read_text())
    return g, snap


def _judge(board, before, after, console_restarted=False, drop=()):
    import slot_guard as g
    v, checks = g.compare(before, after, "unchanged", "x")
    checks = [c for c in checks if c["id"] not in drop]
    return s.judge_slot_guard(board, v, checks, console_restarted, before=before, after=after)


def _with_ota(snap, **state):
    snap = copy.deepcopy(snap)
    ota = json.loads(snap["fields"]["ota.status"])
    for k, v in state.items():
        if k in ("active", "tryboot_now"):
            ota["slots"][k] = v
        else:
            ota["state"][k] = v
    snap["fields"]["ota.status"] = json.dumps(ota, ensure_ascii=False)
    return snap


@pytest.mark.parametrize("board", ["pi1", "pi2"])
def test_real_snapshot_unchanged_passes_on_its_actual_slot(board):
    """Сохранённые снимки цикла 2: pi1 на B (COMMITTED), pi2 на A (ROLLED_BACK). slot_guard считает направление от
    слота снимка «до» (cf200e2) и даёт чистый PASS; судья S4 проверяет по данным снимка, что плата в покое OTA и слот
    согласован, не требуя исторических FAIL."""
    _g, snap = _real(board)
    assert _judge(board, snap, snap) == ("PASS", [])


def test_slot_change_wrong_autoboot_and_ota_not_idle_fail():
    g, snap = _real("pi1")
    moved = _with_ota(snap, active="A")                   # плата на другом слоте, чем в снимке «до»
    v, why = _judge("pi1", snap, moved)
    assert v == "FAIL" and any("слот" in w for w in why), why
    bad_ab = copy.deepcopy(snap)
    bad_ab["fields"]["autoboot"] = "[all];tryboot_a_b=1;boot_partition=2;[tryboot];boot_partition=3;"
    v, why = _judge("pi1", snap, bad_ab)
    assert v == "FAIL" and any("bootconfig.autoboot" in w for w in why), why
    busy = _with_ota(snap, state="TRYBOOT")
    v, why = _judge("pi1", snap, busy)
    assert v == "FAIL" and any("TRYBOOT" in w for w in why), why
    trial = _with_ota(snap, tryboot_now=True)
    v, why = _judge("pi1", snap, trial)
    assert v == "FAIL" and any("пробн" in w for w in why), why


def test_missing_required_check_or_snapshot_is_not_pass():
    _g, snap = _real("pi2")
    v, why = _judge("pi2", snap, snap, drop=("slot.active_unchanged",))
    assert v == "FAIL" and any("slot.active_unchanged" in w for w in why), why
    v, why = s.judge_slot_guard("pi2", "PASS", [chk(c) for c in s.REQUIRED_PASS])
    assert v == "ERROR" and why                           # без снимков нечем проверить конфигурацию платы


def test_unknown_fail_and_any_error():
    _g, snap = _real("pi1")
    checks = [chk(c) for c in s.REQUIRED_PASS] + [chk("same.package", "FAIL", "a → b")]
    v, why = s.judge_slot_guard("pi1", "FAIL", checks, before=snap, after=snap)
    assert v == "FAIL" and why == ["same.package: a → b"]
    checks = [chk(c) for c in s.REQUIRED_PASS] + [chk("evidence.до", "ERROR", "x")]
    assert s.judge_slot_guard("pi1", "ERROR", checks, before=snap, after=snap)[0] == "ERROR"


def test_console_unit_change_allowed_only_on_pi1_after_gui_step():
    _g, snap = _real("pi1")
    checks = [chk(c) for c in s.REQUIRED_PASS] + [chk("same.unit.sheregesh-console", "FAIL", "InvocationID…")]
    assert s.judge_slot_guard("pi1", "FAIL", checks, console_restarted=False, before=snap, after=snap)[0] == "FAIL"
    assert s.judge_slot_guard("pi1", "FAIL", checks, console_restarted=True, before=snap, after=snap)[0] == "PASS"
    _g, snap2 = _real("pi2")
    assert s.judge_slot_guard("pi2", "FAIL", checks, console_restarted=True, before=snap2, after=snap2)[0] == "FAIL"


# ---------------------------------------------------------------- VID-05/06


def ui(t, live=True, src="synthetic"):
    return (t, 1000 + t, {"videoSource": {"live": live, "input": src}})


def test_first_at_and_live_predicate():
    rows = [ui(0.0), ui(1.0), ui(2.5, live=False), ui(6.0), ui(7.0, src="rtsp")]
    assert s.first_at(rows, 0.5, lambda d: not s.console_live(d)) == 2.0
    assert s.first_at(rows, 3.0, s.console_live) == 3.0
    assert s.first_at(rows, 0.0, lambda d: s.console_live(d, "rtsp")) == 7.0
    assert s.console_live({"videoSource": {"live": True, "input": "RTSP"}}, "rtsp")


def test_remote_is_type_or_input():
    st = {"video": {"source": {"type": "rtsp", "input": "RTSP", "state": "streaming"}}}
    assert s.remote_is(st, "rtsp") and not s.remote_is(st, "synthetic")
    assert not s.remote_is({"video": {"source": {"type": "rtsp", "state": "connect-error"}}}, "rtsp")


def test_judge_fault_thresholds():
    ok = {"off_s": 1.8, "back_s": 4.0, "remote_fault_state": "no-signal"}
    assert s.judge_fault(ok, ("no-signal",))[:2] == ("PASS", "PASS")
    slow = {"off_s": 3.4, "back_s": 10.5, "remote_fault_state": "no-signal"}
    v5, v6, p = s.judge_fault(slow, ("no-signal",))
    assert (v5, v6) == ("FAIL", "FAIL") and len(p) == 2
    never = {"off_s": None, "back_s": None, "remote_fault_state": None}
    v5, v6, p = s.judge_fault(never, ("connect-error", "no-signal"))
    assert (v5, v6) == ("FAIL", "FAIL") and len(p) == 3
    wrong_state = {"off_s": 1.0, "back_s": 2.0, "remote_fault_state": "error"}
    assert s.judge_fault(wrong_state, ("no-signal",))[:2] == ("FAIL", "PASS")


def test_agg_and_overall_verdict():
    assert s.agg([]) == "ERROR"
    assert s.agg(["PASS", "ERROR"]) == "ERROR" and s.agg(["ERROR", "FAIL"]) == "FAIL"
    r = {"A": {"verdict": "PASS"}, "B": {"verdict": "SKIP"}}
    assert s.verdict_of(r) == "PASS"
    assert s.verdict_of({**r, "C": {"verdict": "ERROR"}}) == "ERROR"
    assert s.verdict_of({**r, "C": {"verdict": "ERROR"}, "D": {"verdict": "FAIL"}}) == "FAIL"
    assert s.verdict_of({"B": {"verdict": "SKIP"}}) == "SKIP"


# ---------------------------------------------------------------- VID-07


def res(fds=14, thr=6, gst=1, orphans=(), nr=0, pid="3847", zombies=0):
    return {"procs": [{"pid": pid, "rss_kb": 1, "threads": thr, "fds": fds}], "gst_count": gst,
            "gst_orphans": list(orphans), "nrestarts": nr, "service": {"MainPID": pid}, "zombies": zombies}


def test_resources_stable_within_tolerance():
    b = {"remote": res(), "console": res(fds=40, pid="900")}
    a = {"remote": res(fds=16, thr=7), "console": res(fds=42, pid="900")}
    assert s.judge_resources(b, a) == ("PASS", [])


def test_resources_leaks_and_extra_pipelines():
    b = {"remote": res(), "console": res(pid="900")}
    a = {"remote": res(fds=30, gst=2, zombies=1), "console": res(pid="901", nr=1)}
    v, p = s.judge_resources(b, a)
    assert v == "FAIL"
    text = " ".join(p)
    for needle in ("конвейеров 2", "зомби", "дескрипторы", "NRestarts", "MainPID", "не найден"):
        assert needle in text


def test_spec_json_for_switch():
    assert json.loads(s.spec_json("synthetic")) == {"source": "synthetic"}
    assert json.loads(s.spec_json("rtsp", "rtsp://10.0.0.1:8554/s4")) == {
        "source": "rtsp", "url": "rtsp://10.0.0.1:8554/s4", "codec": "h264"}


# ---------------------------------------------------------------- VID-03/04 рендеринг


def shot(name, src="synthetic", changed=None):
    d = {"shot": name, "videoSource": {"input": src}}
    if changed is not None:
        d["frame_changed"] = changed
    return d


def test_gui_full_sequence():
    st = [shot("01-02-stream-video.png"), shot("01-02-stream-video-t+1s.png", changed=True),
          shot("03-04-no-signal-video-t+1s.png", changed=False),
          shot("05-06-recovered-no-signal-video-t+1s.png", changed=True)]
    r = s.judge_gui(st, want_fault="no-signal")
    assert {k: v[0] for k, v in r.items()} == {"VID-03": "PASS", "VID-04": "PASS", "VID-05": "PASS", "VID-06": "PASS"}


def test_gui_frozen_or_missing():
    r = s.judge_gui([shot("01-02-stream-video-t+1s.png", changed=False)], want_fault="no-signal")
    assert r["VID-03"][0] == "PASS" and r["VID-04"][0] == "FAIL"
    assert r["VID-05"][0] == "ERROR" and r["VID-06"][0] == "ERROR"
    assert s.judge_gui([])["VID-03"][0] == "ERROR"
    assert s.judge_gui([shot("01-02-stream-video-t+1s.png", src="rtsp", changed=True)])["VID-03"][0] == "FAIL"
    r = s.judge_gui([shot("03-04-no-signal-video-t+1s.png", changed=True)], want_fault="no-signal")
    assert r["VID-05"][0] == "FAIL"                       # «нет сигнала», а кадр меняется — старое изображение


# ---------------------------------------------------------------- VID-08 и план


def test_journal_problems_filters_noise():
    lines = ["Oct 25 kernel: vc4-drm gpu: swiotlb buffer is full",
             "sheregesh-console stopped: result=success code=exited status=0",
             "sheregesh-remote-net.service: Main process exited, code=killed, status=9/KILL",
             "sheregesh-console.service: Failed with result 'exit-code'.",
             "sheregesh-console stopped: result=exit-code code=exited status=143",
             "Traceback (most recent call last):",
             "video: session from 192.168.1.92"]
    bad = s.journal_problems(lines)
    assert len(bad) == 4 and not any("swiotlb" in b or "result=success" in b for b in bad)


def test_plan_without_ssh(capsys):
    assert s.main(["--plan"]) == 0
    out = capsys.readouterr().out
    assert "VID-09" in out and "Модели" in out and "VID-07" in out


def test_set_source_uses_source_cmd_or_main_pid_hup(monkeypatch):
    import argparse
    calls = []
    monkeypatch.setattr(s.subprocess, "run", lambda cmd, shell, timeout: calls.append(cmd) or
                        argparse.Namespace(returncode=0))
    a = argparse.Namespace(console="c", remote="r", rtsp_url="rtsp://h:8554/s4", rtsp_codec="mjpeg",
                           source_cmd="select-video {source} {url}")
    st = s.Stand.__new__(s.Stand)
    st.a = a
    assert st.set_source("rtsp") and calls == ["select-video rtsp rtsp://h:8554/s4"]
    sent = []
    monkeypatch.setattr(s, "ssh", lambda h, c, timeout=60: sent.append((h, c)) or (0, ""))
    a.source_cmd = None
    assert st.set_source("synthetic")
    host, cmd = sent[0]
    assert host == "r" and "kill -HUP $(systemctl show -p MainPID --value sheregesh-remote-net)" in cmd
    assert "systemctl kill" not in cmd and '{"source": "synthetic"}' in cmd


def test_gui_step_kill_pattern_does_not_match_own_shell_and_path_has_emulators():
    """pkill -f '/tmp/gui_capture.py' в той же команде ssh убивал свою оболочку до `systemctl start` (пульт оставался
    остановлен); backend пульта импортирует sheregesh_emulators — путь как у launcher.py."""
    src = (ROOT / "tools/probe/s4_accept.py").read_text()
    assert "-f '/tmp/gui_capture.py'" not in src and "-f '[/]tmp/gui_capture.py'" in src
    assert "$C/software/emulators" in src


def test_gui_fault_judged_by_dark_field_not_by_unchanged_hash():
    """Подпись «no frames for N s» меняет хеш поля кадра при отказе — критерий VID-05 по доле чёрного."""
    def shots(prefix, dark, changed, src="synthetic"):
        return [{"shot": f"{prefix}-video-t+1s.png", "frame_changed": changed, "frame_dark": dark,
                 "videoSource": {"input": src}}]
    base = shots("01-02-stream", 0.05, True) + shots("05-06-recovered-no-signal", 0.05, True)
    ok = s.judge_gui(base + shots("03-04-no-signal", 0.93, True), want_fault="no-signal")
    assert ok["VID-05"][0] == "PASS" and ok["VID-06"][0] == "PASS"
    bad = s.judge_gui(base + shots("03-04-no-signal", 0.10, True), want_fault="no-signal")
    assert bad["VID-05"][0] == "FAIL"


def test_srtp_fault_seen_on_console_rx():
    """ADR-0016: под SRTP источник блока — служба tx; при её заморозке состояние блока не наблюдаемо, отказ виден по
    переходам службы rx пульта (degraded/recovering) после внесения отказа."""
    t = 1793340000.0
    tr = [[t - 50, "streaming", "first frame"], [t + 1.2, "degraded", "no frames > 1 s"],
          [t + 2.2, "recovering", "PLI sent"], [t + 6.0, "streaming", "frames"]]
    assert s.rx_fault(tr, t) == "rx:degraded"
    assert s.rx_fault(tr[:1], t) is None                                     # до отказа — не считается
    assert s.rx_fault([[t - 30, "degraded", "x"]], t) is None
    assert s.rx_fault([["bad"], None, [t + 1, "recovering", "PLI"]], t) == "rx:degraded"
    m = {"off_s": 1.1, "back_s": 3.0, "remote_fault_state": "rx:degraded", "transport": "srtp"}
    assert s.judge_fault(m, ("rx:degraded",))[:2] == ("PASS", "PASS")
    assert s.judge_fault({**m, "remote_fault_state": None}, ("rx:degraded",))[0] == "FAIL"
    assert s.jload("не json") == {} and s.jload("[1]") == {} and s.jload('{"a": 1}') == {"a": 1}
