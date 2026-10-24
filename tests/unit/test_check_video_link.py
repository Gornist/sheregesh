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


def test_trace_parsing(monkeypatch):
    out = "\n".join([
        "0:00:01 latency, src-element-id=(string)0x1, src-element=(string)videotestsrc0, src=(string)src, "
        "sink-element-id=(string)0x2, sink-element=(string)fakesink0, sink=(string)sink, time=(guint64)40000000, ts=1;",
        "0:00:01 latency, src-element=(string)videotestsrc0, time=(guint64)60000000, ts=2;",
        "0:00:01 element-latency, element-id=(string)0x3, element=(string)v4l2h264dec0, src=(string)src, "
        "time=(guint64)12000000, ts=3;",
        "0:00:01 element-latency, element=(string)rtpjitterbuffer0, time=(guint64)50000000, ts=3;"])
    monkeypatch.setattr(cv, "ssh", lambda h, c, timeout=120: out)
    r = cv._trace("h", "x")
    assert r["samples"] == 2 and r["pipeline_ms_max"] == 60.0
    assert r["elements_ms_p50"] == {"rtpjitterbuffer": 50.0, "v4l2h264dec": 12.0}


def test_errors_are_not_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(cv, "ssh", lambda h, c, timeout=120: "x")

    def boom(c, r):
        raise TimeoutError("ssh")
    monkeypatch.setattr(cv, "SCENARIOS", {"a": boom, "b": lambda c, r: {"verdict": "ERROR", "abort": True},
                                          "c": lambda c, r: {"verdict": "PASS"}})
    assert cv.main(["--console", "c", "--remote", "r", "--out", str(tmp_path), "--only", "a,b,c"]) == 1
    s = json.loads((tmp_path / "summary.json").read_text())
    assert s["results"] == {"a": "ERROR", "b": "ERROR", "c": "SKIP"}         # после abort дальше не идём
