# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Клиент лабораторного канала на стороне пульта (ADR-0007, REQ-SYS-002, SW-003, SW-004).

Работает в фоновом потоке, GUI не блокирует. Реализует интерфейс sheregesh.hal.Peer.
Состояния: connecting (нет соединения: refused/timeout/closed) → handshake → connected | incompatible | auth-failed;
при отсутствии heartbeat дольше таймаута соединение считается потерянным и переустанавливается.

Режимы: loopback без TLS (разработка, тесты) и сетевой лабораторный профиль (tls_cfg, S3.1): TLS 1.3 с взаимной
аутентификацией, вторая сторона опознаётся по URI в сертификате; адрес — из конфигурации или поиском в подсети
(DHCP), но доверие только по сертификату.

Поток канала ждёт данные блока в select вместе с сокетом пробуждения: сообщения от других потоков (video-select,
module-cmd, remote-cmd, кадры органов управления rc) уходят сразу, без ожидания таймаута приёма 0,2 с.
"""
import ipaddress
import json
import math
import os
import re
import select
import socket
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .. import PROTOCOL_VERSION, __version__
from ..control import wire as control_wire
from ..faults import CATALOG
from ..hal import PeerStatus
from ..module import wire
from ..protocol import negotiate
from . import discovery, remotecmd, tls
from .requests import LOST, OFFLINE, QUEUED, UNSUPPORTED, Requests
from .codec import TYPES, BadMessage, Decoder, encode

BACKOFF_S = (0.5, 1.0, 2.0, 4.0)       # задержка переподключения; не реже раза в 4 с (критерий ≤ 10 с)
AUTH_BACKOFF_S = 10.0                  # после отказа аутентификации — реже, без лавины попыток
DISCOVER_EVERY_S = 30.0
HANDSHAKE_TIMEOUT_S = 3.0
MAX_BAD_IN_ROW = 10
RX_WAIT_S = 0.2                        # ожидание данных блока за один оборот цикла канала
RC_RING = 64                           # времена отправки последних кадров rc (задержка по rc-state.n)


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
    def __init__(self, host="127.0.0.1", port=7700, heartbeat_timeout_s=3.0, device_id=None,
                 hardware=None, tls_cfg: "tls.LinkConfig | None" = None, status_file=None,
                 video_session=None):
        self.tls_cfg = tls_cfg
        # Видеосеанс SRTP (протокол 1.5, ADR-0016): объект с hello_fields(), on_message(тип, body), on_session_start(),
        # on_session_end(), peer_ip — sheregesh_console.videorx.VideoRx; None — только TLS-видео, hello без video_*.
        self.video_session = video_session
        self._clock_samples = deque(maxlen=20)   # ts блока − время приёма (с) по heartbeat текущего соединения
        self._rtt_min = None                     # наименьший RTT (мс), сообщённый блоком в этом соединении
        if tls_cfg is None:
            if not (host or "").startswith("127."):
                raise ValueError("без сетевого профиля (TLS) канал только через loopback (127.0.0.0/8)")
            self.hosts = [host]
            self.ctx = None
        else:
            cache = discovery.read_cache(tls_cfg)
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
        if hardware is None or device_id is None:      # REQ-SYS-007: плата и серийный номер, вне платы — эмулятор
            from .. import info as devinfo
            hardware = devinfo.hardware() if hardware is None else hardware
            device_id = devinfo.device_id("console") if device_id is None else device_id
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
        self._send_lock = threading.Lock()          # bye из stop() (поток GUI) и поток канала пишут в один сокет
        self._last_peer_seq = None
        self._outbox = []                   # (тип, тело) от других потоков; отправляет только поток канала
        self._req = Requests()              # video-select, module-cmd, remote-cmd: номер и состояние последнего
        self._rcmd = {}                     # remote-cmd (1.6): req → {"asm": remotecmd.Assembly, "result": dict | None}
        # Органы управления (протокол 1.4, ADR-0013): только последний кадр, не очередь; номер n — в потоке канала
        # при отправке (новое соединение — с 1), поэтому перезаписанные до отправки кадры не дают пропусков номеров.
        self._rc = None                     # тело последнего кадра rc без n, ещё не отправленное
        self._rc_n = 0                      # номер последнего отправленного кадра в сеансе
        self._rc_at = None                  # monotonic отправки кадра _rc_n
        self._rc_ring = deque(maxlen=RC_RING)   # (n, monotonic отправки)
        self._rc_state = None               # последний rc-state блока (sanitize_rc_state) + rx_at, latency_ms
        # Пробуждение потока канала из других потоков (select на сокете канала и _wake_r)
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)

    # ---------- жизненный цикл ----------
    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._wake()
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
        if not self._thread.is_alive():
            for w in (self._wake_r, self._wake_w):
                w.close()

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
            req = self._req.new("video-select")
            if self._st.phase != "connected":
                state = OFFLINE
            elif "video-select" not in TYPES.get(self._st.session or "", ()):
                state = UNSUPPORTED
            else:
                state = QUEUED
                self._outbox.append(("video-select", {"id": source_id, "req": req}))
            self._req.open("video-select", req, state, id=source_id)
        if state == QUEUED:
            self._wake()
        if state == OFFLINE:
            return {"ok": False, "error": "not-connected", "detail": "no link to remote unit"}
        if state == UNSUPPORTED:
            return {"ok": False, "error": "unsupported", "detail": "remote unit does not support video-select (1.1)"}
        deadline = time.monotonic() + max(0.0, wait_s)
        while time.monotonic() < deadline and not self._stop.is_set():
            with self._lock:
                sel = ((self._st.telemetry or {}).get("video") or {}).get("select")
                if self._req.state("video-select", req) == OFFLINE:
                    return {"ok": False, "error": "not-connected", "detail": "link lost before sending"}
                if sel and sel.get("req") == req:
                    self._req.completed("video-select", req)
            if sel and sel.get("req") == req:
                if sel["result"] == "ok":
                    return None
                return {"ok": False, "error": "unknown" if sel["result"] == "unknown-id" else "unavailable",
                        "detail": sel.get("error", "")}
            time.sleep(0.05)
        # Итога нет: принято (переключение видно по sources[].active); в состоянии — timeout или unknown (обрыв)
        with self._lock:
            if wait_s > 0:
                self._req.timed_out("video-select", req)
        return None

    def send_video(self, mtype: str, body: dict) -> bool:
        """Сообщение видеосеанса блоку (video-key-ack, video-fallback; протокол 1.5). Из любого потока, не ждёт:
        True — поставлено в очередь потока канала (уйдёт сразу), False — нет соединения или сессия < 1.5."""
        with self._lock:
            if self._st.phase != "connected" or mtype not in TYPES.get(self._st.session or "", ()):
                return False
            self._outbox.append((mtype, body))
        self._wake()
        return True

    def peer_ip(self):
        """Адрес блока текущего соединения (для nftables видеослужбы) или None."""
        s = self._sock
        try:
            return s.getpeername()[0] if s is not None else None
        except OSError:
            return self._st.host

    def clock_offset(self):
        """Оценка смещения часов блок − пульт (с) или None. По heartbeat блока: ts блока − момент приёма = смещение
        минус задержка доставки. Как фильтр часов NTP: берётся выборка с наименьшей задержкой (наибольшее значение из
        последних 20) и половина наименьшего RTT блока в этом соединении — задержанные heartbeat и выбросы RTT оценку не
        сдвигают (стенд: rtt_max 1,5 с при обычных 2 мс; прежняя медиана + rtt_avg/2 зависела от них). Без RTT —
        None. Точность — порядка асимметрии сети (доли миллисекунды в проводной сети)."""
        with self._lock:
            rtt, samples = self._rtt_min, list(self._clock_samples)
        if not samples or rtt is None:
            return None
        return max(samples) + rtt / 2000.0

    def _on_rtt(self, rtt_ms):
        """RTT блока из status (link.rtt_last_ms): наименьший за соединение — оценка задержки без очередей."""
        if isinstance(rtt_ms, (int, float)) and not isinstance(rtt_ms, bool) and 0 <= rtt_ms < 60000:
            with self._lock:
                self._rtt_min = rtt_ms if self._rtt_min is None else min(self._rtt_min, rtt_ms)

    def module_command(self, op: str, args: dict | None = None, wait_s: float = 5.0) -> dict:
        """Команда подсистеме внешнего модуля блока (протокол 1.3, module-cmd; ADR-0012, разделы 6.3 и 9.2).

        → {"ok": bool, "result": str, "error": str, "req": int}. Вызывать не из GUI-потока. result: код блока
        (sheregesh.module.wire.RESULTS) | not-connected (нет соединения, или потеряно до отправки или до итога — итог
        тогда неизвестен) | unsupported (сессия < 1.3, или блок 1.3 не сообщает status.module: подсистема выключена)
        | timeout (нет итога с этим req за wait_s; для configure — итог ещё pending) | bad-request (op не из OPS,
        неверные args — проверка wire.check_command до отправки). Состояние запроса —
        snapshot()["module_cmd"] = {"req", "op", "state": queued|sent|offline|unsupported}."""
        with self._lock:
            req = self._req.new("module-cmd")
        body = {"req": req, "op": op, "args": dict(args or {})}
        try:
            wire.check_command(body)
        except wire.BadCommand as e:
            return {"ok": False, "result": "bad-request", "error": e.text, "req": req}
        with self._lock:
            st = self._st
            if st.phase != "connected":
                state = OFFLINE
            elif "module-cmd" not in TYPES.get(st.session or "", ()):
                state = UNSUPPORTED
            elif st.telemetry is not None and "module" not in st.telemetry:
                state = UNSUPPORTED                 # блок 1.3 без подсистемы модуля (--no-module)
            else:
                state = QUEUED
                self._outbox.append(("module-cmd", body))
            self._req.open("module-cmd", req, state, op=op)
            connects = self.counters.connects
        if state == QUEUED:
            self._wake()
        if state == OFFLINE:
            return {"ok": False, "result": "not-connected", "error": "no link to remote unit", "req": req}
        if state == UNSUPPORTED:
            return {"ok": False, "result": "unsupported", "req": req,
                    "error": "remote unit does not support the external module"
                             + (f" (protocol {st.session})" if st.session and "module-cmd" not in TYPES.get(st.session, ())
                                else " (module subsystem disabled)")}
        deadline = time.monotonic() + max(0.0, wait_s)
        cmd = None
        while not self._stop.is_set():
            with self._lock:
                cmd = ((self._st.telemetry or {}).get("module") or {}).get("cmd")
                done = bool(cmd and cmd.get("req") == req and cmd.get("result") != "pending")
                if done:
                    self._req.completed("module-cmd", req)
                lost = self._req.state("module-cmd", req) in LOST \
                    or self.counters.connects != connects or self._st.phase != "connected"
            if done:
                return {"ok": cmd["result"] == "ok", "result": cmd["result"], "error": cmd.get("error", ""), "req": req}
            if lost:
                return {"ok": False, "result": "not-connected", "req": req,
                        "error": "link lost before the result (outcome unknown)"}
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        with self._lock:
            self._req.timed_out("module-cmd", req)
        return {"ok": False, "result": "timeout", "req": req,
                "error": f"no result in {wait_s:g} s" + (" (still pending)" if cmd and cmd.get("req") == req else "")}

    def remote_command(self, op: str, wait_s: float = 30.0) -> dict:
        """Обслуживание блока (протокол 1.6, remote-cmd; sheregesh.link.remotecmd): log-export | shutdown | dev-arm.

        Вызывать не из GUI-потока; подтверждение оператора — за вызывающим (shutdown и dev-arm — с confirm: true).
        → {"ok", "result", "error", "req"}, для log-export при ok ещё "name" и "data" (bytes, SHA-256 сверена).
        result: код блока (remotecmd.RESULTS) | not-connected | unsupported (сессия < 1.6) | timeout | bad-reply
        (части журнала не сошлись с итогом)."""
        with self._lock:
            req = self._req.new("remote-cmd")
        body = {"req": req, "op": op, **({"confirm": True} if op in remotecmd.CONFIRMED else {})}
        try:
            remotecmd.check_command(body)
        except remotecmd.BadCommand as e:
            return {"ok": False, "result": "bad-request", "error": e.text, "req": req}
        with self._lock:
            st = self._st
            if st.phase != "connected":
                state = OFFLINE
            elif "remote-cmd" not in TYPES.get(st.session or "", ()):
                state = UNSUPPORTED
            else:
                state = QUEUED
                self._rcmd = {req: {"asm": remotecmd.Assembly(req), "result": None}}   # только последний запрос
                self._outbox.append(("remote-cmd", body))
            self._req.open("remote-cmd", req, state, op=op)
            connects, session = self.counters.connects, st.session
        if state == OFFLINE:
            return {"ok": False, "result": "not-connected", "error": "no link to remote unit", "req": req}
        if state == UNSUPPORTED:
            return {"ok": False, "result": "unsupported", "req": req,
                    "error": f"remote unit does not support maintenance commands (protocol {session})"}
        self._wake()
        deadline = time.monotonic() + max(0.0, wait_s)
        while not self._stop.is_set():
            with self._lock:
                slot = self._rcmd.get(req) or {}
                res = slot.get("result")
                if res is not None:
                    self._req.completed("remote-cmd", req)
                    self._rcmd.pop(req, None)
                lost = self._req.state("remote-cmd", req) in LOST \
                    or self.counters.connects != connects or self._st.phase != "connected"
            if res is not None:
                return self._remote_result(req, op, res, slot["asm"])
            if lost:
                return {"ok": False, "result": "not-connected", "req": req,
                        "error": "link lost before the result (outcome unknown)"}
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        with self._lock:
            self._req.timed_out("remote-cmd", req)
            self._rcmd.pop(req, None)
        return {"ok": False, "result": "timeout", "req": req, "error": f"no result in {wait_s:g} s"}

    @staticmethod
    def _remote_result(req, op, res, asm):
        code = res.get("result") if res.get("result") in remotecmd.RESULTS else "error"
        out = {"ok": code == "ok", "result": code, "error": str(res.get("error") or "")[:200], "req": req}
        if code == "ok" and op == "log-export":
            try:
                out["data"] = asm.finish(res)
            except ValueError as e:
                return {"ok": False, "result": "bad-reply", "error": str(e), "req": req}
            name = str(res.get("name") or "")
            out["name"] = name if re.fullmatch(r"sheregesh-[a-z]+-log-\d{8}-\d{6}\.txt", name) \
                else time.strftime("sheregesh-remote-log-%Y%m%d-%H%M%S.txt")
        return out

    def send_rc(self, body: dict) -> str:
        """Кадр органов управления блоку (протокол 1.4, rc; ADR-0013, раздел 3.1). Можно вызывать из любого потока,
        не ждёт: → "queued" | "offline" (нет соединения) | "unsupported" (сессия < 1.4: старый блок или пульт).

        body = {"en": bool, "ch": [8 целых −1000…1000], "btn": 0…2^24−1, "dev": {"name": str, "ok": bool}}; ключ n,
        если есть, не используется — номер ставит поток канала при отправке. dev.name длиннее NAME_MAX обрезается.
        Неверное тело — sheregesh.control.wire.BadRc (ValueError). Хранится только последний кадр: новый вызов до
        отправки заменяет прежний. Поток канала будится сразу. Отправленное — snapshot()["rc_sent"], ответ блока —
        snapshot()["rc_state"]."""
        if not isinstance(body, dict):
            raise control_wire.BadRc("body is not an object")
        b = {k: v for k, v in body.items() if k != "n"}
        dev = b.get("dev")
        if isinstance(dev, dict) and isinstance(dev.get("name"), str):
            b["dev"] = {**dev, "name": dev["name"][:control_wire.NAME_MAX]}
        if isinstance(b.get("ch"), tuple):
            b["ch"] = list(b["ch"])
        control_wire.check_rc({**b, "n": 1})
        with self._lock:
            if self._st.phase != "connected":
                return "offline"
            if "rc" not in TYPES.get(self._st.session or "", ()):
                return "unsupported"
            self._rc = b
        self._wake()
        return "queued"

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
                    "video_select": self._req.snapshot("video-select"),
                    "module_cmd": self._req.snapshot("module-cmd"),
                    "remote_cmd": self._req.snapshot("remote-cmd"),
                    "rc_state": self._rc_state_snapshot(), "rc_sent": self._rc_sent_snapshot()}

    def _rc_state_snapshot(self):
        """snapshot()["rc_state"]: тело rc-state (sanitize_rc_state) + rx_at (monotonic приёма), rx_age_s, latency_ms
        (приём rc-state − отправка кадра n, мс; None — кадра n нет среди последних RC_RING отправленных). Под _lock."""
        if self._rc_state is None:
            return None
        out = dict(self._rc_state, ch=list(self._rc_state["ch"]), dev=dict(self._rc_state["dev"]))
        out["rx_age_s"] = round(time.monotonic() - out["rx_at"], 3)
        return out

    def _rc_sent_snapshot(self):
        """snapshot()["rc_sent"]: {"n": номер последнего отправленного кадра сеанса (0 — не было), "at": его monotonic
        или None, "pending": кадр ждёт отправки, "ring": [[n, monotonic], …] — последние RC_RING}. Под _lock."""
        return {"n": self._rc_n, "at": self._rc_at, "pending": self._rc is not None,
                "ring": [[n, at] for n, at in self._rc_ring]}

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

    def _wake(self):
        try:
            self._wake_w.send(b"\0")
        except OSError:                                # буфер полон (пробуждение уже ждёт) или клиент остановлен
            pass

    def _wait_readable(self, sock):
        """Ждать данные блока не дольше RX_WAIT_S или пробуждение. True — читать сокет."""
        if self.ctx is not None and sock.pending():     # TLS: расшифрованное уже в буфере, select его не видит
            return True
        r, _, _ = select.select([sock, self._wake_r], [], [], RX_WAIT_S)
        if self._wake_r in r:
            try:
                while self._wake_r.recv(4096):
                    pass
            except OSError:                            # BlockingIOError: всё прочитано
                pass
        return sock in r

    def _send(self, sock, mtype, body):
        with self._send_lock:                        # номер и кадр целиком: записи потоков не перемежаются
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
            h = discovery.discover(self.tls_cfg.discover, self.port, self._tls)
            if h:
                self.hosts = list(dict.fromkeys([h, *self.hosts]))
                discovery.write_cache(self.tls_cfg, h)
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
        self._clock_samples.clear()
        self._rtt_min = None
        if self.video_session is not None:
            self.video_session.on_session_start()
        self._set(phase="handshake", detail="handshake", peer={}, session=None, telemetry=None)
        with self._lock:                                  # кадры rc и их нумерация — заново в каждом соединении
            self._rc, self._rc_n, self._rc_at, self._rc_state = None, 0, None, None
            self._rc_ring.clear()
        dec = Decoder()
        bad_in_row = 0
        rx0 = self.counters.received
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT_S
        established = False
        try:
            sock.settimeout(RX_WAIT_S)
            vs = self.video_session
            hello = {**self.info, **vs.hello_fields()} if vs is not None else self.info
            self._send(sock, "hello", hello)
            while not self._stop.is_set():
                try:
                    data = sock.recv(65536) if self._wait_readable(sock) else None
                    if data == b"":
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
                    data = data or b""                          # None: данных нет (таймаут ожидания или пробуждение)
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
                self._req.lost()                          # неотправленные — offline, отправленные — итог неизвестен
                self._outbox.clear()
                self._rc = None                           # неотправленный кадр устарел: в новое соединение не идёт
            try:
                sock.close()
            except OSError:
                pass
            if self.video_session is not None:
                self.video_session.on_session_end()
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
        elif mtype in ("video-key", "video-stop"):
            if self.video_session is not None:           # блок 1.5 присылает их только после video_transports srtp
                self.video_session.on_message(mtype, body)
            else:
                self.counters.ignored_messages += 1
        elif mtype == "heartbeat":
            if isinstance(msg["ts"], (int, float)):
                self._clock_samples.append(msg["ts"] - time.time())
            self._set(last_rx=time.monotonic())
            self._ack(seq)
        elif mtype == "status":
            tel = _sanitize_status(body)
            self._on_rtt((tel.get("link") or {}).get("rtt_last_ms"))
            self._set(telemetry=tel)
        elif mtype == "rc-state":
            rs = control_wire.sanitize_rc_state(body)
            if rs is None:
                self.counters.ignored_messages += 1
                return
            now = time.monotonic()
            with self._lock:
                sent = next((at for n, at in reversed(self._rc_ring) if n == rs["n"]), None) if rs["n"] else None
                rs.update(rx_at=now, latency_ms=None if sent is None else round((now - sent) * 1000, 1))
                self._rc_state = rs
        elif mtype == "event":
            code = body.get("code")
            if code in CATALOG:
                self._event("REMOTE-EVENT", code=code)
                self._ack(seq)                           # диагностическое подтверждение доставки, не команда
            else:
                self.counters.ignored_messages += 1
        elif mtype in ("remote-log", "remote-cmd-result"):
            with self._lock:
                slot = self._rcmd.get(body.get("req"))
                if slot is None or slot["result"] is not None:
                    self.counters.ignored_messages += 1      # не наш (устаревший) req
                elif mtype == "remote-log":
                    slot["asm"].add(body)
                else:
                    slot["result"] = dict(body)
        elif mtype == "bye":
            self._set(detail="closed")


    def _flush_outbox(self, sock):
        with self._lock:
            out, self._outbox = self._outbox, []
        for mtype, body in out:
            if mtype not in TYPES.get(self._st.session or "", ()):
                continue
            self._send(sock, mtype, body)                # OSError — обрыв: обрабатывает _session
            if mtype in ("video-select", "module-cmd", "remote-cmd"):
                with self._lock:
                    self._req.sent(mtype, body["req"])
        with self._lock:
            rc, self._rc = self._rc, None
            if rc is None or "rc" not in TYPES.get(self._st.session or "", ()):
                return
            self._rc_n += 1
            n, at = self._rc_n, time.monotonic()
            self._rc_at = at
            self._rc_ring.append((n, at))
        self._send(sock, "rc", {"n": n, **rc})           # OSError — обрыв: обрабатывает _session

    def _ack(self, seq):
        if "ack" in TYPES.get(self._st.session or "", ()) and self._sock is not None:
            try:
                self._send(self._sock, "ack", {"seq": seq})
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
            "restarts": num("restarts", int, 10 ** 9), "since_s": num("since_s", float, 10 ** 9),
            # RTSP: заданный транспорт (auto|tcp|udp) и фактический (tcp|udp, "" — неизвестен, напр. при auto)
            "transport_mode": v["transport_mode"] if v.get("transport_mode") in ("auto", "tcp", "udp") else "",
            "transport": v["transport"] if v.get("transport") in ("tcp", "udp") else ""}


def _sanitize_status(body):
    """Только ожидаемые поля и типы; остальное отбрасывается (данные второй стороны недоверенные)."""
    out = {}
    temps = body.get("temperatures")
    if isinstance(temps, dict):
        out["temperatures"] = {str(k)[:16]: float(v) for k, v in list(temps.items())[:16]
                               if _number(v) and -100.0 <= v <= 300.0}
    p = body.get("power")
    if isinstance(p, dict) and _number(p.get("percent")):
        out["power"] = {"source": str(p.get("source", ""))[:16], "percent": min(100.0, max(0.0, float(p["percent"]))),
                        "low_battery": bool(p.get("low_battery"))}
    v = body.get("video")
    if isinstance(v, dict):
        out["video"] = {"enabled": bool(v.get("enabled")), "active": bool(v.get("active")),
                        "codec": v.get("codec") if v.get("codec") in ("h264", "jpeg") else None,
                        "port": int(v["port"]) if isinstance(v.get("port"), int) and 1024 <= v["port"] <= 65535 else None,
                        "transport": v["transport"] if v.get("transport") == "srtp" else None}
        src = _sanitize_video_source(v.get("source"))
        if src is not None:                       # видеоприём блока: необязательное поле (протокол 1.1 без изменений)
            out["video"]["source"] = src
        sources = _sanitize_video_sources(v.get("sources"))
        if sources is not None:                   # список источников блока (S4): необязательное поле
            out["video"]["sources"] = sources
        sel = _sanitize_video_select(v.get("select"))
        if sel is not None:                       # результат последнего video-select (протокол 1.2)
            out["video"]["select"] = sel
    m = wire.sanitize_module(body.get("module"))
    if m is not None:                             # внешний модуль блока (протокол 1.3, ADR-0012): необязательное поле
        out["module"] = m
    from .. import services as svc
    s = svc.sanitize(body.get("services"))
    if s is not None:                             # обязательные службы блока (REQ-SW-001): необязательное поле
        out["services"] = s
    ev = body.get("active_events")
    out["active_events"] = [str(c)[:32] for c in ev if isinstance(c, str)][:20] if isinstance(ev, list) else []
    if isinstance(body.get("uptime_s"), (int, float)):
        out["uptime_s"] = float(body["uptime_s"])
    out["state"] = str(body.get("state", ""))[:16]
    link = body.get("link")
    if isinstance(link, dict):
        out["link"] = {str(k)[:24]: float(v) for k, v in list(link.items())[:16] if _number(v)}
    return out


def _number(v):
    """Конечное число, не bool: 1e300, inf и nan из недоверенного status не доходят до интерфейса."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


class RemotePeer:
    """Адаптер PeerClient к интерфейсу hal.Peer в шкале часов монитора."""

    def __init__(self, client: PeerClient, clock):
        self.client, self.clock = client, clock

    def status(self):
        return self.client.status_for(self.clock())

    def drain_link_events(self):
        return self.client.drain_link_events()
