# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S7 (ADR-0013): кадры органов управления rc и rc-state между PeerClient и настоящим процессом агента (loopback).

50 Гц → ACTIVE, эхо ch/btn, задержка по rc_sent; прекращение кадров → FAILSAFE (timeout) ≤ timeout + 100 мс после
последнего кадра; закрытие сеанса → FAILSAFE (link-lost) сразу; en=false → IDLE (disabled); dev.ok=false → FAILSAFE
(device-lost); неверное тело — bad; send_rc будит поток канала (≤ 5 мс до блока); блок 1.3 — send_rc → unsupported,
пульт 1.3 — rc не исполняется, rc-state не передаётся (CHG-012). output всегда "none"."""
import json
import socket
import statistics
import threading
import time

import pytest

from procs import Agent, wait_for
from sheregesh import PROTOCOL_VERSION
from sheregesh.control import wire
from sheregesh.link.client import PeerClient
from sheregesh.link.codec import Decoder, encode

CH = [0, -12, 1000, 3, -1000, 0, 0, 1000]
DEV = {"name": "OpenTX Radiomaster Boxer Joystick", "ok": True}


def rc(en=True, ch=CH, btn=5, ok=True):
    return {"en": en, "ch": list(ch), "btn": btn, "dev": {**DEV, "ok": ok}}


class Sender(threading.Thread):
    """Поток пульта: client.send_rc с частотой hz, пока не остановлен."""

    def __init__(self, pc, hz=50, body=None):
        super().__init__(daemon=True)
        self.pc, self.period, self.body = pc, 1.0 / hz, body or rc()
        self.halt = threading.Event()
        self.results = []

    def run(self):
        nxt = time.monotonic()
        while not self.halt.is_set():
            self.results.append(self.pc.send_rc(self.body))
            nxt += self.period
            self.halt.wait(max(0.0, nxt - time.monotonic()))

    def stop(self):
        self.halt.set()
        self.join(2)


class Raw:
    """Пульт «по проводу»: hello с версией proto, отправка кадров, все принятые сообщения с временем приёма."""

    def __init__(self, port, proto=PROTOCOL_VERSION, hello=True):
        self.proto, self.seq = proto, 1
        self.s = socket.create_connection(("127.0.0.1", port), timeout=5)
        if hello:
            self.hello()
        self.msgs, self.lock = [], threading.Lock()
        self.t = threading.Thread(target=self._read, daemon=True)
        self.t.start()

    def hello(self):
        self.s.sendall(encode(self.proto, "hello", 1, 0.0, {"role": "console", "proto": self.proto, "software": "t",
                                                              "hardware": "HW-1.0 Rev A", "device_id": "t"}))

    def _read(self):
        dec = Decoder()
        try:
            while True:
                data = self.s.recv(65536)
                if not data:
                    return
                now = time.monotonic()
                with self.lock:
                    self.msgs += [(now, m) for m in dec.feed(data) if isinstance(m, dict)]
        except OSError:
            return

    def send(self, mtype, body):
        self.seq += 1
        self.s.sendall(encode(self.proto, mtype, self.seq, time.time(), body))
        return time.monotonic()

    def states(self):
        with self.lock:
            return [(t, m["body"]) for t, m in self.msgs if m["type"] == "rc-state"]

    def close(self):
        try:
            self.s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.s.close()
        self.t.join(2)


@pytest.fixture
def agent(tmp_path):
    agents = []

    def make(*extra):
        a = Agent("--heartbeat-interval", "0.3", "--status-file", str(tmp_path / f"st{len(agents)}.json"), *extra)
        a.status_file = tmp_path / f"st{len(agents)}.json"
        agents.append(a)
        return a
    yield make
    for a in agents:
        a.stop()


def health(a):
    try:
        return json.loads(a.status_file.read_text()).get("control") or {}
    except (OSError, ValueError):
        return {}


def connected(pc):
    return wait_for(lambda: pc.snapshot()["phase"] == "connected", 5)


def test_50hz_active_echo_latency_then_disable(agent):
    a = agent()
    pc = PeerClient("127.0.0.1", a.port).start()
    try:
        assert connected(pc) and pc.snapshot()["session"] == PROTOCOL_VERSION == "1.5"
        assert wait_for(lambda: (pc.snapshot()["rc_state"] or {}).get("state") == "IDLE", 3)   # сразу после hello
        assert pc.snapshot()["rc_state"]["reason"] == "start"
        s = Sender(pc)
        t0 = time.monotonic()
        s.start()
        assert wait_for(lambda: (pc.snapshot()["rc_state"] or {}).get("state") == "ACTIVE", 1.0, 0.01)
        assert time.monotonic() - t0 <= 1.0
        time.sleep(2.5)
        snap = pc.snapshot()
        st, sent = snap["rc_state"], snap["rc_sent"]
        assert st["ch"] == CH and st["btn"] == 5 and st["dev"] == DEV and st["output"] == "none"
        assert st["rate_hz"] >= 45 and st["gaps"] == 0 and st["stale"] == 0 and st["bad"] == 0
        assert st["failsafes"] == 0 and st["timeout_ms"] == 500 and st["late_max_ms"] < 200
        assert set(s.results) == {"queued"}
        assert 100 <= sent["n"] <= 160 and len(sent["ring"]) == 64 and sent["ring"][-1] == [sent["n"], sent["at"]]
        assert st["n"] <= sent["n"] and st["latency_ms"] is not None and 0 <= st["latency_ms"] < 100
        assert st["rx_age_s"] < 0.3
        h = health(a)
        assert h["state"] == "ACTIVE" and h["frames"] >= 100 and h["output"] == "none"
        s.stop()
        assert pc.send_rc(rc(en=False)) == "queued"      # выключение оператором: один кадр en=false
        assert wait_for(lambda: pc.snapshot()["rc_state"]["state"] == "IDLE", 1.0, 0.01)
        time.sleep(1.0)                                   # кадров больше нет — IDLE, не FAILSAFE
        st = pc.snapshot()["rc_state"]
        assert (st["state"], st["reason"], st["failsafes"]) == ("IDLE", "disabled", 0) and st["ch"] == [0] * 8
    finally:
        pc.stop()
    a.stop()
    err = a.stderr()
    assert "control: IDLE -> ACTIVE (fresh) n=1" in err and "control: ACTIVE -> IDLE (disabled)" in err


def test_frames_stop_failsafe_timing(agent):
    a = agent()
    c = Raw(a.port)
    try:
        last = 0.0
        for n in range(1, 51):                            # 1 с при 50 Гц
            last = c.send("rc", {"n": n, **rc()})
            time.sleep(0.02)
        assert wait_for(lambda: any(b["state"] == "FAILSAFE" for _t, b in c.states()), 2.0, 0.01)
        t_fs, b = next((t, b) for t, b in c.states() if b["state"] == "FAILSAFE")
        assert b["reason"] == "timeout" and b["ch"] == [0] * 8 and b["btn"] == 0 and b["output"] == "none"
        assert b["since_s"] == 0.0 and 500 <= b["age_ms"] <= 600, b     # на блоке: переход ≤ timeout + 100 мс
        assert 0.5 < t_fs - last <= 0.6 + 0.05, t_fs - last              # и у пульта (loopback)
        actives = [b for _t, b in c.states() if b["state"] == "ACTIVE"]
        assert actives and actives[-1]["ch"] == CH and actives[-1]["n"] == 50
        # период rc-state: 10 Гц в ACTIVE/FAILSAFE
        time.sleep(1.0)
        fs = [t for t, b in c.states() if b["state"] == "FAILSAFE"]
        assert 8 <= len(fs) <= 13
        # свежие кадры — снова ACTIVE (номера продолжаются в сеансе)
        c.send("rc", {"n": 51, **rc()})
        assert wait_for(lambda: c.states()[-1][1]["state"] == "ACTIVE", 1.0, 0.01)
        assert c.states()[-1][1]["failsafes"] == 1
    finally:
        c.close()


def test_session_close_link_lost(agent):
    a = agent()
    c = Raw(a.port)
    for n in range(1, 26):
        c.send("rc", {"n": n, **rc()})
        time.sleep(0.02)
    assert wait_for(lambda: c.states() and c.states()[-1][1]["state"] == "ACTIVE", 1.0, 0.01)
    c.close()
    assert wait_for(lambda: health(a).get("state") == "FAILSAFE", 2.0, 0.02)
    h = health(a)
    assert h["reason"] == "link-lost" and h["failsafes"] == 1
    assert h["age_ms"] - h["since_s"] * 1000 < 300        # переход сразу при закрытии, не по таймауту 500 мс
    c2 = Raw(a.port)                                      # новый сеанс видит состояние блока; номера — с 1
    try:
        assert wait_for(lambda: c2.states(), 2.0, 0.01)
        b = c2.states()[0][1]
        assert (b["state"], b["reason"], b["ch"]) == ("FAILSAFE", "link-lost", [0] * 8)
        c2.send("rc", {"n": 1, **rc()})
        assert wait_for(lambda: c2.states()[-1][1]["state"] == "ACTIVE", 1.0, 0.01)
        assert c2.states()[-1][1]["stale"] == 0
    finally:
        c2.close()
    a.stop()
    assert "control: ACTIVE -> FAILSAFE (link-lost)" in a.stderr()


def test_new_connection_without_hello_does_not_control(agent):
    """Сеанс принадлежит соединению: после завершённого нормального сеанса новое соединение без своего hello шлёт rc —
    сторож не переходит в ACTIVE и кадры не учитываются; после hello этого соединения — ACTIVE с n=1."""
    a = agent()
    c = Raw(a.port)
    for n in range(1, 11):
        c.send("rc", {"n": n, **rc()})
        time.sleep(0.02)
    assert wait_for(lambda: health(a).get("state") == "ACTIVE", 2.0, 0.02)
    c.close()
    assert wait_for(lambda: health(a).get("state") == "FAILSAFE", 2.0, 0.02)
    frames = health(a)["frames"]
    c2 = Raw(a.port, hello=False)
    try:
        for n in range(1, 26):
            c2.send("rc", {"n": n, **rc()})
            time.sleep(0.02)
        time.sleep(0.3)
        h = health(a)
        assert (h["state"], h["reason"], h["frames"]) == ("FAILSAFE", "link-lost", frames), h
        assert not c2.states()
        c2.hello()
        c2.send("rc", {"n": 1, **rc()})
        assert wait_for(lambda: c2.states() and c2.states()[-1][1]["state"] == "ACTIVE", 1.0, 0.01)
    finally:
        c2.close()
    a.stop()
    assert "FAILSAFE -> ACTIVE" in a.stderr()


def test_console_stop_link_lost_via_client(agent):
    a = agent()
    pc = PeerClient("127.0.0.1", a.port).start()
    s = Sender(pc)
    try:
        assert connected(pc)
        s.start()
        assert wait_for(lambda: (pc.snapshot()["rc_state"] or {}).get("state") == "ACTIVE", 1.0, 0.01)
        s.stop()
        pc.stop()                                         # bye и закрытие: как systemctl stop sheregesh-console
        t0 = time.monotonic()
        assert wait_for(lambda: health(a).get("state") == "FAILSAFE", 2.0, 0.02)
        assert health(a)["reason"] == "link-lost" and time.monotonic() - t0 < 0.5
    finally:
        s.stop()
        pc.stop()


def test_device_lost_and_bad_frames(agent):
    a = agent()
    c = Raw(a.port)
    try:
        for n in range(1, 11):
            c.send("rc", {"n": n, **rc()})
            time.sleep(0.02)
        c.send("rc", {"n": 11, **rc(ok=False)})
        assert wait_for(lambda: c.states()[-1][1]["state"] == "FAILSAFE", 1.0, 0.01)
        b = c.states()[-1][1]
        assert b["reason"] == "device-lost" and b["dev"]["ok"] is False and b["ch"] == [0] * 8
        c.send("rc", {"n": 12, **rc(), "arm": True})                     # лишний ключ
        c.send("rc", {"n": 13, **rc(ch=[2000] * 8)})                     # вне диапазона
        c.send("rc", {"n": 11, **rc()})                                  # устаревший номер
        time.sleep(0.4)
        b = c.states()[-1][1]
        assert b["bad"] == 2 and b["stale"] == 1 and b["state"] == "FAILSAFE" and b["n"] == 11
    finally:
        c.close()
    a.stop()
    err = a.stderr()
    assert err.count("control: bad rc frame") == 1 and "control: ACTIVE -> FAILSAFE (device-lost)" in err


def test_send_rc_wakes_link_thread():
    """send_rc → кадр у блока ≤ 5 мс (медиана): поток канала не ждёт таймаута приёма 0,2 с."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    arrivals, ready = [], threading.Event()

    def serve():
        conn, _ = srv.accept()
        conn.sendall(encode(PROTOCOL_VERSION, "hello", 1, 0.0, {"role": "remote", "proto": PROTOCOL_VERSION,
                                                                  "software": "t", "hardware": "HW-1.0 Rev A",
                                                                  "device_id": "remote-t"}))
        dec = Decoder()
        conn.settimeout(10)
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    return
                now = time.monotonic()
                for m in dec.feed(data):
                    if isinstance(m, dict) and m["type"] == "hello":
                        ready.set()
                    if isinstance(m, dict) and m["type"] == "rc":
                        arrivals.append((m["body"]["n"], now))
        except OSError:
            return
        finally:
            conn.close()

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    pc = PeerClient("127.0.0.1", srv.getsockname()[1], heartbeat_timeout_s=30).start()
    try:
        assert pc.send_rc(rc()) in ("offline", "queued")
        assert ready.wait(5) and connected(pc)
        delays = []
        for i in range(1, 41):
            time.sleep(0.03)                             # между кадрами поток канала успевает уснуть в select
            t = time.monotonic()
            assert pc.send_rc(rc(btn=i)) == "queued"
            assert wait_for(lambda: len(arrivals) >= i, 1.0, 0.0005)
            delays.append((arrivals[i - 1][1] - t) * 1000)
        assert [n for n, _t in arrivals] == list(range(1, 41))       # номера ставит канал, с 1, без пропусков
        assert statistics.median(delays) <= 5.0 and max(delays) < 100, delays
    finally:
        pc.stop()
        srv.close()


