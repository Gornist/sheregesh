# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Видеосеанс SRTP пульта (videorx.VideoRx, ADR-0016, контракт v5 §6): подменные служба rx, systemctl и часы."""
import json
import time

import pytest

from sheregesh.videosvc import session as vs
from sheregesh.videosvc.ctl import CtlError
from sheregesh_console import videorx as V

CANARY = "SHRG-CANARY-VIDEOKEY"
CIPHER = "aes-128-gcm"


class FakeCtl:
    def __init__(self, svc):
        self.svc = svc

    def request(self, cmd, args=None):
        self.svc.calls.append((cmd, args))
        if self.svc.fail.get(cmd):
            raise self.svc.fail[cmd]
        return {}

    def close(self):
        pass


class Svc:
    def __init__(self):
        self.calls, self.fail, self.units, self.sent, self.logs, self.allow_ok = [], {}, [], [], [], True
        self.t = 100.0

    def systemctl(self, unit):
        self.units.append(unit)
        return self.allow_ok or unit.endswith("@none.service")

    def cmds(self, name):
        return [a for c, a in self.calls if c == name]


@pytest.fixture
def env(tmp_path):
    svc = Svc()
    rx = V.VideoRx(lambda t, b: svc.sent.append((t, b)) or True, lambda: "192.168.1.169", runtime=str(tmp_path),
                   ctl_factory=lambda path: FakeCtl(svc), systemctl=svc.systemctl, clock=lambda: svc.t,
                   log=lambda lv, tx: svc.logs.append(tx), rx_port=7444, clock_offset=lambda: 0.0123)
    return rx, svc, tmp_path


def key_body(gen=1, session="a" * 32, key=None):
    return vs.video_key_body("main", session, gen, 1234 + gen, CIPHER, key if key is not None else bytes(range(28)))


def write_state(path, state="streaming", **extra):
    st = {"state": state, "fps": 29.8, "rtp": {"packets": 100, "lost": 3, "jitter_ms": 1.2},
          "latency_ms": {"p50": 20.0, "p95": 30.0, "max": 40.0, "samples": 10}, **extra}
    (path / "state.json").write_text(json.dumps({"v": 1, "role": "rx", "time": time.time(), "streams": {"main": st}}))


def test_hello_by_service_availability_and_config(env):
    rx, svc, _ = env
    assert rx.hello_fields() == {"video_transports": ["srtp", "tls"], "video_rx_port": 7444}
    svc.fail["status"] = OSError("нет сокета")
    assert rx.hello_fields() == {"video_transports": ["tls"]}
    del svc.fail["status"]
    rx.transport_pref = "tls"
    assert rx.hello_fields() == {"video_transports": ["tls"]}


def test_link_json_transport_and_port(tmp_path):
    p = tmp_path / "link.json"
    p.write_text(json.dumps({"video": {"transport": "tls"}, "video_port": 7555}))
    assert V.link_video_prefs(p) == ("tls", 7555)
    p.write_text(json.dumps({"video": True}))
    assert V.link_video_prefs(p) == ("auto", 7444)
    assert V.link_video_prefs(tmp_path / "нет.json") == ("auto", 7444)


def test_video_key_allow_start_ack_in_order(env):
    rx, svc, _ = env
    rx.handle("video-key", key_body())
    assert svc.units == ["sheregesh-video-allow@192.168.1.169.service"]
    start = svc.cmds("start")[0]
    assert start["peer"] == {"ip": "192.168.1.169"} and start["local"] == {"port": 7444, "rtcp_port": 7445}
    assert start["session"] == "a" * 32 and start["gen"] == 1 and start["srtp"]["cipher"] == CIPHER
    assert start["srtp"]["key_b64"] == key_body()["srtp"]["key_b64"] and start["codec"] == "h264"
    assert svc.sent == [("video-key-ack", {"stream": "main", "session": "a" * 32, "gen": 1, "ok": True})]
    assert rx.active and rx.phase == "starting"


def test_repeat_same_key_is_idempotent(env):
    rx, svc, _ = env
    rx.handle("video-key", key_body())
    rx.handle("video-key", key_body())
    assert len(svc.cmds("start")) == 1 and len(svc.units) == 1
    assert [b["ok"] for t, b in svc.sent] == [True, True]


def test_stale_and_bad_key_nack_without_start(env):
    rx, svc, _ = env
    rx.handle("video-key", key_body(gen=2))
    svc.sent.clear()
    rx.handle("video-key", key_body(gen=1, key=bytes(28)))          # gen меньше принятого
    rx.handle("video-key", key_body(gen=3, session="b" * 32))        # чужой session
    bad = key_body(gen=4)
    bad["srtp"]["key_b64"] = "!!!"
    rx.handle("video-key", bad)
    assert [(b["ok"], b.get("error")) for t, b in svc.sent] == [(False, "stale"), (False, "stale"),
                                                               (False, "bad-request")]
    assert len(svc.cmds("start")) == 1


