# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Защищённый видеоканал удалённый блок → пульт (S3.1-VIDEO, ADR-0011).

Блок:  videotestsrc → H.264 → rtph264pay → rtpstreampay → stdout ─┐
       этот модуль: отдельное соединение TLS 1.3 с взаимной аутентификацией (порт video_port, те же CA, сертификаты
       и ожидаемая идентичность пульта, что у канала управления) ─→ пульт
Пульт: этот модуль → stdin → rtpstreamdepay → rtpjitterbuffer → декодер → RGBx (sheregesh_console.video).

RTP не попадает ни в один сокет: только каналы процессов на каждой плате и TLS между ними. Своей криптографии нет —
модуль ssl (TLS 1.3, AEAD): подложные или изменённые байты рвут соединение, ключи сеанса эфемерны (ECDHE) и не
покидают процесс. Видеосеанс принимается только от того же пульта (URI сертификата) и того же адреса, что текущий
сеанс управления, и закрывается, когда сеанс управления кончился (VideoServer.drop). Резервного UDP нет.

Кадр по TLS: тип (1 байт) + длина (4 байта) + данные.
  INFO  JSON с кодеком и параметрами источника            (блок → пульт)
  RTP   8 байт времени отправки (time.time(), double) + RTP-пакет   (блок → пульт)
  PING  8 байт t0 пульта; PONG — t0 + t1 блока: оценка смещения часов для задержки (как в NTP)
"""
import json
import os
import select
import signal
import socket
import ssl
import struct
import subprocess
import threading
import time
from collections import deque

from . import video
from .link import tls

HDR = struct.Struct("!BI")
TS = struct.Struct("!d")
T_INFO, T_RTP, T_PING, T_PONG = 1, 2, 3, 4
MAX_PAYLOAD = 256 * 1024
SEND_TIMEOUT_S = 5.0
ACCEPT_TIMEOUT_S = tls.HANDSHAKE_TIMEOUT_S


class VideoLinkError(Exception):
    pass


def frame(kind, payload=b""):
    return HDR.pack(kind, len(payload)) + payload


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise VideoLinkError("соединение закрыто")
        buf += chunk
    return bytes(buf)


def read_frame(sock):
    kind, n = HDR.unpack(recv_exact(sock, HDR.size))
    if n > MAX_PAYLOAD or kind not in (T_INFO, T_RTP, T_PING, T_PONG):
        raise VideoLinkError(f"неверный кадр видеоканала: тип {kind}, {n} байт")
    return kind, recv_exact(sock, n) if n else b""


class Rfc4571:
    """Разбор потока «2 байта длины + RTP-пакет» из канала GStreamer (rtpstreampay)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= 2:
            n = int.from_bytes(self.buf[:2], "big")
            if len(self.buf) < 2 + n:
                break
            out.append(bytes(self.buf[2:2 + n]))
            del self.buf[:2 + n]
        return out


def rtp_info(pkt):
    """(номер последовательности, маркер конца кадра, полезная нагрузка) или None для не-RTP."""
    if len(pkt) < 12 or pkt[0] >> 6 != 2:
        return None
    cc, ext = pkt[0] & 0x0F, pkt[0] & 0x10
    off = 12 + 4 * cc
    if ext and len(pkt) >= off + 4:
        off += 4 + 4 * int.from_bytes(pkt[off + 2:off + 4], "big")
    return int.from_bytes(pkt[2:4], "big"), bool(pkt[1] & 0x80), pkt[off:]


