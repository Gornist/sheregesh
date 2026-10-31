# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Пробы стенда stream-loss и ctl-loss (tools/probe/check_video_link.py). stream-loss — критерий REQ-SW-002 п. 3:
обрыв потока (пауза службы tx) → «нет видео» ≤ 3 с, возврат ≤ 10 с. ctl-loss — потеря управления: выключение видео
не позже тайм-аута heartbeat + 1 с. Время — по событию: часы блока в момент сигнала и метка смены состояния видео
пульта (video.state_at), не опрос. Без плат: ssh и ui-state.json подменены, часы плат — модельные и общие (NTP)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "probe"))
import check_video_link as cvl  # noqa: E402


@pytest.fixture
def stand(monkeypatch):
    clock = {"t": 1000.0, "stop": None, "cont": None, "delivered": True}
    monkeypatch.setattr(cvl.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(cvl.time, "time", lambda: clock["t"])
    monkeypatch.setattr(cvl.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))

    def ssh(host, cmd, timeout=120):
        clock["t"] += 0.4                                              # установление ssh — до сигнала
        if cmd.startswith("systemctl show -p MainPID"):
            return "4242"
        if "kill -STOP" in cmd:
            clock["stop"] = clock["t"]
            return f"{clock['t']:.6f}\nok" if clock["delivered"] else f"{clock['t']:.6f}"
        if "kill -CONT" in cmd:
            clock["cont"] = clock["t"]
            return f"{clock['t']:.6f}\nok"
        return ""
    monkeypatch.setattr(cvl, "ssh", ssh)
    monkeypatch.setattr(cvl, "journal", lambda *a: [])
    monkeypatch.setattr(cvl, "jread", lambda host, path: {"phase": clock.get("phase", "connected")})
    return clock


def ui(clock, no_video_s, off_s=None, back_s=2.0, event=True):
    """Видео пульта: состояние и метка его смены на модельных часах. off_s=None — управление живо (stream-loss)."""
    def video(ch):
        clock["t"] += 0.15                                             # ssh cat ui-state.json, раз в 1 с дамп
        s, c, t = clock["stop"], clock["cont"], clock["t"]
        if c is not None:
            if t - c >= back_s:
                v = {"mode": "remote-srtp", "state": "playing", "state_at": c + back_s}
            else:
                v = {"mode": "off", "state": "off", "state_at": c} if off_s is not None else \
                    {"mode": "remote-srtp", "state": "no-video", "state_at": s + no_video_s}
        elif s is None or t - s < no_video_s:
            v = {"mode": "remote-srtp", "state": "playing", "state_at": 900.0}
        elif off_s is None or t - s < off_s:
            v = {"mode": "remote-srtp", "state": "no-video", "state_at": s + no_video_s}
        else:
            v = {"mode": "off", "state": "off", "state_at": s + off_s}
        if not event:
            v.pop("state_at")
        return v

    def service(host):
        return {"session": "s2" if clock["cont"] is not None and clock["t"] - clock["cont"] >= back_s else "s1"}
    return video, service


def use(monkeypatch, video_service):
    monkeypatch.setattr(cvl, "cv", video_service[0])
    monkeypatch.setattr(cvl, "vs", video_service[1])


def test_stream_loss_measured_by_event_not_poll(stand, monkeypatch):
    use(monkeypatch, ui(stand, no_video_s=1.62))
    r = cvl.sc_stream_loss("c", "r")
    assert r["verdict"] == "PASS", r
    assert r["no_video_by"] == "event" and r["no_video_after_s"] == pytest.approx(1.62, abs=1e-3)
    assert r["back_by"] == "event" and r["video_back_after_resume_s"] == pytest.approx(2.0, abs=1e-3)


def test_stream_loss_late_no_video_fails(stand, monkeypatch):
    use(monkeypatch, ui(stand, no_video_s=3.05))
    r = cvl.sc_stream_loss("c", "r")
    assert r["verdict"] == "FAIL" and r["no_video_after_s"] > 3.0, r


def test_stream_loss_slow_recovery_fails(stand, monkeypatch):
    use(monkeypatch, ui(stand, no_video_s=1.6, back_s=11.0))
    r = cvl.sc_stream_loss("c", "r")
    assert r["verdict"] == "FAIL" and r["video_back_after_resume_s"] > 10, r


def test_stream_loss_requires_live_control(stand, monkeypatch):
    stand["phase"] = "lost"                                            # пропало управление — это не обрыв потока
    use(monkeypatch, ui(stand, no_video_s=1.6))
    assert cvl.sc_stream_loss("c", "r")["verdict"] == "FAIL"


def test_stream_loss_undelivered_signal_is_error(stand, monkeypatch):
    stand["delivered"] = False
    use(monkeypatch, ui(stand, no_video_s=1.6))
    r = cvl.sc_stream_loss("c", "r")
    assert r["verdict"] == "ERROR" and stand["cont"] is None, r


def test_poll_fallback_without_state_at(stand, monkeypatch):
    use(monkeypatch, ui(stand, no_video_s=1.6, event=False))
    r = cvl.sc_stream_loss("c", "r")
    assert r["no_video_by"] == "poll" and r["no_video_after_s"] >= 1.6, r


def test_ctl_loss_judged_by_heartbeat_timeout(stand, monkeypatch):
    use(monkeypatch, ui(stand, no_video_s=3.2, off_s=3.2))             # как на стенде: «нет видео» = выключение
    r = cvl.sc_ctl_loss("c", "r")
    assert r["verdict"] == "PASS", r
    assert r["video_off_after_s"] == pytest.approx(3.2, abs=1e-3) and r["video_off_by"] == "event"
    assert r["video_back_after_resume_s"] <= 10


def test_ctl_loss_off_later_than_timeout_plus_1_fails(stand, monkeypatch):
    use(monkeypatch, ui(stand, no_video_s=4.3, off_s=4.3))
    r = cvl.sc_ctl_loss("c", "r")
    assert r["verdict"] == "FAIL" and r["video_off_after_s"] > cvl.HB_TIMEOUT_S + 1.0, r


def test_ctl_loss_slow_recovery_fails(stand, monkeypatch):
    use(monkeypatch, ui(stand, no_video_s=3.1, off_s=3.1, back_s=12.0))
    r = cvl.sc_ctl_loss("c", "r")
    assert r["verdict"] == "FAIL" and r["video_back_after_resume_s"] > 10, r
