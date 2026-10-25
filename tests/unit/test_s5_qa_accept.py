# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Приёмка S5 адаптера (tools/probe/s5_accept.py): суждения без плат — выбор источника, BLOCKED, ручной отказ,
GUI-путь выбора, рендеринг отказа (поле кадра только при ярком потоке), OTA относительно базовой линии, план."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "probe"))
sys.path.insert(0, str(ROOT / "tools" / "deploy" / "ota-check"))
import s5_accept as s  # noqa: E402

RETRY = ROOT / "docs/acceptance/e2e/ota-pi1/cycle2/retry"
SOURCES = [{"id": "synthetic", "input": "synthetic", "available": True},
           {"id": "v4l2:534d:2109", "input": "hdmi", "available": True},
           {"id": "rtsp:0", "input": "rtsp", "available": True}]


def test_resolve_source_and_blocked():
    assert s.resolve_source(SOURCES, "v4l2") == ("v4l2:534d:2109", None)
    assert s.resolve_source(SOURCES, "v4l2:534d:2109") == ("v4l2:534d:2109", None)
    sid, why = s.resolve_source(SOURCES[:1], "v4l2")
    assert sid is None and why.startswith(s.HW_UNAVAILABLE)
    sid, why = s.resolve_source(SOURCES, "v4l2:1b71:3002")
    assert sid is None and why.startswith(s.HW_UNAVAILABLE)
    gone = [dict(SOURCES[1], available=False)]
    assert s.resolve_source(gone, "v4l2:534d:2109")[1].startswith(s.HW_UNAVAILABLE)
    two = SOURCES + [{"id": "v4l2:1b71:3002", "input": "cvbs", "available": True}]
    sid, why = s.resolve_source(two, "v4l2")
    assert sid is None and "несколько" in why and not why.startswith(s.HW_UNAVAILABLE)
    assert s.resolve_source(None, "v4l2")[1].startswith(s.HW_UNAVAILABLE)


def test_verdict_order_blocked_never_pass():
    r = {"a": {"verdict": "PASS"}, "b": {"verdict": s.BLOCKED}, "c": {"verdict": "SKIP"}}
    assert s.verdict_of(r) == s.BLOCKED
    assert s.verdict_of({**r, "d": {"verdict": "ERROR"}}) == "ERROR"
    assert s.verdict_of({**r, "d": {"verdict": "FAIL"}, "e": {"verdict": "ERROR"}}) == "FAIL"
    assert s.verdict_of({"a": {"verdict": "PASS"}, "c": {"verdict": "SKIP"}}) == "PASS"
    assert s.verdict_of({"c": {"verdict": "SKIP"}}) == "SKIP"


def test_spec_and_merged_config_keeps_rtsp_urls():
    spec = s.v4l2_spec("v4l2:534d:2109", "pal")
    assert spec == {"source": "v4l2", "device": "534d:2109", "mode": "auto", "format": "auto", "standard": "pal"}
    orig = json.dumps({"source": "rtsp", "url": "rtsp://h/x", "codec": "mjpeg", "transport": "udp",
                       "rtsp_urls": [{"url": "rtsp://h/x"}]})
    out = json.loads(s.merged_config(orig, spec))
    assert out == {**spec, "rtsp_urls": [{"url": "rtsp://h/x"}]}            # url/codec/transport прежнего ушли
    assert json.loads(s.merged_config("__ABSENT__", {"source": "synthetic"})) == {"source": "synthetic"}
    assert json.loads(s.merged_config("{битый", {"source": "synthetic"})) == {"source": "synthetic"}