def test_allow_failure_nacks_unsupported_and_falls_back(env):
    rx, svc, _ = env
    svc.allow_ok = False
    rx.handle("video-key", key_body())
    assert not svc.cmds("start")
    assert [t for t, _ in svc.sent] == ["video-key-ack", "video-fallback"]
    assert svc.sent[0][1]["error"] == "unsupported"
    assert rx.phase == "fallback" and not rx.active


def test_start_error_falls_back(env):
    rx, svc, _ = env
    svc.fail["start"] = CtlError("pipeline", "нет конвейера")
    rx.handle("video-key", key_body())
    assert [t for t, _ in svc.sent] == ["video-fallback"]
    assert svc.sent[0][1] == {"stream": "main", "session": "a" * 32, "gen": 1, "reason": "start-failed"}
    assert svc.cmds("stop") and svc.units[-1] == "sheregesh-video-allow@none.service"
    assert rx.phase == "fallback"


def test_first_frame_timeout_falls_back(env):
    rx, svc, tmp = env
    rx.handle("video-key", key_body())
    write_state(tmp, state="starting")
    svc.t += 4.9
    rx.tick()
    assert rx.active
    svc.t += 0.2
    rx.tick()
    assert rx.phase == "fallback" and svc.sent[-1][0] == "video-fallback"
    assert svc.sent[-1][1]["reason"] == "no-first-frame"
    assert svc.units[-1] == "sheregesh-video-allow@none.service" and svc.cmds("stop")
    svc.sent.clear()
    rx.handle("video-key", key_body(gen=2))                           # обратно на SRTP в этом сеансе нельзя
    assert svc.sent[0][1]["ok"] is False and len(svc.cmds("start")) == 1


def test_streaming_state_prevents_fallback_and_clock_offset_every_5s(env):
    rx, svc, tmp = env
    rx.handle("video-key", key_body())
    write_state(tmp)
    rx.tick()
    assert rx.phase == "streaming"
    assert svc.cmds("set-clock-offset") == [{"offset_s": 0.0123}]
    svc.t += 4
    rx.tick()
    assert len(svc.cmds("set-clock-offset")) == 1
    svc.t += 1.1
    rx.tick()
    assert len(svc.cmds("set-clock-offset")) == 2
    svc.t += 60
    rx.tick()
    assert rx.phase == "streaming"


def test_no_clock_offset_no_command(env):
    rx, svc, tmp = env
    rx._clock_offset = lambda: None
    rx.handle("video-key", key_body())
    write_state(tmp)
    rx.tick()
    assert not svc.cmds("set-clock-offset")


def test_video_stop_and_new_session(env):
    rx, svc, _ = env
    rx.handle("video-key", key_body())
    rx.handle("video-stop", vs.stop_body("main", "c" * 32, 1, "x"))    # чужой session: игнор
    assert rx.active
    rx.handle("video-stop", vs.stop_body("main", "a" * 32, 1, "source-change"))
    assert not rx.active and svc.cmds("stop") and svc.units[-1] == "sheregesh-video-allow@none.service"
    rx.handle("video-key", key_body(gen=1, session="d" * 32))          # новый сеанс управления требует reset
    assert svc.sent[-1][1]["error"] == "stale"
    rx.handle("session-end", {})
    rx.handle("session-start", {})
    rx.handle("video-key", key_body(gen=1, session="d" * 32))
    assert svc.sent[-1][1]["ok"] is True and rx.active


def test_session_end_stops_service_and_clears_fallback(env):
    rx, svc, _ = env
    rx.handle("video-key", key_body())
    rx.handle("session-end", {})
    assert not rx.active and svc.cmds("stop") and svc.units[-1].endswith("@none.service")
    svc.fail["start"] = CtlError("pipeline")
    rx.handle("video-key", key_body(gen=1, session="e" * 32))
    assert rx.phase == "fallback"
    rx.handle("session-end", {})
    assert rx.phase == "idle"


def test_stats_from_state_json_and_no_key_leak(env):
    rx, svc, tmp = env
    body = key_body(key=CANARY.encode().ljust(28, b"."))
    canary_b64 = body["srtp"]["key_b64"]
    rx.handle("video-key", body)
    rx.handle("video-key", {**body, "ssrc": 0})                         # плохое тело
    write_state(tmp)
    rx.tick()
    st = rx.stats()
    assert st["received_fps_5s"] == 29.8 and st["seq_gaps"] == 3 and st["lost_packets"] == 3
    assert st["transport_latency_ms"]["p50"] == 20.0 and st["decode_latency_ms"]["p50"] is None
    assert st["transport"] == "srtp" and st["connected"] is True
    blob = json.dumps(st) + "\n".join(svc.logs) + json.dumps([b for t, b in svc.sent if t != "video-key-ack"])
    assert canary_b64 not in blob and CANARY not in blob
    assert any("video-key" in line and "***" in line for line in svc.logs)


def test_stale_state_json_is_ignored(env):
    rx, svc, tmp = env
    write_state(tmp)
    rx._wall = lambda: time.time() + 60
    assert rx.read_state() == {}
