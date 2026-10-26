# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Юнит-тесты службы приёмника видео (videosvc/rx.py, ADR-0016, контракт §2, §3, §5, §8).

Конвейер подменён фабрикой, проверка nftables — функцией, часы управляемые. Сокет не запускается: команды идут
через RxService.handle, цикл — через tick.
"""
import base64
import json
import logging
import secrets

import pytest

from sheregesh.videosvc import rx as R
from sheregesh.videosvc.ctl import CtlError
from sheregesh.videosvc.state import validate_state

SESSION = "c" * 32
CANARY = b"SHRG-CANARY-VIDEOKEY-"


class Clock:
    def __init__(self, now=1000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt


class FakePipe:
    def __init__(self, cfg, on_event, clock):
        self.cfg, self.on_event, self.clock = cfg, on_event, clock
        self.frames, self.last_frame = 0, 0.0
        self.started, self.stops, self.keyframes, self.offset = False, 0, 0, None
        self.fail_start = False

    def start(self):
        if self.fail_start:
            raise RuntimeError("не собран")
        self.started = True

    def stop(self):
        self.stops += 1
        self.started = False

    def request_keyframe(self):
        self.keyframes += 1
        return True

    def set_clock_offset(self, v):
        self.offset = v

    def frame(self, n=1):
        self.frames += n
        self.last_frame = self.clock()

    def emit(self, kind, **d):
        self.on_event(kind, d)

    def stats(self):
        return {"frames": self.frames, "fps": 30.0, "packets": 10, "lost": 1, "jitter_ms": 0.5, "pli_sent": 2,
                "unknown_ssrc": 3, "bitrate_kbps": 900.0, "last_frame_age_s": 0.1,
                "latency_ms": {"p50": 5.0, "p95": 9.0, "max": 12.0, "samples": 40}}


class Factory:
    def __init__(self, clock):
        self.clock, self.pipes, self.fail_next = clock, [], False

    def __call__(self, cfg, on_event):
        p = FakePipe(cfg, on_event, self.clock)
        p.fail_start = self.fail_next
        self.pipes.append(p)
        return p


def args(gen=1, key=None, **over):
    key = key if key is not None else secrets.token_bytes(28)
    a = {"stream": "main", "session": SESSION, "gen": gen, "ssrc": 777, "peer": {"ip": "127.0.0.1"},
         "local": {"port": 7444, "rtcp_port": 7445},
         "srtp": {"cipher": "aes-128-gcm", "key_b64": base64.b64encode(key).decode()}, "codec": "h264"}
    a.update(over)
    return a


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    f = Factory(clock)
    fw = {"ok": True}
    svc = R.RxService(str(tmp_path), factory=f, nft_check=lambda: fw["ok"], decoder="avdec_h264",
                      sink="fakesink", mono=clock, clock=clock, version="t")
    return svc, f, clock, fw


def st(svc):
    return svc.state.snapshot()["streams"]["main"]


def code(fn, *a):
    with pytest.raises(CtlError) as e:
        fn(*a)
    return e.value.code


def run_to_streaming(svc, f, clock):
    svc.handle("start", args())
    f.pipes[-1].frame()
    svc.tick()
    assert st(svc)["state"] == "streaming"


def test_start_и_первый_кадр(env):
    svc, f, clock, _ = env
    r = svc.handle("start", args())
    assert r["state"] == "starting" and "key" not in json.dumps(r)
    cfg = f.pipes[0].cfg
    assert (cfg.local_port, cfg.rtcp_local_port, cfg.rtcp_peer_port, cfg.ssrc) == (7444, 7445, 7445, 777)
    f.pipes[0].frame()
    svc.tick()
    assert st(svc)["state"] == "streaming"
    assert validate_state(svc.state.snapshot()) == []


def test_ошибки_start(env):
    svc, f, clock, _ = env
    assert code(svc.handle, "start", {}) == "bad-request"
    assert code(svc.handle, "start", args(codec="vp8")) == "unsupported"
    assert code(svc.handle, "start", args(ssrc=0)) == "bad-request"
    bad = args()
    bad["srtp"]["cipher"] = "null"
    assert code(svc.handle, "start", bad) == "unsupported"
    assert code(svc.handle, "start", args(key=b"x" * 5)) == "bad-request"
    extra = args()
    extra["peer"]["x"] = 1
    assert code(svc.handle, "start", extra) == "bad-request"
    assert code(svc.handle, "nope", {}) == "unknown-cmd"
    assert code(svc.handle, "stop", {"stream": "zzz"}) == "unknown-stream"
    assert f.pipes == []


def test_идемпотентность_и_stale(env):
    svc, f, clock, _ = env
    a = args(gen=2)
    svc.handle("start", a)
    svc.handle("start", a)
    assert len(f.pipes) == 1 and f.pipes[0].stops == 0
    assert code(svc.handle, "start", args(gen=1)) == "stale"
    assert code(svc.handle, "start", args(gen=2)) == "bad-request"   # другой ключ, тот же gen
    svc.handle("start", args(gen=3))
    assert len(f.pipes) == 2 and f.pipes[0].stops == 1


def test_stop_уничтожает_ключ(env):
    svc, f, clock, _ = env
    a = args()
    svc.handle("start", a)
    assert svc.handle("stop", {"stream": "main", "reason": "x"}) == {"stream": "main", "state": "idle"}
    assert svc._act == {} and f.pipes[0].stops == 1 and st(svc)["state"] == "idle"
    svc.handle("stop", {"stream": "main"})
    assert code(svc.handle, "start", a) == "stale"


def test_degraded_recovering_streaming(env):
    svc, f, clock, _ = env
    run_to_streaming(svc, f, clock)
    clock.advance(1.5)
    svc.tick()
    assert st(svc)["state"] == "recovering"
    assert [t[1] for t in st(svc)["transitions"]][-2:] == ["degraded", "recovering"]
    assert f.pipes[0].keyframes == 1
    clock.advance(1.1)
    svc.tick()
    assert f.pipes[0].keyframes == 2
    f.pipes[0].frame()
    svc.tick()
    assert st(svc)["state"] == "streaming"


def test_нет_ключевого_кадра_после_5_PLI_снова_degraded(env):
    svc, f, clock, _ = env
    run_to_streaming(svc, f, clock)
    clock.advance(1.5)
    svc.tick()
    for _ in range(5):
        clock.advance(1.1)
        svc.tick()
    assert f.pipes[0].keyframes == 5
    assert st(svc)["state"] == "degraded"
    svc.tick()
    assert st(svc)["state"] == "recovering"


def test_откат_перезапусков_и_stuck(env):
    svc, f, clock, _ = env
    svc.handle("start", args())
    a_key = f.pipes[0].cfg.srtp.key
    for i, delay in enumerate(R.BACKOFF_S):
        f.pipes[-1].emit("error", message="boom")
        svc.tick()
        assert st(svc)["state"] == "error" and st(svc)["error"] == "pipeline"
        clock.advance(delay - 0.1)
        svc.tick()
        assert st(svc)["state"] == "error"           # ещё рано
        clock.advance(0.2)
        svc.tick()
        assert st(svc)["state"] == "starting"
        assert f.pipes[-1].cfg.srtp.key == a_key     # тот же ключ и то же поколение
        assert st(svc)["gen"] == 1
    assert st(svc)["restarts"] == 6
    f.pipes[-1].emit("error", message="boom")
    svc.tick()
    assert st(svc)["state"] == "stuck"
    clock.advance(100)
    svc.tick()
    assert st(svc)["state"] == "stuck" and len(f.pipes) == 7
    svc.handle("stop", {"stream": "main"})
    svc.handle("start", args(gen=2))
    assert st(svc)["state"] == "starting"


def test_streaming_сбрасывает_счётчик_сбоев(env):
    svc, f, clock, _ = env
    svc.handle("start", args())
    f.pipes[-1].emit("error", message="x")
    svc.tick()
    clock.advance(1.1)
    svc.tick()
    f.pipes[-1].frame()
    svc.tick()
    assert st(svc)["state"] == "streaming"
    f.pipes[-1].emit("error", message="x")
    svc.tick()
    clock.advance(1.1)                                # снова первый шаг отката (1 с), а не второй
    svc.tick()
    assert st(svc)["state"] == "starting"


def test_таймаут_первого_кадра(env):
    svc, f, clock, _ = env
    svc.handle("start", args())
    clock.advance(5.2)
    svc.tick()
    assert st(svc)["state"] == "error"


def test_firewall_при_start(env):
    svc, f, clock, fw = env
    fw["ok"] = False
    assert code(svc.handle, "start", args()) == "pipeline"
    assert st(svc)["state"] == "error" and st(svc)["error"] == "firewall"
    assert f.pipes == [] and validate_state(svc.state.snapshot()) == []


def test_firewall_в_работе_и_возврат(env):
    svc, f, clock, fw = env
    run_to_streaming(svc, f, clock)
    fw["ok"] = False
    clock.advance(1.1)
    svc.tick()
    assert st(svc)["state"] == "error" and st(svc)["error"] == "firewall" and f.pipes[0].stops == 1
    clock.advance(30)
    svc.tick()
    assert len(f.pipes) == 1                           # пока таблицы нет — не перезапускается
    fw["ok"] = True
    clock.advance(1.1)
    svc.tick()
    assert st(svc)["state"] == "starting" and len(f.pipes) == 2


def test_clock_offset_и_статистика(env):
    svc, f, clock, _ = env
    assert code(svc.handle, "set-clock-offset", {"offset_s": float("nan")}) == "bad-request"
    assert code(svc.handle, "set-clock-offset", {"offset_s": True}) == "bad-request"
    svc.handle("set-clock-offset", {"offset_s": 0.25})
    svc.handle("start", args())
    assert f.pipes[0].offset == 0.25
    svc.handle("set-clock-offset", {"offset_s": -1.5})
    assert f.pipes[0].offset == -1.5
    f.pipes[0].frame(3)
    clock.advance(1.1)
    svc.tick()
    s = st(svc)
    assert s["frames"] == 3 and s["latency_ms"]["p95"] == 9.0 and s["rtcp"]["pli_sent"] == 2
    assert s["srtp"]["unknown_ssrc"] == 3 and validate_state(svc.state.snapshot()) == []


def test_ошибка_старта_конвейера(env):
    svc, f, clock, _ = env
    f.fail_next = True
    assert code(svc.handle, "start", args()) == "pipeline"
    assert st(svc)["state"] == "error" and svc._act == {}


def test_канарейка_ключа_нигде_не_видна(env, caplog, tmp_path):
    svc, f, clock, fw = env
    caplog.set_level(logging.DEBUG)
    raw = CANARY + secrets.token_hex(3).encode()
    raw = raw.ljust(28, b"\0")
    key_b64 = base64.b64encode(raw).decode()
    seen = []
    a = args(key=raw)
    seen.append(json.dumps(svc.handle("start", a)))
    f.pipes[0].emit("error", message="boom")
    svc.tick()
    clock.advance(1.1)
    svc.tick()
    seen.append(json.dumps(svc.handle("status", {})))
    bad = args(key=raw[:10])
    with pytest.raises(CtlError) as e:
        svc.handle("start", bad)
    seen.append(str(e.value))
    svc.state.write()
    seen.append((tmp_path / "state.json").read_text())
    svc.handle("stop", {"stream": "main"})
    seen.append(caplog.text)
    blob = "\n".join(seen)
    assert "SHRG-CANARY-VIDEOKEY" not in blob and key_b64 not in blob
    assert repr(f.pipes[0].cfg).count("CANARY") == 0


def test_shutdown(env):
    svc, f, clock, _ = env
    run_to_streaming(svc, f, clock)
    svc.shutdown_streams()
    assert st(svc)["state"] == "idle" and f.pipes[0].stops == 1
    assert svc.status_line().startswith("rx ")