def test_v4l2_predicates():
    st = {"video": {"source": {"type": "v4l2", "input": "hdmi", "state": "streaming"}}}
    assert s.remote_v4l2(st) and not s.remote_v4l2(st, "no-signal")
    assert s.remote_v4l2({"video": {"source": {"type": "uvc", "state": "streaming"}}})
    assert not s.remote_v4l2({"video": {"source": {"type": "synthetic", "state": "streaming"}}})
    assert s.console_v4l2({"videoSource": {"live": True, "input": "CVBS"}})
    assert not s.console_v4l2({"videoSource": {"live": True, "input": "synthetic"}})
    assert not s.console_v4l2({"videoSource": {"live": False, "input": "hdmi"}})


def test_manual_fault_judgement_and_blue_field_explained():
    ok = {"off_s": 1.6, "back_s": 3.2, "remote_fault_state": "no-signal", "remote_states_seen": ["no-signal", "streaming"]}
    assert s.judge_manual_fault(ok, ("no-signal",))[:2] == ("PASS", "PASS")
    blue = {"off_s": None, "back_s": 0.5, "remote_fault_state": None, "remote_states_seen": ["streaming"]}
    v12, v13, p = s.judge_manual_fault(blue, ("no-signal",))
    assert v12 == "FAIL" and any("синее" in x or "поле" in x for x in p)
    usb = {"off_s": 1.0, "back_s": 6.0, "remote_fault_state": "no-device", "remote_states_seen": ["no-device"]}
    assert s.judge_manual_fault(usb, s.FAULTS["usb"])[:2] == ("PASS", "PASS")
    assert s.judge_manual_fault(usb, s.FAULTS["cable"])[0] == "FAIL"


def shot(name, inp="hdmi", changed=True, dark=0.1, **kw):
    return {"shot": name, "frame_changed": changed, "frame_dark": dark, "videoSource": {"input": inp}, **kw}


def test_gui_select_judgement():
    sid = "v4l2:534d:2109"
    good = [shot("01-02-stream-video-t+1s.png", "synthetic"), shot("03-04-selected-v4l2_534d_2109-video-t+1s.png"),
            shot("05-06-selected-synthetic-video-t+1s.png", "synthetic")]
    assert s.judge_gui_select(good, sid)[0] == "PASS"
    frozen = [good[0], shot("03-04-selected-v4l2_534d_2109-video-t+1s.png", changed=False), good[2]]
    v, d = s.judge_gui_select(frozen, sid)
    assert v == "FAIL" and "без движения" in d
    wrong = [good[0], shot("03-04-selected-v4l2_534d_2109-video-t+1s.png", inp="synthetic"), good[2]]
    assert s.judge_gui_select(wrong, sid)[0] == "FAIL"
    rejected = [good[0], {"shot": "select-error", "select_error": {"state": "error", "code": "unavailable"}}]
    v, d = s.judge_gui_select(rejected, sid)
    assert v == "FAIL" and "unavailable" in d
    assert s.judge_gui_select([], sid)[0] == "ERROR"
    assert s.judge_gui_select(good[:2], sid)[0] == "FAIL"                          # возврат на synthetic не снят


def test_gui_fault_dark_only_when_stream_bright():
    rec = shot("05-06-recovered-no-signal-video-t+1s.png")
    bright = shot("01-02-stream-video-t+1s.png", dark=0.05)
    dark_stream = shot("01-02-stream-video-t+1s.png", dark=0.95)
    black = shot("03-04-no-signal-video-t+1s.png", dark=0.97)
    lit = shot("03-04-no-signal-video-t+1s.png", dark=0.2)
    assert s.judge_gui_fault([bright, black, rec], "no-signal")[0] == "PASS"
    v, d, n = s.judge_gui_fault([bright, lit, rec], "no-signal")
    assert v == "FAIL" and "не погашено" in d and n["dark_applicable"]
    v, d, n = s.judge_gui_fault([dark_stream, lit, rec], "no-signal")      # тёмный поток: поле не критерий
    assert v == "PASS" and "неприменим" in d and not n["dark_applicable"]
    v, d, _ = s.judge_gui_fault([bright, rec], "no-signal")
    assert v == "FAIL" and "не показал" in d
    assert s.judge_gui_fault([], "no-signal")[0] == "ERROR"
    frozen_rec = shot("05-06-recovered-no-signal-video-t+1s.png", changed=False)
    assert s.judge_gui_fault([bright, black, frozen_rec], "no-signal")[0] == "FAIL"


