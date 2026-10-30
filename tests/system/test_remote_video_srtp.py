# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Видеосеанс SRTP агента блока (ADR-0016, протокол 1.5) в настоящем процессе sheregesh_remote с подменной службой tx.

Loopback без TLS, как в test_module_agent. Служба sheregesh-video-tx заменена сокетом управления (CtlServer): команды
status / start / stop записываются и получают ответ по контракту. systemctl заменён скриптом, который дописывает свои
аргументы в журнал. Пульт — сырой сокет; heartbeat он подтверждает ack, иначе агент разорвёт соединение (сессия с ack,
--peer-timeout).

A — полный сеанс: video-key → ack → start (тот же ssrc, ключ, session, gen), статус srtp; ключ не попадает в статусы и
в журнал агента. B — bye и закрытие сокета: stop службы, allow@none. D — video-fallback старого пульта: stop, allow@none,
сеанс без видео (off) и без нового ключа. E — пульт 1.4: video-key не отправляется, video-key-ack игнорируется.
F — служба недоступна: видеотранспортов нет (TLS-видео удалено).
"""
import json
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from procs import Agent, wait_for
from sheregesh.link.codec import Decoder, encode
from sheregesh.videosvc.ctl import CtlServer

RUNTIME_ENV = "SHEREGESH_VIDEO_RUNTIME"
SYSTEMCTL_ENV = "SHEREGESH_SYSTEMCTL"
ALLOW_LINE = "start sheregesh-video-allow@127.0.0.1.service"
ALLOW_NONE_LINE = "start sheregesh-video-allow@none.service"
PEER_RX_PORT = 7444
PEER_RTCP_PORT = 7445


class Console:
    """Пульт по проводу: сырой сокет и читатель в отдельном потоке.

    Читатель работает с таймаутом (settimeout), поэтому закрытие сокета и остановка потока не зависают. Каждый heartbeat
    агента подтверждается ack: в сессии с ack агент разрывает соединение, если пульт молчит дольше --peer-timeout.
    """

    def __init__(self, port, proto):
        self.proto = proto
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.settimeout(0.2)
        self.msgs = []                       # принятые dict и BadMessage, в порядке прихода
        self._lock = threading.Lock()        # защищает msgs
        self._wlock = threading.Lock()       # защищает seq и запись в сокет
        self._seq = 0
        self._closing = threading.Event()
        self._reader = threading.Thread(target=self._read, daemon=True, name="console-reader")
        self._reader.start()

    def send(self, mtype, body):
        with self._wlock:
            self._seq += 1
            self.sock.sendall(encode(self.proto, mtype, self._seq, time.time(), body))

    def _read(self):
        dec = Decoder()
        while not self._closing.is_set():
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            for m in dec.feed(data):
                with self._lock:
                    self.msgs.append(m)
                if isinstance(m, dict) and m["type"] == "heartbeat":
                    try:
                        self.send("ack", {"seq": m["seq"]})
                    except OSError:
                        return

    def msgs_of(self, mtype):
        with self._lock:
            return [m for m in self.msgs if isinstance(m, dict) and m["type"] == mtype]

    def last_status(self):
        st = self.msgs_of("status")
        return st[-1]["body"] if st else None

    def recv_until(self, pred, timeout=10.0):
        """Первое принятое сообщение, для которого pred истинно; None по таймауту."""
        found = []

        def probe():
            with self._lock:
                hit = next((m for m in self.msgs if isinstance(m, dict) and pred(m)), None)
            if hit is not None:
                found.append(hit)
            return hit is not None
        wait_for(probe, timeout)
        return found[0] if found else None

    def close(self):
        self._closing.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self._reader.join(2)


class FakeTx:
    """Подменная служба tx: сокет управления. Каждая команда записывается в calls и получает ответ по контракту."""

    def __init__(self, sock_path):
        self.calls = []
        self._lock = threading.Lock()
        self._last = None                    # аргументы последнего start
        self._state = "idle"
        self._server = CtlServer(str(sock_path), self._handle)
        self._server.start()

    def _handle(self, cmd, args):
        with self._lock:
            self.calls.append((cmd, args))
            if cmd == "start":
                self._last, self._state = args, "streaming"
                return {"state": "starting"}
            if cmd == "stop":
                self._state = "idle"
                return {}
            if cmd == "status":
                if self._last is None:
                    return {"v": 1, "role": "tx", "streams": {"main": {"state": "idle", "gen": 0}}}
                return {"v": 1, "role": "tx", "streams": {"main": {
                    "state": self._state, "gen": self._last["gen"], "fps": 30.0, "bitrate_kbps": 6000,
                    "rtcp": {"keyframes_requested": 0, "keyframe_unsupported": False}, "error": ""}}}
            return {}

    def snapshot(self):
        with self._lock:
            return list(self.calls)

    def calls_of(self, cmd):
        return [args for c, args in self.snapshot() if c == cmd]

    def count(self, cmd):
        return len(self.calls_of(cmd))

    def starts(self):
        return self.calls_of("start")

    def close(self):
        self._server.close()


class Rig:
    """Окружение сценария: каталог служб, журнал systemctl-заглушки, пульт и агент; всё закрывается фикстурой."""

    def __init__(self, base):
        self.base = base
        self.runtime = base / "rt"
        self.journal = base / "systemctl.log"
        self.script = base / "systemctl.sh"
        self.env = {RUNTIME_ENV: str(self.runtime), SYSTEMCTL_ENV: str(self.script)}
        self.agents, self.consoles = [], []
        self.tx = None

    def systemctl(self):
        return self.journal.read_text() if self.journal.exists() else ""

    def launch(self, env=None):
        agent = Agent("--heartbeat-interval", "0.2", env=env or self.env)
        self.agents.append(agent)
        return agent

    def console(self, agent, proto="1.5"):
        """Пульт подключается, получает hello агента и отвечает своим hello. → (пульт, тело hello агента)."""
        c = Console(agent.port, proto)
        self.consoles.append(c)
        h = c.recv_until(lambda m: m["type"] == "hello")
        assert h is not None, "агент не прислал hello"
        body = {"proto": proto, "software": h["body"]["software"], "hardware": h["body"]["hardware"],
                "role": "console", "device_id": "c"}
        if proto == "1.5":
            body.update(video_transports=["srtp"], video_rx_port=PEER_RX_PORT)
        c.send("hello", body)
        return c, h["body"]


def video_of(c):
    return (c.last_status() or {}).get("video") or {}


def streaming(c):
    v = video_of(c)
    return v.get("transport") == "srtp" and (v.get("srtp") or {}).get("state") == "streaming"


def establish(rig, c):
    """Пульт принимает video-key, подтверждает его; служба получила start и отвечает streaming.
    → (тело video-key, аргументы start)."""
    vk = c.recv_until(lambda m: m["type"] == "video-key")
    assert vk is not None, rig.tx.snapshot()
    b = vk["body"]
    time.sleep(0.5)
    assert rig.tx.starts() == [], "start пришёл до video-key-ack"
    c.send("video-key-ack", {"stream": "main", "session": b["session"], "gen": b["gen"], "ok": True})
    starts = wait_for(lambda: rig.tx.starts(), 10)
    assert starts, rig.tx.snapshot()
    assert wait_for(lambda: streaming(c), 10), video_of(c)
    return b, starts[0]


@pytest.fixture
def rig():
    # Короткий путь: сокет unix ограничен ~104 байтами, длинный каталог pytest не подходит.
    base = Path(tempfile.mkdtemp(dir="/tmp", prefix="vsrtp-"))
    r = Rig(base)
    r.runtime.mkdir()
    r.script.write_text(f'#!/bin/sh\necho "$@" >> "{r.journal}"\nexit 0\n')
    r.script.chmod(0o755)
    r.tx = FakeTx(r.runtime / "ctl.sock")
    yield r
    for c in r.consoles:
        c.close()
    for a in r.agents:
        a.stop()
    r.tx.close()
    shutil.rmtree(base, ignore_errors=True)


def test_srtp_session_video_key_ack_start(rig):
    """A: полный сеанс SRTP; ключ есть только в video-key и в start, но не в статусах и журнале агента."""
    agent = rig.launch()
    c, hello = rig.console(agent)
    assert hello["video_transports"] == ["srtp"]
    b, a = establish(rig, c)
    assert b["transport"] == "srtp" and b["rtcp_port"] == PEER_RTCP_PORT and b["gen"] == 1
    assert b["srtp"]["cipher"] == "aes-128-gcm" and b["codec"] and isinstance(b["ssrc"], int) and b["ssrc"] > 0
    assert set(b["profile"]) == {"width", "height", "fps", "bitrate"}
    assert a["stream"] == "main" and a["session"] == b["session"] and a["gen"] == b["gen"]
    assert a["ssrc"] == b["ssrc"]
    assert a["srtp"] == {"cipher": "aes-128-gcm", "key_b64": b["srtp"]["key_b64"]}
    assert a["peer"] == {"ip": "127.0.0.1", "port": PEER_RX_PORT, "rtcp_port": PEER_RTCP_PORT}
    assert "source" in a and a["profile"] == b["profile"]
    assert ALLOW_LINE in rig.systemctl().splitlines()
    v = video_of(c)
    assert v["transport"] == "srtp" and v["srtp"]["state"] == "streaming" and v["srtp"]["gen"] == 1
    key = b["srtp"]["key_b64"]
    agent.stop()
    statuses = [json.dumps(m["body"], ensure_ascii=False) for m in c.msgs_of("status")]
    assert statuses and not any(key in s for s in statuses)
    err = agent.stderr()
    assert "video: tx start gen 1 ok" in err and "video: video-key-ack ok gen 1" in err
    assert key not in err


def test_bye_stops_service_and_closes_allow(rig):
    """B: bye и закрытие сокета → stop службы, затем systemctl allow@none."""
    agent = rig.launch()
    c, _ = rig.console(agent)
    establish(rig, c)
    c.send("bye", {})
    assert wait_for(lambda: rig.tx.count("stop") == 1, 5), rig.tx.snapshot()
    c.close()
    assert wait_for(lambda: ALLOW_NONE_LINE in rig.systemctl().splitlines(), 5), rig.systemctl()
    assert rig.tx.calls_of("stop")[0]["stream"] == "main"
    assert rig.systemctl().splitlines() == [ALLOW_LINE, ALLOW_NONE_LINE]


def test_fallback_stops_service_and_leaves_session_without_video(rig):
    """D: video-fallback (старый пульт) текущего поколения → stop службы, allow@none, режим off с причиной; нового
    ключа нет."""
    agent = rig.launch()
    c, _ = rig.console(agent)
    b, _ = establish(rig, c)
    c.send("video-fallback", {"stream": "main", "session": b["session"], "gen": b["gen"], "reason": "no-frames"})
    assert wait_for(lambda: rig.tx.count("stop") == 1, 5), rig.tx.snapshot()
    assert wait_for(lambda: ALLOW_NONE_LINE in rig.systemctl().splitlines(), 5), rig.systemctl()
    assert rig.tx.calls_of("stop")[0]["reason"] == "fallback"
    assert wait_for(lambda: (video_of(c).get("srtp") or {}).get("state") == "off", 5), video_of(c)
    assert video_of(c).get("transport") != "srtp" and video_of(c)["enabled"] is False
    time.sleep(1.5)
    assert len(c.msgs_of("video-key")) == 1, "после отката SRTP-поколение открыто заново"
    assert rig.tx.count("start") == 1


def test_console_14_gets_no_video_key(rig):
    """E: пульт 1.4 без video_transports — video-key не отправляется, video-key-ack игнорируется."""
    agent = rig.launch()
    c, hello = rig.console(agent, proto="1.4")
    assert hello["video_transports"] == ["srtp"]
    assert wait_for(lambda: c.msgs_of("status"), 5)
    c.send("video-key-ack", {"stream": "main", "session": "0" * 32, "gen": 1, "ok": True})
    time.sleep(1.5)
    assert c.msgs_of("video-key") == []
    assert rig.tx.count("start") == 0
    assert "allow" not in rig.systemctl()
    assert video_of(c).get("transport") != "srtp"


def test_service_unavailable_no_video_transports(rig):
    """F: сокет службы недоступен — hello агента с video_transports == [], video-key не приходит."""
    empty = rig.base / "empty"
    empty.mkdir()
    agent = rig.launch({**rig.env, RUNTIME_ENV: str(empty)})
    c, hello = rig.console(agent)
    assert hello["video_transports"] == []
    assert wait_for(lambda: c.msgs_of("status"), 5)
    time.sleep(1.5)
    assert c.msgs_of("video-key") == []
    assert video_of(c).get("transport") != "srtp"
    assert rig.tx.count("start") == 0 and rig.systemctl() == ""