def test_only_last_frame_kept():
    """Без соединения кадр не ставится (offline); при соединении хранится только последний (не очередь)."""
    pc = PeerClient("127.0.0.1", 9)                      # не запущен: фаза connecting
    assert pc.send_rc(rc()) == "offline"
    with pc._lock:                                       # подменить фазу: проверка хранения без потока канала
        pc._st.phase, pc._st.session = "connected", PROTOCOL_VERSION
    assert pc.send_rc(rc(btn=1)) == "queued" and pc.send_rc(rc(btn=2)) == "queued"
    assert pc._rc["btn"] == 2 and pc.snapshot()["rc_sent"] == {"n": 0, "at": None, "pending": True, "ring": []}
    with pytest.raises(wire.BadRc):
        pc.send_rc({**rc(), "ch": [0] * 7})
    long = pc.send_rc({"n": 77, **rc(), "dev": {"name": "x" * 300, "ok": True}})
    assert long == "queued" and pc._rc["dev"]["name"] == "x" * wire.NAME_MAX and "n" not in pc._rc
    with pc._lock:
        pc._st.session = "1.3"
    assert pc.send_rc(rc()) == "unsupported"


def test_old_remote_13_unsupported(agent):
    a = agent("--protocol", "1.3")
    pc = PeerClient("127.0.0.1", a.port).start()
    try:
        assert connected(pc) and pc.snapshot()["session"] == "1.3"
        assert pc.send_rc(rc()) == "unsupported"
        time.sleep(1.0)
        snap = pc.snapshot()
        assert snap["rc_state"] is None and snap["rc_sent"]["n"] == 0
        assert health(a)["state"] == "IDLE" and health(a)["frames"] == 0
    finally:
        pc.stop()


def test_old_console_13_rc_not_executed(agent):
    a = agent()
    c = Raw(a.port, proto="1.3")
    try:
        for n in range(1, 21):
            c.send("rc", {"n": n, **rc()})
            time.sleep(0.02)
        time.sleep(1.5)
        with c.lock:
            types = {m["type"] for _t, m in c.msgs}
        assert "rc-state" not in types and "status" in types
        h = health(a)
        assert h["state"] == "IDLE" and h["frames"] == 0 and h["bad"] == 0
    finally:
        c.close()
    a.stop()
    assert "control:" not in a.stderr()