def test_ota_baseline_real_pi1_b_and_pi2():
    """Сравнение относительно самой платы: pi1 на B COMMITTED — два «статических» FAIL допустимы; pi2 — PASS."""
    for board in ("pi1", "pi2"):
        snap = json.loads((RETRY / f"{board}-final.json").read_text())
        v, bad, _checks = s.judge_ota(board, snap, snap)
        assert (v, bad) == ("PASS", []), board


def test_ota_baseline_detects_change_and_console_restart_rule():
    snap = json.loads((RETRY / "pi1-final.json").read_text())
    after = json.loads(json.dumps(snap))
    f = after["fields"]
    key = next(k for k in f if k.startswith("unit.sheregesh-console"))
    f[key] = f[key] + " X"
    v, bad, _ = s.judge_ota("pi1", snap, after)
    assert v == "FAIL" and any("sheregesh-console" in b for b in bad)
    assert s.judge_ota("pi1", snap, after, console_restarted=True)[0] == "PASS"
    after["fields"]["boot_id"] = "другой"
    assert s.judge_ota("pi1", snap, after, console_restarted=True)[0] == "FAIL"


def test_stand_set_source_file_and_cmd(monkeypatch):
    sent = []
    monkeypatch.setattr(s, "ssh", lambda h, c, timeout=60: sent.append((h, c)) or (0, ""))
    a = argparse.Namespace(console="c", remote="r", source_cmd=None, standard="ntsc", mode="auto", format="auto")
    st = s.Stand.__new__(s.Stand)
    st.a, st.sid, st.orig_conf = a, "v4l2:1b71:3002", json.dumps({"source": "synthetic", "rtsp_urls": ["rtsp://h/a"]})
    assert st.set_source("v4l2")
    host, cmd = sent[-1]
    assert host == "r" and '"device": "1b71:3002"' in cmd and '"standard": "ntsc"' in cmd and "rtsp_urls" in cmd
    assert "kill -HUP $(systemctl show -p MainPID --value sheregesh-remote-net)" in cmd
    calls = []
    monkeypatch.setattr(s.subprocess, "run", lambda cmd, shell, timeout: calls.append(cmd) or
                        argparse.Namespace(returncode=0))
    a.source_cmd = "videoselect {source}"
    assert st.set_source("v4l2") and st.set_source("synthetic")
    assert calls == ["videoselect v4l2:1b71:3002", "videoselect synthetic"]


def test_restore_config_absent_writes_synthetic_then_removes(monkeypatch):
    sent = []
    monkeypatch.setattr(s, "ssh", lambda h, c, timeout=60: sent.append(c) or (0, ""))
    s.restore_config("r", "__ABSENT__")
    assert '{"source": "synthetic"}' in sent[0] and sent[1].startswith("rm -f")
    sent.clear()
    s.restore_config("r", '{"source": "rtsp"}')
    assert len(sent) == 1 and '{"source": "rtsp"}' in sent[0]
    sent.clear()
    s.restore_config("r", None)
    assert not sent


def test_gui_runner_lessons_from_s4():
    src = (ROOT / "tools/probe/s5_accept.py").read_text()
    assert "-f '/tmp/gui_capture.py'" not in src and "-f '[/]tmp/gui_capture.py'" in src
    assert "$C/software/emulators" in src


def test_plan_without_ssh(capsys):
    assert s.main(["--plan", "--source", "v4l2:534d:2109", "--gui"]) == 0
    out = capsys.readouterr().out
    assert "S5-11" in out and "BLOCKED" in out and "ЧЕЛОВЕК" in out and "S5-15" in out
