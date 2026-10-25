# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Клиент лабораторного канала на стороне пульта (ADR-0007, REQ-SYS-002, SW-003, SW-004).

Работает в фоновом потоке, GUI не блокирует. Реализует интерфейс sheregesh.hal.Peer.
Состояния: connecting (нет соединения: refused/timeout/closed) → handshake → connected | incompatible | auth-failed;
при отсутствии heartbeat дольше таймаута соединение считается потерянным и переустанавливается.

Режимы: loopback без TLS (разработка, тесты) и сетевой лабораторный профиль (tls_cfg, S3.1): TLS 1.3 с взаимной
аутентификацией, вторая сторона опознаётся по URI в сертификате; адрес — из конфигурации или поиском в подсети
(DHCP), но доверие только по сертификату.
"""
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .. import PROTOCOL_VERSION, __version__
from ..faults import CATALOG
from ..hal import PeerStatus
from ..protocol import negotiate
from . import tls
from .codec import TYPES, BadMessage, Decoder, encode

BACKOFF_S = (0.5, 1.0, 2.0, 4.0)       # задержка переподключения; не реже раза в 4 с (критерий ≤ 10 с)
AUTH_BACKOFF_S = 10.0                  # после отказа аутентификации — реже, без лавины попыток
DISCOVER_EVERY_S = 30.0
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
    phase: str = "connecting"             # connecting | handshake | connected | incompatible | auth-failed
    detail: str = "starting"              # starting | refused | timeout | closed | heartbeat | bad-messages | <reason>
    peer: dict = field(default_factory=dict)
    session: str | None = None
    last_rx: float | None = None           # monotonic последнего heartbeat
    telemetry: dict | None = None
    host: str | None = None                # адрес текущей/последней попытки
    tls: str | None = None                 # версия TLS сессии или None (loopback)
    lost_at: float | None = None           # monotonic потери установленного соединения


class PeerClient:
    def __init__(self, host="127.0.0.1", port=7700, heartbeat_timeout_s=3.0, device_id="console-emu-01",
                 hardware="HW-1.0 Rev A (emulator)", tls_cfg: "tls.LinkConfig | None" = None, status_file=None):
        self.tls_cfg = tls_cfg
        if tls_cfg is None:
            if not (host or "").startswith("127."):
                raise ValueError("без сетевого профиля (TLS) канал только через loopback (127.0.0.0/8)")
            self.hosts = [host]
            self.ctx = None
        else:
            cache = _read_cache(tls_cfg)
            self.hosts = list(dict.fromkeys(h for h in [host, *tls_cfg.peer_hosts, cache] if h))
            for h in self.hosts:
                try:
                    ipaddress.ip_address(h)
                except ValueError:
                    continue
                if not tls.is_lab_address(h):
                    raise ValueError(f"{h}: вне лабораторного контура")
            self.ctx = tls.make_context(tls_cfg, server=False)
            port = tls_cfg.port if port in (None, 7700) else port
            device_id = tls_cfg.own_uri
        self.host, self.port = self.hosts[0] if self.hosts else None, port
        self.status_file = status_file
        self._last_discover = 0.0
        self._last_status_write = 0.0
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
        self._outbox = []                   # (тип, тело) от других потоков; отправляет только поток канала
        self._vsel_req = 0
        self._vsel = None                   # последний запрос выбора видеоисточника (snapshot()["video_select"])

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

    def select_video_source(self, source_id: str, wait_s: float = 3.0):
        """Выбрать видеоисточник блока по id из status.video.sources (протокол 1.2, video-select).

        Сообщение уходит из потока канала; вызов ждёт результат блока (status.video.select с тем же req) не дольше
        wait_s (0 — не ждать). Возврат: None — принято (блок ответил ok или ответа ещё нет: переключение видно по
        sources[].active и source.state); отказ — {"ok": False, "error": код, "detail": текст}, код:
        not-connected (нет соединения), unsupported (сессия 1.1: старый блок), unknown (блок не знает id),
        unavailable (видео на блоке выключено). Неверная строка id — ValueError. Состояние запроса —
        snapshot()["video_select"] = {"req", "id", "state": queued|sent|offline|unsupported}. Вызывать не из GUI-потока."""
        if not isinstance(source_id, str) or not VIDEO_SOURCE_ID.fullmatch(source_id):
            raise ValueError(f"bad video source id {source_id!r}")
        with self._lock:
            self._vsel_req += 1
            req = self._vsel_req
            if self._st.phase != "connected":
                state = "offline"
            elif "video-select" not in TYPES.get(self._st.session or "", ()):
                state = "unsupported"
            else:
                state = "queued"
                self._outbox.append(("video-select", {"id": source_id, "req": req}))
            self._vsel = {"req": req, "id": source_id, "state": state}
        if state == "offline":
            return {"ok": False, "error": "not-connected", "detail": "no link to remote unit"}
        if state == "unsupported":
            return {"ok": False, "error": "unsupported", "detail": "remote unit does not support video-select (1.1)"}
        deadline = time.monotonic() + max(0.0, wait_s)
        while time.monotonic() < deadline and not self._stop.is_set():
            with self._lock:
                sel = ((self._st.telemetry or {}).get("video") or {}).get("select")
                if self._vsel and self._vsel["state"] == "offline":
                    return {"ok": False, "error": "not-connected", "detail": "link lost before sending"}
            if sel and sel.get("req") == req:
                if sel["result"] == "ok":
                    return None
                return {"ok": False, "error": "unknown" if sel["result"] == "unknown-id" else "unavailable",
                        "detail": sel.get("error", "")}
            time.sleep(0.05)
        return None

    # ---------- интерфейс Peer ----------
    def heartbeat_age(self):
        with self._lock:
            return None if self._st.last_rx is None else time.monotonic() - self._st.last_rx

    def snapshot(self):
        with self._lock:
            st = self._st
            return {"phase": st.phase, "detail": st.detail, "peer": dict(st.peer), "session": st.session,
                    "telemetry": st.telemetry, "counters": dict(self.counters.__dict__),
                    "heartbeat_age": None if st.last_rx is None else round(time.monotonic() - st.last_rx, 2),
                    "host": st.host, "port": self.port, "tls": st.tls, "secure": self.ctx is not None,
                    "lost_s": None if st.lost_at is None else round(time.monotonic() - st.lost_at, 1),
                    "peer_uri": self.tls_cfg.peer_uri if self.tls_cfg else None,
                    "video_select": dict(self._vsel) if self._vsel else None}

    def drain_link_events(self):
        with self._lock:
            ev, self._events = self._events, []
        return ev

    def status_for(self, now):
        """PeerStatus в шкале часов монитора now: last_heartbeat = now − возраст heartbeat."""
        with self._lock:
            st = self._st
            up = st.phase in ("connected", "incompatible")      # auth-failed: соединения нет, причина в detail
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

    def _event(self, event_code, **params):        # params может содержать свой «code» (REMOTE-EVENT)
        with self._lock:
            self._events.append((event_code, params))

    def _send(self, sock, mtype, body):
        self._seq += 1
        sock.sendall(encode(PROTOCOL_VERSION, mtype, self._seq, time.time(), body))

    def _run(self):
        attempt = 0
        while not self._stop.is_set():
            ok = self._session()
            self._write_status(force=True)
            if self._stop.is_set():
                break
            attempt = 0 if ok else attempt + 1
            wait = AUTH_BACKOFF_S if self._st.phase == "auth-failed" else BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
            self._stop.wait(wait)

    def _connect(self):
        """TCP к известным адресам, затем (сетевой профиль) поиск в подсети. Возвращает (sock, host) или None."""
        refused = False
        for h in self.hosts:
            try:
                return socket.create_connection((h, self.port), timeout=2.0), h
            except ConnectionRefusedError:
                refused = True
            except OSError:
                pass
        self._set(phase="connecting", detail="refused" if refused else "timeout", last_rx=None)
        if self.ctx is not None and self.tls_cfg.discover and time.monotonic() - self._last_discover > DISCOVER_EVERY_S:
            self._last_discover = time.monotonic()
            h = self._discover()
            if h:
                try:
                    return socket.create_connection((h, self.port), timeout=2.0), h
                except OSError:
                    pass
        return None

    def _tls(self, raw, host):
        """TLS 1.3 + проверка URI второй стороны. None и состояние auth-failed при отказе."""
        sock = self.ctx.wrap_socket(raw, do_handshake_on_connect=False)
        sock.settimeout(tls.HANDSHAKE_TIMEOUT_S)
        try:
            sock.do_handshake()
            why = tls.check_peer(sock.getpeercert(), self.tls_cfg.peer_uri)
        except ssl.SSLError as e:
            why = tls.describe_ssl_error(e)
        except OSError:
            sock.close()
            self._set(phase="connecting", detail="timeout", last_rx=None)
            return None
        if why:
            sock.close()
            self._set(phase="auth-failed", detail=f"auth: {why}", last_rx=None, host=host)
            self._event("LINK-AUTH-FAIL", reason=why)
            return None
        return sock

    def _discover(self):
        """Поиск второй стороны в подсети eth0 (или discover=CIDR): открытый порт → TLS → URI из конфигурации."""
        spec = self.tls_cfg.discover
        nets = [ipaddress.ip_network(spec, strict=False)] if spec and spec != "auto" else \
            [n.network for n in tls.local_networks() if n.network.prefixlen >= 22]
        hosts = [str(ip) for net in nets for ip in net.hosts()][:1024]
        if not hosts:
            return None

        def probe(h):
            try:
                with socket.create_connection((h, self.port), timeout=0.4):
                    return h
            except OSError:
                return None
        with ThreadPoolExecutor(64) as ex:
            candidates = [h for h in ex.map(probe, hosts) if h]
        for h in candidates:
            try:
                raw = socket.create_connection((h, self.port), timeout=2.0)
            except OSError:
                continue
            sock = self._tls(raw, h)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
                self.hosts = list(dict.fromkeys([h, *self.hosts]))
                _write_cache(self.tls_cfg, h)
                return h
        return None

    def _write_status(self, force=False):
        """Состояние канала для sheregesh-health (без ключей), не чаще раза в 2 с."""
        if not self.status_file or (not force and time.monotonic() - self._last_status_write < 2.0):
            return
        self._last_status_write = time.monotonic()
        snap = self.snapshot()
        snap.update(role="console", time=time.time(), software=__version__, proto=PROTOCOL_VERSION)
        try:
            path = Path(self.status_file)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(snap, ensure_ascii=False, default=str))
            os.replace(tmp, path)
        except OSError:
            pass

    def _session(self):
        """Одна попытка соединения. True — соединение было установлено (затем потеряно)."""
        got = self._connect()
        if got is None:
            return False
        sock, host = got
        self._set(host=host)
        if self.ctx is not None:
            sock = self._tls(sock, host)
            if sock is None:
                return False
            self._set(tls=sock.version())
        self._sock = sock
        self.counters.connects += 1
        self._last_peer_seq = None
        self._set(phase="handshake", detail="handshake", peer={}, session=None, telemetry=None)
        dec = Decoder()
        bad_in_row = 0
        rx0 = self.counters.received
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT_S
        established = False
        try:
            sock.settimeout(0.2)
            self._send(sock, "hello", self.info)
            while not self._stop.is_set():
                try:
                    data = sock.recv(65536)
                    if not data:
                        if self._st.phase == "auth-failed":
                            return False
                        if self.ctx is not None and self._st.phase == "handshake" and self.counters.received == rx0:
                            # TLS установлен, но блок закрыл соединение до hello — отказ в доступе этому пульту
                            self._set(phase="auth-failed", detail="auth: rejected by remote unit")
                            self._event("LINK-AUTH-FAIL", reason="rejected by remote unit")
                            return False
                        if self._st.phase != "incompatible":   # причина отказа важнее факта закрытия
                            self._set(detail="closed")
                        break
                except socket.timeout:
                    data = b""
                except (ssl.SSLError, OSError) as e:
                    # TLS 1.3: отказ сервера в сертификате пульта приходит алертом или сбросом уже после рукопожатия
                    if self.ctx is not None and self._st.phase in ("handshake", "auth-failed"):
                        if self._st.phase == "handshake":
                            why = tls.describe_ssl_error(e) if isinstance(e, ssl.SSLError) else "rejected by remote unit"
                            self._set(phase="auth-failed", detail=f"auth: {why}")
                            self._event("LINK-AUTH-FAIL", reason=why)
                        return False
                    raise
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
                self._flush_outbox(sock)
                self._write_status()
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
            with self._lock:
                for mtype, body in self._outbox:          # соединение потеряно до отправки
                    if mtype == "video-select" and self._vsel and self._vsel["req"] == body["req"]:
                        self._vsel["state"] = "offline"
                self._outbox.clear()
            try:
                sock.close()
            except OSError:
                pass
            self.counters.disconnects += 1
            with self._lock:
                if self._st.phase == "connected":
                    self._st.lost_at = time.monotonic()
                if self._st.phase not in ("incompatible", "auth-failed"):
                    self._st.phase = "connecting"
                self._st.last_rx = None
                self._st.tls = None
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
                self._set(phase="connected", detail="ok", peer=peer, session=n.session, last_rx=time.monotonic(),
                          lost_at=None)
            else:
                self._set(phase="incompatible", detail=n.reason, peer=peer, session=None, last_rx=time.monotonic())
        elif self._st.phase != "connected":
            if mtype == "error" and body.get("code") == "LINK-AUTH":
                self._set(phase="auth-failed", detail=f"auth: {str(body.get('reason', 'rejected'))[:40]}")
                self._event("LINK-AUTH-FAIL", reason=str(body.get("reason", "rejected"))[:40])
            elif mtype == "error":
                self._set(phase="incompatible", detail=str(body.get("reason", "rejected")))
            return                                       # до успешного рукопожатия данные не принимаются
        elif mtype == "heartbeat":
            self._set(last_rx=time.monotonic())
            self._ack(seq)
        elif mtype == "status":
            self._set(telemetry=_sanitize_status(body))
        elif mtype == "event":
            code = body.get("code")
            if code in CATALOG:
                self._event("REMOTE-EVENT", code=code)
                self._ack(seq)                           # диагностическое подтверждение доставки, не команда
            else:
                self.counters.ignored_messages += 1
        elif mtype == "bye":
            self._set(detail="closed")


    def _flush_outbox(self, sock):
        with self._lock:
            out, self._outbox = self._outbox, []
        for mtype, body in out:
            if mtype not in TYPES.get(self._st.session or "", ()):
                continue
            self._send(sock, mtype, body)                # OSError — обрыв: обрабатывает _session
            if mtype == "video-select":
                with self._lock:
                    if self._vsel and self._vsel["req"] == body["req"]:
                        self._vsel["state"] = "sent"

    def _ack(self, seq):
        if "ack" in TYPES.get(self._st.session or "", ()) and self._sock is not None:
            try:
                self._send(self._sock, "ack", {"seq": seq})
            except OSError:
                pass


def _read_cache(cfg):
    try:
        return json.loads((cfg.dir / "peer-cache.json").read_text()).get("host")
    except (OSError, ValueError, AttributeError):
        return None


def _write_cache(cfg, host):
    try:
        (cfg.dir / "peer-cache.json").write_text(json.dumps({"host": host, "time": time.time()}))
    except OSError:
        pass


# Те же наборы, что у пульта (sheregesh_console/videosource.py: INPUTS, SOURCE_STATES)
VIDEO_SOURCE_STATES = ("no-device", "no-signal", "starting", "streaming", "connect-error", "error", "reconnecting",
                       "retrying")    # два последних блок пока не сообщает: резерв, чтобы не терять source целиком
VIDEO_SOURCE_INPUTS = ("synthetic", "hdmi", "cvbs", "usb", "v4l2", "rtsp")
VIDEO_SOURCE_ID = re.compile(r"[A-Za-z0-9._:@+/-]{1,96}")       # sheregesh.videosources.ID_RE
VIDEO_SOURCES_MAX = 16
VIDEO_SELECT_RESULTS = ("ok", "unknown-id", "error")


def _sanitize_video_sources(v):
    """status.video.sources (sheregesh.videosources): не более 16 объектов {id, input, name, available, active};
    неверные элементы и повторы id отбрасываются. Не список — None."""
    if not isinstance(v, list):
        return None
    out, seen = [], set()
    for e in v[:VIDEO_SOURCES_MAX]:
        if not isinstance(e, dict) or not isinstance(e.get("id"), str) or not VIDEO_SOURCE_ID.fullmatch(e["id"]) \
                or e["id"] in seen or e.get("input") not in VIDEO_SOURCE_INPUTS:
            continue
        seen.add(e["id"])
        out.append({"id": e["id"], "input": e["input"],
                    "name": e["name"][:64] if isinstance(e.get("name"), str) else "",
                    "available": e.get("available") is True, "active": e.get("active") is True})
    return out


def _sanitize_video_select(v):
    """status.video.select — результат последнего video-select: {req, id, result: ok|unknown-id|error, error}."""
    if not isinstance(v, dict) or v.get("result") not in VIDEO_SELECT_RESULTS:
        return None
    req = v.get("req")
    return {"req": req if isinstance(req, int) and not isinstance(req, bool) and 0 <= req < 2 ** 31 else 0,
            "id": v["id"][:96] if isinstance(v.get("id"), str) else "", "result": v["result"],
            "error": v["error"][:200] if isinstance(v.get("error"), str) else ""}


def _sanitize_video_source(v):
    """status.video.source (sheregesh.videoingest.Ingest.status): только известные поля, типы и длины."""
    if not isinstance(v, dict) or v.get("state") not in VIDEO_SOURCE_STATES:
        return None

    def num(key, kind=float, hi=1e6):
        x = v.get(key)
        return kind(min(max(x, 0), hi)) if isinstance(x, (int, float)) and not isinstance(x, bool) else kind(0)

    def text(key, n):
        x = v.get(key)
        return x[:n] if isinstance(x, str) else ""
    return {"state": v["state"], "input": v["input"] if v.get("input") in VIDEO_SOURCE_INPUTS else "",
            "type": text("type", 16), "name": text("name", 64), "id": text("id", 96), "device": text("device", 128),
            "error": text("error", 200), "format": text("format", 8), "standard": text("standard", 8),
            "width": num("width", int, 16384), "height": num("height", int, 16384),
            "stream_width": num("stream_width", int, 16384), "stream_height": num("stream_height", int, 16384),
            "fps": num("fps", float, 1000), "fps_actual": num("fps_actual", float, 1000),
            "restarts": num("restarts", int, 10 ** 9), "since_s": num("since_s", float, 10 ** 9)}


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
                        "port": int(v["port"]) if isinstance(v.get("port"), int) and 1024 <= v["port"] <= 65535 else None,
                        "transport": "tls" if v.get("transport") == "tls" else None}
        src = _sanitize_video_source(v.get("source"))
        if src is not None:                       # видеоприём блока: необязательное поле (протокол 1.1 без изменений)
            out["video"]["source"] = src
        sources = _sanitize_video_sources(v.get("sources"))
        if sources is not None:                   # список источников блока (S4): необязательное поле
            out["video"]["sources"] = sources
        sel = _sanitize_video_select(v.get("select"))
        if sel is not None:                       # результат последнего video-select (протокол 1.2)
            out["video"]["select"] = sel
    ev = body.get("active_events")
    out["active_events"] = [str(c)[:32] for c in ev if isinstance(c, str)][:20] if isinstance(ev, list) else []
    if isinstance(body.get("uptime_s"), (int, float)):
        out["uptime_s"] = float(body["uptime_s"])
    out["state"] = str(body.get("state", ""))[:16]
    link = body.get("link")
    if isinstance(link, dict):
        out["link"] = {str(k)[:24]: float(v) for k, v in list(link.items())[:16]
                       if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return out


class RemotePeer:
    """Адаптер PeerClient к интерфейсу hal.Peer в шкале часов монитора."""

    def __init__(self, client: PeerClient, clock):
        self.client, self.clock = client, clock

    def status(self):
        return self.client.status_for(self.clock())

    def drain_link_events(self):
        return self.client.drain_link_events()
