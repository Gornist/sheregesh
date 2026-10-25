# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Приёмка S4 (tools/probe/s4_accept.py): суждение без плат — slot_guard для pi1 на B, потеря/возврат, ресурсы,
рендеринг, журнал, план."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "probe"))
sys.path.insert(0, str(ROOT / "tools" / "deploy" / "ota-check"))
import s4_accept as s  # noqa: E402

RETRY = ROOT / "docs/acceptance/e2e/ota-pi1/cycle2/retry"


def chk(cid, status="PASS", detail=""):
    return {"id": cid, "status": status, "detail": detail}


# ---------------------------------------------------------------- VID-09


def test_pi1_b_committed_real_snapshot_two_expected_fails():
    """Настоящий снимок pi1 после COMMITTED B: unchanged даёт ровно ожидаемые FAIL → шаг PASS."""
    import slot_guard as g
    snap = json.loads((RETRY / "pi1-final.json").read_text())
    v, checks = g.compare(snap, snap, "unchanged", "x")
    assert v == "FAIL"
    assert s.judge_slot_guard("pi1", v, checks) == ("PASS", [])


def test_pi2_real_snapshot_requires_clean_pass():
    import slot_guard as g
    snap = json.loads((RETRY / "pi2-final.json").read_text())
    v, checks = g.compare(snap, snap, "unchanged", "x")
    assert s.judge_slot_guard("pi2", v, checks) == ("PASS", [])


def test_pi1_expected_fail_with_other_detail_is_fail():
    checks = [chk("slot.active_unchanged", "FAIL", "A → B"),
              chk("bootconfig.autoboot", "FAIL", s.PI1_B_EXPECTED["bootconfig.autoboot"])]
    v, why = s.judge_slot_guard("pi1", "FAIL", checks)
    assert v == "FAIL" and "A → B" in why[0]


def test_pi1_missing_expected_fail_means_wrong_state():
    v, why = s.judge_slot_guard("pi1", "PASS", [chk("health")])
    assert v == "FAIL" and len(why) == 2


def test_console_unit_change_allowed_only_after_gui_step():
    checks = [chk(k, "FAIL", d) for k, d in s.PI1_B_EXPECTED.items()]
    checks.append(chk("same.unit.sheregesh-console", "FAIL", "InvocationID…"))
    assert s.judge_slot_guard("pi1", "FAIL", checks, console_restarted=False)[0] == "FAIL"
    assert s.judge_slot_guard("pi1", "FAIL", checks, console_restarted=True)[0] == "PASS"


def test_pi2_any_fail_and_error():
    assert s.judge_slot_guard("pi2", "FAIL", [chk("bootconfig.autoboot", "FAIL", "x")])[0] == "FAIL"
    assert s.judge_slot_guard("pi2", "ERROR", [chk("evidence.до", "ERROR", "x")])[0] == "ERROR"


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
