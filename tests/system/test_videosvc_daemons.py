# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Системные тесты служб видео (ADR-0016, трек D): два процесса `sheregesh.videosvc.tx` и `.rx` по 127.0.0.1.

Нужен интерпретатор с gi и GStreamer (x264enc, avdec_h264, srtpenc, srtpdec): SHEREGESH_GI_PYTHON или
/opt/homebrew/bin/python3. Иначе тесты пропускаются. Службы запускаются отдельными процессами, команды — через
CtlClient; кодер и декодер на Mac задают переменные окружения (на платах — v4l2h264enc/v4l2h264dec).
"""
import base64
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "software"))

from sheregesh.videosvc.ctl import CtlClient  # noqa: E402


def _gi_python():
    cand = os.environ.get("SHEREGESH_GI_PYTHON") or "/opt/homebrew/bin/python3"
    if not shutil.which(cand) and not Path(cand).exists():
        return None
    code = ("import gi;gi.require_version('Gst','1.0');from gi.repository import Gst;Gst.init(None);"
            "import sys;sys.exit(0 if all(Gst.ElementFactory.find(e) for e in "
            "('x264enc','avdec_h264','srtpenc','srtpdec','rtpbin')) else 1)")
    try:
        r = subprocess.run([cand, "-c", code], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return cand if r.returncode == 0 else None


GI_PY = _gi_python()
pytestmark = pytest.mark.skipif(GI_PY is None, reason="нет интерпретатора с gi и GStreamer (SHEREGESH_GI_PYTHON)")

ENCODER = "x264enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate_kbps} key-int-max=60"


class Pair:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="svd", dir="/tmp")            # короткий путь: лимит unix-сокета
        self.procs = {}
        self.clients = {}
        self.base = 18000 + secrets.randbelow(3000) * 8
        self.ssrc = secrets.randbelow(0xFFFFFFF0) + 1
        self.session = secrets.token_hex(16)

    def launch(self, role):
        d = os.path.join(self.dir, role)
        os.makedirs(d, mode=0o700)
        env = dict(os.environ, PYTHONPATH=str(ROOT / "software"), SHEREGESH_VIDEO_ENCODER_FRAGMENT=ENCODER,
                   SHEREGESH_VIDEO_DECODER="avdec_h264", SHEREGESH_VIDEO_RX_SINK="fakesink sync=false",
                   SHEREGESH_VIDEO_NFT_CHECK="0")
        env.pop("NOTIFY_SOCKET", None)
        p = subprocess.Popen([GI_PY, "-W", "ignore", "-m", f"sheregesh.videosvc.{role}", "--runtime-dir", d],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs[role] = p
        sock = os.path.join(d, "ctl.sock")
        for _ in range(100):
            if os.path.exists(sock):
                break
            assert p.poll() is None, p.stdout.read()
            time.sleep(0.1)
        self.clients[role] = CtlClient(sock, timeout=10)
        return self.clients[role]

    def state(self, role, stream="main"):
        try:
            with open(os.path.join(self.dir, role, "state.json")) as f:
                return json.load(f)["streams"][stream]
        except (OSError, ValueError, KeyError):
            return {}

    def wait(self, role, pred, timeout):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            s = self.state(role)
            if s and pred(s):
                return s
            time.sleep(0.1)
        return self.state(role)

    def start_both(self, tx_key, rx_key, rx_ssrc=None, gen=1):
        tx, rx = self.clients["tx"], self.clients["rx"]
        b = self.base
        rx.request("start", {"stream": "main", "session": self.session, "gen": gen, "ssrc": rx_ssrc or self.ssrc,
                             "peer": {"ip": "127.0.0.1"},
                             "local": {"port": b, "rtcp_port": b + 2, "rtcp_peer_port": b + 3},
                             "srtp": {"cipher": "aes-128-gcm", "key_b64": base64.b64encode(rx_key).decode()},
                             "codec": "h264"})
        tx.request("start", {"stream": "main", "session": self.session, "gen": gen, "ssrc": self.ssrc,
                             "peer": {"ip": "127.0.0.1", "port": b, "rtcp_port": b + 2, "rtcp_local_port": b + 3},
                             "srtp": {"cipher": "aes-128-gcm", "key_b64": base64.b64encode(tx_key).decode()},
                             "source": {"source": "synthetic"},
                             "profile": {"width": 640, "height": 360, "fps": 30, "bitrate": 2000}})

    def close(self):
        for c in self.clients.values():
            c.close()
        for p in self.procs.values():
            if p.poll() is None:
                p.kill()
            p.wait(10)
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture
def pair():
    p = Pair()
    try:
        p.launch("tx")
        p.launch("rx")
        yield p
    finally:
        p.close()


def test_кадры_идут_и_stop_возвращает_idle(pair):
    key = secrets.token_bytes(28)
    pair.start_both(key, key)
    s = pair.wait("rx", lambda s: s["state"] == "streaming" and s["frames"] > 30, 8)
    assert s["state"] == "streaming" and s["frames"] > 30, s
    assert pair.state("tx")["state"] in ("starting", "streaming")
    pair.clients["tx"].request("stop", {"stream": "main", "reason": "test"})
    pair.clients["rx"].request("stop", {"stream": "main", "reason": "test"})
    assert pair.wait("rx", lambda s: s["state"] == "idle", 3)["state"] == "idle"
    assert pair.state("tx")["state"] == "idle"
    assert pair.clients["tx"].request("status")["streams"]["main"]["state"] == "idle"


def test_чужой_ключ_ноль_кадров(pair):
    pair.start_both(secrets.token_bytes(28), secrets.token_bytes(28))
    time.sleep(4)
    s = pair.state("rx")
    assert s["frames"] == 0 and s["state"] != "streaming", s


def test_задержка_после_set_clock_offset(pair):
    key = secrets.token_bytes(28)
    pair.clients["rx"].request("set-clock-offset", {"offset_s": 0.0})
    pair.start_both(key, key)
    s = pair.wait("rx", lambda s: s["latency_ms"]["samples"] > 10, 10)
    lat = s["latency_ms"]
    assert lat["samples"] > 10 and lat["p50"] is not None and lat["p50"] < 1000, s


def test_задержка_null_без_смещения(pair):
    key = secrets.token_bytes(28)
    pair.start_both(key, key)
    s = pair.wait("rx", lambda s: s["frames"] > 30, 8)
    assert s["frames"] > 30 and s["latency_ms"]["p50"] is None


@pytest.mark.parametrize("role", ["tx", "rx"])
def test_sigterm_выход_ноль_и_state_idle(pair, role):
    key = secrets.token_bytes(28)
    pair.start_both(key, key)
    pair.wait("rx", lambda s: s["frames"] > 5, 8)
    p = pair.procs[role]
    p.send_signal(signal.SIGTERM)
    assert p.wait(15) == 0
    assert pair.state(role)["state"] == "idle"


def test_tx_неверный_start_отвечает_bad_request_не_роняя_поток(pair):
    key = secrets.token_bytes(28)
    pair.start_both(key, key)
    pair.wait("tx", lambda s: s["state"] == "streaming", 8)
    from sheregesh.videosvc.ctl import CtlError
    with pytest.raises(CtlError) as e:
        pair.clients["tx"].request("start", {"stream": "main", "session": pair.session, "gen": 0})
    assert e.value.code == "bad-request"
