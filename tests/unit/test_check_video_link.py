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
