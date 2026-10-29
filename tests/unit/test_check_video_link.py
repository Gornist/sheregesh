# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Инструмент испытаний видеоканала (tools/probe/check_video_link.py): без плат."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "probe"))
import check_video_link as cv  # noqa: E402


def test_busy_counts_idle_and_iowait():
    assert cv._busy([100, 0, 0, 900, 0], [150, 0, 0, 1850, 0]) == 5.0       # 50 занятых из 1000
    assert cv._busy([0] * 5, [0] * 5) is None


def test_errors_are_not_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(cv, "ssh", lambda h, c, timeout=120: "x")

    def boom(c, r):
        raise TimeoutError("ssh")
    monkeypatch.setattr(cv, "SCENARIOS", {"a": boom, "b": lambda c, r: {"verdict": "ERROR", "abort": True},
                                          "c": lambda c, r: {"verdict": "PASS"}})
    assert cv.main(["--console", "c", "--remote", "r", "--out", str(tmp_path), "--only", "a,b,c"]) == 1
    s = json.loads((tmp_path / "summary.json").read_text())
    assert s["results"] == {"a": "ERROR", "b": "ERROR", "c": "SKIP"}         # после abort дальше не идём


def sk(**kw):
    m = {"killed": ["12"], "restart_s": 1.1, "left": [], "remote_pipelines": 1, "remote_orphans": [],
         "video_back_s": 2.0, "control_phase": "connected", "control_dropped": False, "no_video_seen": False,
         "new_session": False}
    return {**m, **kw}


def test_sender_kill_same_session_without_no_video_is_pass():
    """Ingest (3e147dd): генератор перезапущен в том же видеосеансе, «нет видео» не наступило — PASS."""
    assert cv.judge_sender_kill(sk()) == ("PASS", [])
    assert cv.judge_sender_kill(sk(no_video_seen=True, new_session=True))[0] == "PASS"


def test_sender_kill_failures_and_errors():
    assert cv.judge_sender_kill(sk(restart_s=None))[0] == "FAIL"
    assert cv.judge_sender_kill(sk(video_back_s=None))[0] == "FAIL"
    assert cv.judge_sender_kill(sk(video_back_s=10.4))[0] == "FAIL"
    assert cv.judge_sender_kill(sk(left=["12"]))[0] == "FAIL"
    assert cv.judge_sender_kill(sk(remote_pipelines=2))[0] == "FAIL"
    assert cv.judge_sender_kill(sk(remote_orphans=["99 1 gst-launch"]))[0] == "FAIL"
    assert cv.judge_sender_kill(sk(control_dropped=True))[0] == "FAIL"
    assert cv.judge_sender_kill(sk(killed=[]))[0] == "ERROR"


class Board:
    """Подмена SSH: pi2 — генератор 12, после SIGKILL через 2 опроса появляется 57; pi1 — кадры растут после
    перезапуска, сеанс прежний."""

    def __init__(self):
        self.killed, self.polls, self.frames = False, 0, 1000

    def __call__(self, host, cmd, timeout=120):
        if host == "r":
            if cmd.startswith("pgrep"):
                if not self.killed:
                    return "12"
                self.polls += 1
                return "57" if self.polls > 2 else ""
            if cmd.startswith("kill -KILL"):
                self.killed = True
                return ""
            if cmd.startswith("ps -eo"):
                return "57 4000 gst-launch-1.0 ... rtpstreampay ! fdsink"
            if "link-status" in cmd:
                return json.dumps({"video": {"sender_restarts": 1 if self.polls > 2 else 0}})
            return ""
        if "ui-state" in cmd:
            if self.killed and self.polls > 2:
                self.frames += 30
            return json.dumps({"video": {"mode": "remote-tls", "state": "playing", "sessions": 1,
                                         "frames_decoded": self.frames}})
        if "link-status" in cmd:
            return json.dumps({"phase": "connected"})
        return ""


def test_sc_sender_kill_recovers_in_same_session(monkeypatch):
    board = Board()
    monkeypatch.setattr(cv, "ssh", board)
    monkeypatch.setattr(cv.time, "sleep", lambda s: None)
    r = cv.sc_sender_kill("c", "r", timeout=5)
    assert r["verdict"] == "PASS", r["problems"]
    assert r["new_pids"] == ["57"] and r["new_session"] is False and r["sessions"] == [1, 1]


def _win(link0, link1, dec0=0, dec1=600, frames0=0, frames1=600):
    c0 = {"link": {"connected": True, **link0}, "sessions": 1, "frames_decoded": dec0, "restarts": 0,
          "codec": "h264", "output": "640x360"}
    c1 = {"link": {"connected": True, "stream_sps": {"width": 1280, "height": 720}, **link1}, "sessions": 1,
          "frames_decoded": dec1, "restarts": 0, "codec": "h264", "output": "640x360"}
    return c0, c1, {"frames": frames0}, {"frames": frames1}


def test_stream_window_measured_zero_passes_unmeasured_fails():
    """Контракт видео v9: «ноль потерь» — только измеренный ноль; неизмеренный счётчик (None) не превращается в 0."""
    base = {"frames": 0, "packets": 0, "bytes": 0, "seq_gaps": 0, "lost_packets": 0, "bad_rtp": 0}
    end = {"frames": 600, "packets": 900, "bytes": 1_500_000, "seq_gaps": 0, "lost_packets": 0, "bad_rtp": 0}
    r = cv.judge_stream(*_win(base, end), 20.0)
    assert r["verdict"] == "PASS" and r["metrics"]["seq_gaps"] == 0 and "unmeasured" not in r
    r = cv.judge_stream(*_win({**base, "seq_gaps": None}, {**end, "seq_gaps": None}), 20.0)
    assert r["verdict"] == "FAIL" and r["metrics"]["seq_gaps"] is None and r["unmeasured"] == ["seq_gaps"]
    assert cv.delta({"x": None}, {"x": 5}, "x") is None and cv.delta({}, {"x": 5}, "x") is None
    assert cv.delta({"x": 0}, {"x": 0}, "x") == 0


def test_latency_judged_by_metric_with_same_bounds():
    srtp = {"transport": "srtp", "capture_to_decoded_ms": {"p50": 80.0, "p95": 95.0, "max": 120.0, "samples": 600},
            "transport_latency_ms": {"p50": None, "p95": None, "max": None, "samples": 0},
            "decode_latency_ms": {"p50": None, "p95": None, "samples": 0, "desync_resets": 0}}
    r = cv.judge_latency(srtp)
    assert r["verdict"] == "PASS" and r["capture_to_decoded_ms"]["p50"] == 80.0
    assert "measured_part_p50_ms" not in r and "transport_pi2_to_pi1_ms" not in r     # не складывается с TLS
    assert cv.judge_latency({**srtp, "capture_to_decoded_ms": {"p50": None, "samples": 0}})["verdict"] == "FAIL"
    tls = {"transport": "tls", "transport_latency_ms": {"p50": 4.0, "samples": 600},
           "decode_latency_ms": {"p50": 30.0, "samples": 600, "desync_resets": 0}}
    r = cv.judge_latency(tls)
    assert r["verdict"] == "PASS" and r["measured_part_p50_ms"] == 34.0 and "capture_to_decoded_ms" not in r
