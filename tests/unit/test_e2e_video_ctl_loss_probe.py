# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Проба стенда ctl-loss (tools/probe/check_video_link.py): критерий REQ-SW-002 п. 3 — время показа «нет видео»
(состояние приёмника не «playing»), а не время выключения видео по тайм-ауту канала управления. Без плат: ssh и
ui-state.json подменены, часы — модельные."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "probe"))
import check_video_link as cvl  # noqa: E402


@pytest.fixture
def stand(monkeypatch):
    clock = {"t": 0.0, "kill": None, "cont": None}
    monkeypatch.setattr(cvl.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(cvl.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))

    def ssh(host, cmd, timeout=120):
        clock["t"] += 0.4                                              # установление ssh
        if cmd.startswith("systemctl show"):
            return "4242"
        if cmd.startswith("kill -STOP"):
            clock["kill"] = clock["t"]
        if cmd.startswith("kill -CONT"):
            clock["cont"] = clock["t"]
            return "resumed"
        return ""
    monkeypatch.setattr(cvl, "ssh", ssh)
    monkeypatch.setattr(cvl, "journal", lambda *a: [])
    return clock


def ui(clock, no_video_s, off_s, back_s=2.0):
    """ui-state.json пульта; сеанс SRTP службы rx — по тем же часам (новый сеанс — после возобновления блока)."""
    def back():
        return clock["cont"] is not None and clock["t"] - clock["cont"] >= back_s

    def video(ch):
        clock["t"] += 0.15                                             # ssh cat ui-state.json
        k = clock["kill"]
        if clock["cont"] is not None:
            return {"mode": "remote-srtp", "state": "playing"} if back() else {"mode": "off", "state": "off"}
        if k is None or clock["t"] - k < no_video_s:
            return {"mode": "remote-srtp", "state": "playing"}
        if clock["t"] - k < off_s:
            return {"mode": "remote-srtp", "state": "no-video"}
        return {"mode": "off", "state": "off"}

    def service(host):
        return {"session": "s2" if back() else "s1"}
    return video, service


def use(monkeypatch, video_service):
    monkeypatch.setattr(cvl, "cv", video_service[0])
    monkeypatch.setattr(cvl, "vs", video_service[1])


def test_no_video_measured_separately_from_control_timeout(stand, monkeypatch):
    use(monkeypatch, ui(stand, no_video_s=2.5, off_s=3.3))   # как на стенде: off через 3,3 с
    r = cvl.sc_ctl_loss("c", "r")
    assert r["verdict"] == "PASS", r
    assert 2.5 <= r["no_video_after_s"] <= 2.8 and r["video_off_after_s"] >= 3.3
    assert r["video_back_after_resume_s"] <= 10


def test_no_video_later_than_3_s_fails(stand, monkeypatch):
    use(monkeypatch, ui(stand, no_video_s=3.31, off_s=3.31))  # «нет видео» только вместе с off
    r = cvl.sc_ctl_loss("c", "r")
    assert r["verdict"] == "FAIL" and r["no_video_after_s"] > 3.0, r


def test_slow_recovery_fails(stand, monkeypatch):
    use(monkeypatch, ui(stand, no_video_s=1.6, off_s=3.1, back_s=12.0))
    r = cvl.sc_ctl_loss("c", "r")
    assert r["verdict"] == "FAIL" and r["video_back_after_resume_s"] > 10, r
