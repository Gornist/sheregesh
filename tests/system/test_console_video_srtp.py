# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""ADR-0016, трек E2: PeerClient пульта с видеосеансом SRTP против подменного блока 1.5 (loopback) и подменной
службы rx (настоящий CtlServer из videosvc.ctl). video-key → служба получила start с тем же ключом → ack ok;
блок 1.4 → video-key не исполняется, hello честно объявляет транспорты."""
import shutil
import socket
import tempfile
import threading
import time

import pytest

from procs import wait_for
from sheregesh import PROTOCOL_VERSION, __version__
from sheregesh.link.client import PeerClient
from sheregesh.link.codec import Decoder, encode
from sheregesh.videosvc import session as vs
from sheregesh.videosvc.ctl import CtlServer
from sheregesh_console.videorx import VideoRx

KEY = bytes(range(40, 68))


class FakeBlock(threading.Thread):
    """Блок: принимает одно соединение, отвечает hello заданной версии, шлёт heartbeat, затем message (если задано)."""

    def __init__(self, proto, send_key):
        super().__init__(daemon=True)
        self.proto, self.send_key = proto, send_key
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(2)
        self.port = self.srv.getsockname()[1]
        self.got, self.hello = [], None
        self.halt = threading.Event()

    def run(self):
        self.srv.settimeout(10)
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        conn.settimeout(0.2)
        dec, seq, sent_key = Decoder(), 1, False
        conn.sendall(encode(self.proto, "hello", seq, time.time(),
                            {"role": "remote", "proto": self.proto, "software": __version__,
                             "hardware": "HW-1.0 Rev A (emulator)", "device_id": "blk"}))
        last = 0.0
        while not self.halt.is_set():
            if time.monotonic() - last > 0.2:
                seq += 1
                conn.sendall(encode(self.proto, "heartbeat", seq, time.time(), {}))
                last = time.monotonic()
                if self.send_key and not sent_key:
                    seq += 1
                    conn.sendall(encode(self.proto, "video-key", seq, time.time(), self.send_key))
                    sent_key = True
            try:
                data = conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            for m in dec.feed(data):
                if isinstance(m, dict):
                    self.got.append(m)
                    if m["type"] == "hello":
                        self.hello = m["body"]
        conn.close()

    def close(self):
        self.halt.set()
        self.srv.close()


@pytest.fixture
def rx_service():
    d = tempfile.mkdtemp(prefix="vrx")                      # короткий путь: лимит unix-сокета на macOS
    calls = []

    def handler(cmd, args):
        calls.append((cmd, args))
        return {}
    srv = CtlServer(f"{d}/ctl.sock", handler)
    srv.start()
    yield d, calls
    srv.close()
    shutil.rmtree(d, ignore_errors=True)


def make_client(block, d, units):
    vrx = VideoRx(lambda t, b: client.send_video(t, b), lambda: client.peer_ip(), runtime=d,
                  systemctl=lambda u: units.append(u) or True)
    client = PeerClient("127.0.0.1", block.port, video_session=vrx)
    vrx.start()
    client.start()
    return client, vrx


def test_video_key_reaches_service_and_ack_returns(rx_service):
    d, calls = rx_service
    units = []
    body = vs.video_key_body("main", "f" * 32, 1, 4242, "aes-128-gcm", KEY)
    block = FakeBlock(PROTOCOL_VERSION, body)
    block.start()
    client, vrx = make_client(block, d, units)
    try:
        wait_for(lambda: any(m["type"] == "video-key-ack" for m in block.got), 10)
        ack = next(m for m in block.got if m["type"] == "video-key-ack")["body"]
        assert ack == {"stream": "main", "session": "f" * 32, "gen": 1, "ok": True}
        assert block.hello["video_transports"] == ["srtp", "tls"] and block.hello["video_rx_port"] == 7444
        start = next(a for c, a in calls if c == "start")
        assert start["srtp"] == body["srtp"] and start["ssrc"] == 4242 and start["peer"] == {"ip": "127.0.0.1"}
        assert units == ["sheregesh-video-allow@127.0.0.1.service"]
        assert vrx.active
    finally:
        client.stop()
        vrx.stop()
        block.close()
    assert wait_for(lambda: not vrx.active, 5) is not False


def test_block_14_ignores_video_key_and_hello_still_announces(rx_service):
    d, calls = rx_service
    body = vs.video_key_body("main", "f" * 32, 1, 4242, "aes-128-gcm", KEY)
    block = FakeBlock("1.4", body)
    block.start()
    client, vrx = make_client(block, d, [])
    try:
        wait_for(lambda: block.hello is not None, 10)
        time.sleep(1.0)
        assert not any(c == "start" for c, _ in calls)
        assert not any(m["type"] == "video-key-ack" for m in block.got)
        assert not vrx.active
    finally:
        client.stop()
        vrx.stop()
        block.close()