# ---------------------------------------------------------------- SPS: фактическое разрешение потока
class _Bits:
    def __init__(self, data):
        self.d, self.i = data, 0

    def u(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | ((self.d[self.i >> 3] >> (7 - (self.i & 7))) & 1)
            self.i += 1
        return v

    def ue(self):
        z = 0
        while self.u(1) == 0:
            z += 1
        return (1 << z) - 1 + self.u(z)

    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


def _unescape(nal):
    out, zeros = bytearray(), 0
    for b in nal:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def parse_sps(nal):
    """Ширина, высота и профиль из SPS H.264 (NAL type 7). Пробрасывает IndexError на усечённых данных."""
    b = _Bits(_unescape(nal[1:]))
    profile = b.u(8)
    b.u(16)
    b.ue()
    chroma = 1
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma = b.ue()
        if chroma == 3:
            b.u(1)
        b.ue(), b.ue(), b.u(1)
        if b.u(1):
            for i in range(8 if chroma != 3 else 12):
                if b.u(1):
                    last = nxt = 8
                    for _ in range(16 if i < 6 else 64):
                        if nxt:
                            nxt = (last + b.se() + 256) % 256
                        last = nxt or last
    b.ue()
    poc = b.ue()
    if poc == 0:
        b.ue()
    elif poc == 1:
        b.u(1), b.se(), b.se()
        for _ in range(b.ue()):
            b.se()
    b.ue(), b.u(1)
    w_mbs, h_map = b.ue() + 1, b.ue() + 1
    frame_mbs_only = b.u(1)
    if not frame_mbs_only:
        b.u(1)
    b.u(1)
    crop = (0, 0, 0, 0)
    if b.u(1):
        crop = (b.ue(), b.ue(), b.ue(), b.ue())
    cx, cy = (2, 2 * (2 - frame_mbs_only)) if chroma == 1 else (1, 2 - frame_mbs_only)
    width = w_mbs * 16 - cx * (crop[0] + crop[1])
    height = (2 - frame_mbs_only) * h_map * 16 - cy * (crop[2] + crop[3])
    return {"width": width, "height": height, "profile_idc": profile}


def sps_from_rtp(payload):
    """NAL SPS из полезной нагрузки RTP H.264: одиночный NAL (7) или STAP-A (24)."""
    if not payload:
        return None
    t = payload[0] & 0x1F
    if t == 7:
        return payload
    if t == 24:
        i = 1
        while i + 2 <= len(payload):
            n = int.from_bytes(payload[i:i + 2], "big")
            nal = payload[i + 2:i + 2 + n]
            if nal and nal[0] & 0x1F == 7:
                return nal
            i += 2 + n
    return None


def _pct(values, q):
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))], 2)


# ---------------------------------------------------------------- блок: сервер
class VideoServer:
    """Видеосервер блока: TLS 1.3 + взаимная аутентификация, один сеанс, GStreamer только пока есть сеанс.

    authorize(addr) → None (разрешено) или причина отказа: привязка к текущему сеансу управления.
    """

    def __init__(self, cfg, codec, authorize, log, bind="0.0.0.0", port=None, command=None,
                 width=1280, height=720, fps=30):
        self.cfg, self.codec, self.authorize, self.log = cfg, codec, authorize, log
        self.bind, self.port = bind, port if port is not None else cfg.video_port
        self.command = command or video.stream_sender_args(codec, width, height, fps)
        self.params = {"codec": codec, "width": width, "height": height, "fps": fps}
        self.ctx = tls.make_context(cfg, server=True)
        self.lock = threading.Lock()
        self.session = None
        self.stop_ev = threading.Event()
        self.st = {"sessions": 0, "auth_failures": 0, "rejected": 0, "dropped_by_control": 0, "packets": 0,
                   "bytes": 0, "frames": 0, "pings": 0, "sender_restarts": 0}
        self.frame_times = deque(maxlen=300)
        self.last_reason = None

    def start(self):
        self.lsock = socket.create_server((self.bind, self.port), reuse_port=False)
        self.port = self.lsock.getsockname()[1]
        threading.Thread(target=self._accept_loop, name="video-accept", daemon=True).start()
        return self

    def close(self):
        self.stop_ev.set()
        # Linux: close() слушающего сокета не прерывает accept() в другом потоке — сокет остаётся в LISTEN, пока
        # accept не вернёт следующее соединение, и остановленный сервер успел бы принять ещё один видеосеанс.
        # shutdown() будит accept (EINVAL); на macOS для слушающего сокета он не нужен и даёт ENOTCONN.
        try:
            self.lsock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.lsock.close()
        except OSError:
            pass
        self.drop("служба останавливается")

    @property
    def active(self):
        return self.session is not None

    def drop(self, reason):
        """Закрыть текущий видеосеанс (сеанс управления кончился, остановка)."""
        with self.lock:
            s, self.session = self.session, None
        if s is not None:
            self.st["dropped_by_control"] += reason.startswith("сеанс управления")
            self.log(f"video: session {s.addr} closed: {reason}")
            # не блокировать вызывающего (цикл asyncio блока: heartbeat не должен ждать остановки GStreamer)
            threading.Thread(target=s.close, name="video-close", daemon=True).start()

    def stats(self):
        now = time.monotonic()
        recent = [t for t in self.frame_times if now - t <= 5.0]
        s = self.session
        return {**self.st, "active": s is not None, "peer": s.addr if s else None, "port": self.port,
                "transport": "tls1.3-mutual", **self.params,
                "sender_fps_5s": round(len(recent) / 5.0, 1) if recent else 0.0, "last_reject": self.last_reason}

    def _accept_loop(self):
        while not self.stop_ev.is_set():
            try:
                raw, (addr, *_) = self.lsock.accept()
            except OSError:
                return
            if self.stop_ev.is_set():                    # принято в момент остановки — не обслуживать
                raw.close()
                return
            threading.Thread(target=self._admit, args=(raw, addr), daemon=True).start()

    def _reject(self, raw, addr, why, auth=True):
        self.st["auth_failures" if auth else "rejected"] += 1
        self.last_reason = f"{addr}: {why}"
        self.log(f"video auth: reject {addr}: {why}")
        try:
            raw.close()
        except OSError:
            pass

    def _admit(self, raw, addr):
        if not tls.peer_allowed(addr):
            return self._reject(raw, addr, "outside lab network", auth=False)
        raw.settimeout(ACCEPT_TIMEOUT_S)
        try:
            sock = self.ctx.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError) as e:
            return self._reject(raw, addr, f"TLS handshake failed: {tls.describe_ssl_error(e)}")
        why = tls.check_peer(sock.getpeercert(), self.cfg.peer_uri)
        if why:
            return self._reject(sock, addr, why)
        why = self.authorize(addr)
        if why:                                        # подлинный пульт, но нет текущего сеанса управления
            return self._reject(sock, addr, why)
        sess = _ServerSession(self, sock, addr)
        with self.lock:
            old, self.session = self.session, sess
        if old is not None:
            old.close()
        self.st["sessions"] += 1
        self.log(f"video: session from {addr}: {self.cfg.peer_uri} ({sock.version()}) {self.params}")
        sess.run()
        with self.lock:
            if self.session is sess:
                self.session = None


