# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Клиент лабораторного канала на стороне пульта (ADR-0007, REQ-SYS-002, SW-003, SW-004).

Работает в фоновом потоке, GUI не блокирует. Реализует интерфейс sheregesh.hal.Peer.
Состояния: connecting (нет соединения: refused/timeout/closed) → handshake → connected | incompatible;
при отсутствии heartbeat дольше таймаута соединение считается потерянным и переустанавливается.
"""
import socket
import threading
import time
from dataclasses import dataclass, field

from .. import PROTOCOL_VERSION, __version__
from ..faults import CATALOG
from ..hal import PeerStatus
from ..protocol import negotiate
from .codec import TYPES, BadMessage, Decoder, encode

BACKOFF_S = (0.5, 1.0, 2.0, 4.0)       # задержка переподключения; не реже раза в 4 с (критерий ≤ 10 с)
HANDSHAKE_TIMEOUT_S = 3.0
MAX_BAD_IN_ROW = 10


@dataclass
class LinkCounters:
    connects: int = 0
    disconnects: int = 0
    bad_messages: int = 0
    ignored_messages: int = 0
    received: int = 0
    seq_gaps: int = 0
    last_bad_reason: str = ""


@dataclass
class _State:
    phase: str = "connecting"             # connecting | handshake | connected | incompatible
    detail: str = "starting"              # starting | refused | timeout | closed | heartbeat | bad-messages | <reason>
    peer: dict = field(default_factory=dict)
    session: str | None = None
    last_rx: float | None = None           # monotonic последнего heartbeat
    telemetry: dict | None = None


class PeerClient:
    def __init__(self, host="127.0.0.1", port=7700, heartbeat_timeout_s=3.0, device_id="console-emu-01",
                 hardware="HW-1.0 Rev A (emulator)"):
        if not host.startswith("127."):
            raise ValueError("S1.5: канал только через loopback (127.0.0.0/8)")
        self.host, self.port = host, port
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.info = {"role": "console", "proto": PROTOCOL_VERSION, "software": __version__, "hardware": hardware,
                     "device_id": device_id}
        self.counters = LinkCounters()
        self._st = _State()
        self._lock = threading.Lock()
        self._events = []                   # (code, params) для журнала монитора
        self._stop = threading.Event()
        self._sock = None
        self._thread = threading.Thread(target=self._run, name="peer-link", daemon=True)
        self._seq = 0
        self._last_peer_seq = None

    # ---------- жизненный цикл ----------
    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        s = self._sock
        if s is not None:
            try:
                self._send(s, "bye", {})
            except OSError:
                pass
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._thread.join(timeout=5)

    def set_heartbeat_timeout(self, seconds):
        self.heartbeat_timeout_s = float(seconds)

    # ---------- интерфейс Peer ----------
    def heartbeat_age(self):
        with self._lock:
            return None if self._st.last_rx is None else time.monotonic() - self._st.last_rx

    def snapshot(self):
        with self._lock:
            st = self._st
            return {"phase": st.phase, "detail": st.detail, "peer": dict(st.peer), "session": st.session,
                    "telemetry": st.telemetry, "counters": dict(self.counters.__dict__),
                    "heartbeat_age": None if st.last_rx is None else round(time.monotonic() - st.last_rx, 2)}

    def drain_link_events(self):
        with self._lock:
            ev, self._events = self._events, []
        return ev

    def status_for(self, now):
        """PeerStatus в шкале часов монитора now: last_heartbeat = now − возраст heartbeat."""
        with self._lock:
            st = self._st
            up = st.phase in ("connected", "incompatible")
            age = None if st.last_rx is None else time.monotonic() - st.last_rx
            if st.phase == "incompatible":
                age = 0.0                      # ответ получен; монитор должен дойти до проверки версий
            return PeerStatus(up, (now - age) if (up and age is not None) else None, st.peer.get("proto"),
                              st.peer.get("software"), st.peer.get("hardware"), st.peer.get("device_id"),
                              detail=st.detail, telemetry=st.telemetry, session=st.session)

    # ---------- внутреннее ----------
    def _set(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self._st, k, v)

    def _event(self, code, **params):
        with self._lock:
            self._events.append((code, params))

    def _send(self, sock, mtype, body):
        self._seq += 1
        sock.sendall(encode(PROTOCOL_VERSION, mtype, self._seq, time.time(), body))

    def _run(self):
        attempt = 0
        while not self._stop.is_set():
            ok = self._session()
            if self._stop.is_set():
                break
            attempt = 0 if ok else attempt + 1
            self._stop.wait(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])

    def _session(self):
        """Одна попытка соединения. True — соединение было установлено (затем потеряно)."""
        try:
            sock = socket.create_connection((self.host, self.port), timeout=2.0)
        except ConnectionRefusedError:
            self._set(phase="connecting", detail="refused", last_rx=None)
            return False
        except OSError:
            self._set(phase="connecting", detail="timeout", last_rx=None)
            return False
        self._sock = sock
        self.counters.connects += 1
        self._last_peer_seq = None
        self._set(phase="handshake", detail="handshake", peer={}, session=None, telemetry=None)
        dec = Decoder()
        bad_in_row = 0
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT_S
        established = False
        try:
            sock.settimeout(0.2)
            self._send(sock, "hello", self.info)
            while not self._stop.is_set():
                try:
                    data = sock.recv(65536)
                    if not data:
                        if self._st.phase != "incompatible":   # причина отказа важнее факта закрытия
                            self._set(detail="closed")
                        break
                except socket.timeout:
                    data = b""
                for msg in dec.feed(data):
                    if isinstance(msg, BadMessage):
                        self.counters.bad_messages += 1
                        self.counters.last_bad_reason = msg.reason
                        bad_in_row += 1
                        self._event("LINK-BAD-MSG", reason=msg.reason, count=self.counters.bad_messages)
                        if bad_in_row >= MAX_BAD_IN_ROW:
                            self._set(detail="bad-messages")
                            return established
                        continue
                    bad_in_row = 0
                    self.counters.received += 1
                    self._on_message(msg)
                    established = established or self._st.phase in ("connected", "incompatible")
                phase = self._st.phase
                now = time.monotonic()
                if phase == "handshake" and now > deadline:
                    self._set(detail="handshake-timeout")
                    break
                age = self.heartbeat_age()
                if phase == "connected" and age is not None and age > self.heartbeat_timeout_s:
                    self._set(detail="heartbeat")          # соединение «живо», но блок молчит
                    self._event("LINK-HB-TIMEOUT", age=round(age, 1))
                    break
        except OSError:
            if self._st.phase != "incompatible":
                self._set(detail="closed")
        finally:
            self._sock = None
            try:
                sock.close()
            except OSError:
                pass
            self.counters.disconnects += 1
            with self._lock:
                if self._st.phase != "incompatible":
                    self._st.phase = "connecting"
                self._st.last_rx = None
        return established

    def _on_message(self, msg):
        seq = msg["seq"]
        if self._last_peer_seq is not None and seq != self._last_peer_seq + 1:
            self.counters.seq_gaps += 1
        self._last_peer_seq = seq
        mtype, body = msg["type"], msg["body"]
        session = self._st.session or PROTOCOL_VERSION
        if mtype not in TYPES.get(session, ()):
            self.counters.ignored_messages += 1          # тип новее версии сессии: учесть и не исполнять
            return
        if mtype == "hello":
            n = negotiate(PROTOCOL_VERSION, body.get("proto"), body.get("software"), body.get("hardware"))
            peer = {k: body.get(k) for k in ("proto", "software", "hardware", "device_id")}
            if n.compatible:
                self._set(phase="connected", detail="ok", peer=peer, session=n.session, last_rx=time.monotonic())
            else:
                self._set(phase="incompatible", detail=n.reason, peer=peer, session=None, last_rx=time.monotonic())
        elif self._st.phase != "connected":
            if mtype == "error":
                self._set(phase="incompatible", detail=str(body.get("reason", "rejected")))
            return                                       # до успешного рукопожатия данные не принимаются
        elif mtype == "heartbeat":
            self._set(last_rx=time.monotonic())
        elif mtype == "status":
            self._set(telemetry=_sanitize_status(body))
        elif mtype == "event":
            code = body.get("code")
            if code in CATALOG:
                self._event("REMOTE-EVENT", code=code)
            else:
                self.counters.ignored_messages += 1
        elif mtype == "bye":
            self._set(detail="closed")


def _sanitize_status(body):
    """Только ожидаемые поля и типы; остальное отбрасывается (данные второй стороны недоверенные)."""
    out = {}
    temps = body.get("temperatures")
    if isinstance(temps, dict):
        out["temperatures"] = {str(k)[:16]: float(v) for k, v in temps.items()
                               if isinstance(v, (int, float)) and not isinstance(v, bool)}
    p = body.get("power")
    if isinstance(p, dict) and isinstance(p.get("percent"), (int, float)):
        out["power"] = {"source": str(p.get("source", ""))[:16], "percent": float(p["percent"]),
                        "low_battery": bool(p.get("low_battery"))}
    v = body.get("video")
    if isinstance(v, dict):
        out["video"] = {"enabled": bool(v.get("enabled")), "active": bool(v.get("active")),
                        "codec": v.get("codec") if v.get("codec") in ("h264", "jpeg") else None,
                        "port": int(v["port"]) if isinstance(v.get("port"), int) and 1024 <= v["port"] <= 65535 else None}
    ev = body.get("active_events")
    out["active_events"] = [str(c)[:32] for c in ev if isinstance(c, str)][:20] if isinstance(ev, list) else []
    if isinstance(body.get("uptime_s"), (int, float)):
        out["uptime_s"] = float(body["uptime_s"])
    out["state"] = str(body.get("state", ""))[:16]
    return out


class RemotePeer:
    """Адаптер PeerClient к интерфейсу hal.Peer в шкале часов монитора."""

    def __init__(self, client: PeerClient, clock):
        self.client, self.clock = client, clock

    def status(self):
        return self.client.status_for(self.clock())

    def drain_link_events(self):
        return self.client.drain_link_events()