class _ServerSession:
    def __init__(self, server, sock, addr):
        self.srv, self.sock, self.addr = server, sock, addr
        self.proc = None
        self.closed = threading.Event()

    def close(self):
        self.closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self._kill()

    def _kill(self):
        p, self.proc = self.proc, None
        if p is not None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                p.wait(timeout=3)
            except Exception:                            # noqa: BLE001 — процесс уже снят killpg
                pass

    def run(self):
        srv = self.srv
        try:
            self.sock.settimeout(SEND_TIMEOUT_S)
            self.sock.sendall(frame(T_INFO, json.dumps(srv.params).encode()))
            self.proc = video.spawn(srv.command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            pipe = self.proc.stdout.fileno()
            parser = Rfc4571()
            while not self.closed.is_set():
                ready = [self.sock] if self.sock.pending() else []
                if not ready:
                    r, _, _ = select.select([self.sock, pipe], [], [], 0.5)
                    ready = r
                if pipe in ready:
                    data = os.read(pipe, 65536)
                    if not data:
                        raise VideoLinkError("генератор видео завершился")
                    now = time.time()
                    out = bytearray()
                    for pkt in parser.feed(data):
                        info = rtp_info(pkt)
                        if info is None:
                            continue
                        if info[1]:
                            srv.st["frames"] += 1
                            srv.frame_times.append(time.monotonic())
                        srv.st["packets"] += 1
                        srv.st["bytes"] += len(pkt)
                        out += frame(T_RTP, TS.pack(now) + pkt)
                    if out:
                        self.sock.sendall(out)
                if self.sock in ready:
                    kind, payload = read_frame(self.sock)
                    if kind == T_PING and len(payload) == TS.size:
                        srv.st["pings"] += 1
                        self.sock.sendall(frame(T_PONG, payload + TS.pack(time.time())))
        except (VideoLinkError, ssl.SSLError, OSError) as e:
            if not self.closed.is_set():
                srv.log(f"video: session {self.addr} ended: {e}")
        finally:
            self.close()


# ---------------------------------------------------------------- пульт: клиент
class VideoClient:
    """Клиент видеоканала пульта: TLS к блоку, проверка его идентичности, RTP → stdin декодера."""

    def __init__(self, cfg, host, port=None, log=lambda m: None):
        self.cfg, self.host, self.port, self.log = cfg, host, port or cfg.video_port, log
        self.ctx = tls.make_context(cfg, server=False)
        self.sock = None
        self.stop_ev = threading.Event()
        self.info = None
        self.st = {"connected": False, "packets": 0, "bytes": 0, "frames": 0, "seq_gaps": 0, "lost_packets": 0,
                   "bad_rtp": 0, "tls": None, "peer_uri": None, "error": None}
        self.sps = None
        self.latency = deque(maxlen=600)              # мс: отправка блоком → приём пультом (по маркерам кадров)
        self.offsets = deque(maxlen=20)               # (rtt, смещение часов блок−пульт)
        self.frame_times = deque(maxlen=300)
        self._last_seq = None
        # Задержка приёмной части на живом потоке, по одним часам: момент записи последнего пакета кадра в stdin
        # декодера → момент выхода кадра RGBx (frame_decoded). Учёт — после первого SPS (с ним приходит IDR):
        # кадры до ключевого декодер не выдаёт.
        self.written = deque(maxlen=120)
        self.decode_latency = deque(maxlen=600)
        self.decode_desync = 0

    def frame_decoded(self, t):
        """Вызывает читатель декодера на каждый готовый кадр (тот же процесс, time.monotonic)."""
        if self.written:
            self.decode_latency.append((t - self.written.popleft()) * 1000)
        if len(self.written) > 60:                        # декодер отстал/выбросил кадры — очередь не сопоставима
            self.decode_desync += 1
            self.written.clear()

    def key(self):
        return (self.host, self.port)

    def close(self):
        self.stop_ev.set()
        s = self.sock
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    def offset(self):
        if not self.offsets:
            return None
        return min(self.offsets)[1]                   # по пингу с наименьшим RTT

    def stats(self):
        now = time.monotonic()
        recent = [t for t in self.frame_times if now - t <= 5.0]
        lat = list(self.latency)
        best = min(self.offsets) if self.offsets else None
        return {**self.st, "host": self.host, "port": self.port, "source": self.info, "stream_sps": self.sps,
                "received_fps_5s": round(len(recent) / 5.0, 1) if recent else 0.0,
                "transport_latency_ms": {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95), "max": round(max(lat), 2)
                                         if lat else None, "samples": len(lat)},
                "ping_rtt_ms": round(best[0] * 1000, 2) if best else None,
                "decode_latency_ms": {"p50": _pct(list(self.decode_latency), 0.5),
                                      "p95": _pct(list(self.decode_latency), 0.95), "samples": len(self.decode_latency),
                                      "desync_resets": self.decode_desync}}

    def connect(self):
        raw = socket.create_connection((self.host, self.port), timeout=ACCEPT_TIMEOUT_S)
        try:
            sock = self.ctx.wrap_socket(raw, server_side=False)
        except (ssl.SSLError, OSError) as e:
            raw.close()
            raise VideoLinkError(f"auth: {tls.describe_ssl_error(e)}") from None
        why = tls.check_peer(sock.getpeercert(), self.cfg.peer_uri)
        if why:
            sock.close()
            raise VideoLinkError(f"auth: {why}")
        self.sock = sock
        self.st.update(connected=True, tls=sock.version(), peer_uri=self.cfg.peer_uri, error=None)
        return sock

    def run(self, out_fd):
        """Читать видеоканал и писать RTP (RFC 4571) в out_fd до разрыва. Возвращает причину завершения."""
        try:
            sock = self.sock or self.connect()
            sock.settimeout(SEND_TIMEOUT_S)
            next_ping = 0.0
            while not self.stop_ev.is_set():
                now_m = time.monotonic()
                if now_m >= next_ping:
                    sock.sendall(frame(T_PING, TS.pack(time.time())))
                    next_ping = now_m + 1.0
                if not sock.pending():
                    r, _, _ = select.select([sock], [], [], 0.5)
                    if not r:
                        continue
                kind, payload = read_frame(sock)
                if kind == T_RTP and len(payload) > TS.size:
                    self._on_rtp(TS.unpack(payload[:TS.size])[0], payload[TS.size:], out_fd)
                elif kind == T_PONG and len(payload) == 2 * TS.size:
                    t0, t1 = TS.unpack(payload[:8])[0], TS.unpack(payload[8:])[0]
                    t2 = time.time()
                    self.offsets.append((t2 - t0, t1 - (t0 + t2) / 2))
                elif kind == T_INFO:
                    self.info = json.loads(payload)
            return "остановлен"
        except (VideoLinkError, ssl.SSLError, OSError, ValueError) as e:
            self.st["error"] = str(e)[:200]
            return str(e)
        finally:
            self.st["connected"] = False
            self.close()

    def _on_rtp(self, sent, pkt, out_fd):
        info = rtp_info(pkt)
        if info is None:
            self.st["bad_rtp"] += 1
            return
        seq, marker, payload = info
        if self._last_seq is not None and seq != (self._last_seq + 1) & 0xFFFF:
            self.st["seq_gaps"] += 1
            self.st["lost_packets"] += (seq - self._last_seq - 1) & 0xFFFF
        self._last_seq = seq
        self.st["packets"] += 1
        self.st["bytes"] += len(pkt)
        if self.sps is None:
            nal = sps_from_rtp(payload)
            if nal is not None:
                try:
                    self.sps = parse_sps(nal)
                except IndexError:
                    pass
        os.write(out_fd, len(pkt).to_bytes(2, "big") + pkt)
        if marker:
            self.st["frames"] += 1
            self.frame_times.append(time.monotonic())
            if self.sps is not None:
                self.written.append(time.monotonic())
            off = self.offset()
            if off is not None:
                self.latency.append((time.time() - (sent - off)) * 1000)
