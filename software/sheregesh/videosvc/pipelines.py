# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Конвейеры SRTP TX и RX на python3-gi (ADR-0016, контракт docs/architecture/VIDEO-SERVICE-CONTRACT.md, трек A).

Python только строит конвейер, задаёт свойства и читает статистику; ни один RTP-пакет через него не проходит.
Единственный пробник на кадр — на выходе декодера RX (счёт кадров). Остальные пробники однократные
(первый буфер TX) либо на события (не на пакеты).

Структура:
  TX:  media -> rtph264pay -> rtpbin(AVPF) -> srtpenc -> udpsink;  RTCP: rtpbin -> srtpenc -> udpsink,
       udpsrc -> rtpbin (srtpdec внутри). PLI от приёмника превращается rtpbin в GstForceKeyUnit вверх по
       конвейеру, до кодера.
  RX:  udpsrc -> rtpbin (srtpdec, джиттер-буфер внутри rtpbin, latency=jitter_latency_ms) -> rtph264depay
       (request-keyframe) -> h264parse -> декодер -> приёмник кадров.
Шифрование подключено через сигналы rtpbin request-rtp-encoder/-decoder и request-rtcp-encoder/-decoder
(отдельный srtpenc/srtpdec для RTP и для RTCP). Ключ задаётся свойством (Gst.Buffer) или
возвращается сигналом request-key, и только для своего SSRC. В строке описания, журнале, repr и статистике ключа нет.

Обратный канал (RTCP приёмника) идёт под тем же мастер-ключом, но со своим SSRC: rtcp_ssrc_for(ssrc) — детерминированная
функция от SSRC потока, обе стороны вычисляют её независимо.

Командная строка (для системных тестов, интерпретатор с gi): python3 -m sheregesh.videosvc.pipelines selftest <сценарий>
"""
from __future__ import annotations

import base64
import collections
import itertools
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

# Длина мастер-ключа с солью по шифру (контракт §5). null запрещён.
KEY_LENGTHS = {"aes-128-gcm": 28, "aes-128-icm": 30}
# Соответствие шифра и аутентификации: GCM аутентифицирует сам, ICM — hmac-sha1-80.
CIPHER_AUTH = {"aes-128-gcm": "null", "aes-128-icm": "hmac-sha1-80"}
PAYLOAD_TYPE = 96
_seq = itertools.count()
CLOCK_RATE = 90000
_STREAM_RE = re.compile(r"^[a-z0-9-]{1,16}$")
_SRC_RE = re.compile(r"^[A-Za-z0-9_.:/=,! \-]+$")


@dataclass(frozen=True)
class SrtpParams:
    """Параметры SRTP: шифр и мастер-ключ с солью. Ключ не попадает в repr."""
    cipher: str
    key: bytes = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.cipher, str) or self.cipher not in KEY_LENGTHS:
            raise ValueError(f"недопустимый шифр SRTP: {self.cipher!r} (разрешены {sorted(KEY_LENGTHS)})")
        if not isinstance(self.key, (bytes, bytearray)):
            raise ValueError("ключ SRTP должен быть bytes")
        if len(self.key) != KEY_LENGTHS[self.cipher]:
            raise ValueError(f"длина ключа {self.cipher}: нужно {KEY_LENGTHS[self.cipher]} байт, получено {len(self.key)}")
        object.__setattr__(self, "key", bytes(self.key))

    @classmethod
    def from_b64(cls, cipher: str, key_b64: str) -> "SrtpParams":
        try:
            raw = base64.b64decode(key_b64, validate=True)
        except (ValueError, TypeError) as e:
            raise ValueError("key_b64 не base64") from e
        return cls(cipher, raw)


def rtcp_ssrc_for(ssrc: int) -> int:
    """SSRC обратного RTCP приёмника: побитовая инверсия SSRC потока (если вышел 0 — 1). Детерминированно на обеих сторонах."""
    r = ssrc ^ 0xFFFFFFFF
    return r or 1


def _check_ssrc(ssrc) -> None:
    if not isinstance(ssrc, int) or isinstance(ssrc, bool) or not (1 <= ssrc <= 0xFFFFFFFF):
        raise ValueError(f"ssrc вне 1..2^32-1: {ssrc!r}")
    if ssrc == 0xFFFFFFFF:
        raise ValueError("ssrc 0xFFFFFFFF зарезервирован (его инверсия давала бы SSRC 0, подставляется 1)")


def _check_port(name, port) -> None:
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise ValueError(f"{name}: порт вне 1..65535: {port!r}")


def _check_ip(ip) -> None:
    import ipaddress
    if not isinstance(ip, str):
        raise ValueError(f"peer_ip должен быть строкой: {ip!r}")
    try:
        ipaddress.ip_address(ip)
    except (ValueError, TypeError) as e:
        raise ValueError(f"peer_ip не адрес IP: {ip!r}") from e


def _check_fragment(name, text) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{name}: пустой фрагмент конвейера")
    if "\n" in text or "\x00" in text:
        raise ValueError(f"{name}: недопустимые символы")


def _check_stream(stream) -> None:
    if not isinstance(stream, str) or not _STREAM_RE.fullmatch(stream):
        raise ValueError(f"stream должен соответствовать [a-z0-9-]{{1,16}}: {stream!r}")


@dataclass(frozen=True)
class TxConfig:
    stream: str
    ssrc: int
    peer_ip: str
    peer_port: int
    rtcp_local_port: int      # где TX слушает RTCP приёмника
    rtcp_peer_port: int       # куда TX шлёт RTCP (SR) на приёмнике
    media: str                # фрагмент gst-launch, выдающий video/x-h264,stream-format=byte-stream,alignment=au
    srtp: SrtpParams
    mtu: int = 1200

    def __post_init__(self):
        _check_stream(self.stream)
        _check_ssrc(self.ssrc)
        _check_ip(self.peer_ip)
        for n in ("peer_port", "rtcp_local_port", "rtcp_peer_port"):
            _check_port(n, getattr(self, n))
        _check_fragment("media", self.media)
        if not isinstance(self.srtp, SrtpParams):
            raise ValueError("srtp: нужен SrtpParams")
        if not isinstance(self.mtu, int) or not (200 <= self.mtu <= 65000):
            raise ValueError(f"mtu вне 200..65000: {self.mtu!r}")


@dataclass(frozen=True)
class RxConfig:
    stream: str
    ssrc: int
    peer_ip: str
    local_port: int           # куда приходит RTP
    rtcp_local_port: int      # где RX слушает RTCP передатчика
    rtcp_peer_port: int       # куда RX шлёт RTCP (PLI, RR) на передатчике
    decoder: str              # имя элемента: avdec_h264, v4l2h264dec
    sink: str                 # фрагмент приёмника кадров: unixfdsink socket-path=..., fakesink
    srtp: SrtpParams
    jitter_latency_ms: int = 20

    def __post_init__(self):
        _check_stream(self.stream)
        _check_ssrc(self.ssrc)
        _check_ip(self.peer_ip)
        for n in ("local_port", "rtcp_local_port", "rtcp_peer_port"):
            _check_port(n, getattr(self, n))
        if not isinstance(self.decoder, str) or not re.fullmatch(r"[a-z0-9_]+", self.decoder):
            raise ValueError(f"decoder: нужно имя элемента GStreamer: {self.decoder!r}")
        _check_fragment("sink", self.sink)
        if not isinstance(self.srtp, SrtpParams):
            raise ValueError("srtp: нужен SrtpParams")
        if not isinstance(self.jitter_latency_ms, int) or isinstance(self.jitter_latency_ms, bool) or not (0 <= self.jitter_latency_ms <= 2000):
            raise ValueError(f"jitter_latency_ms вне 0..2000: {self.jitter_latency_ms!r}")


def _srtp_caps_fields(cipher: str) -> str:
    """Шифры в caps для udpsrc (без ключа): нужны srtpdec, когда ключ приходит сигналом request-key."""
    auth = CIPHER_AUTH[cipher]
    return f"srtp-cipher={cipher},srtp-auth={auth},srtcp-cipher={cipher},srtcp-auth={auth}"


def _enc_fragment(cipher: str) -> str:
    """srtpenc с шифром и аутентификацией, без ключа (ключ задаётся свойством после сборки)."""
    auth = CIPHER_AUTH[cipher]
    return f"srtpenc name=senc rtp-cipher={cipher} rtcp-cipher={cipher} rtp-auth={auth} rtcp-auth={auth}"


def build_tx_description(cfg: TxConfig) -> str:
    """Строка описания TX без ключа. Именованные элементы: pay, payout, rtpbin, rtpout, rtcpout, rtcpin.

    Выводы rtpbin, srtpenc и srtpdec в строке не соединены: площадки запрашиваются и соединяются в Python (см. _build).
    """
    rtp_caps = (f"application/x-rtp,media=video,encoding-name=H264,payload={PAYLOAD_TYPE},"
                f"clock-rate={CLOCK_RATE},rtcp-fb-nack-pli=true")
    return (
        f"rtpbin name=rtpbin rtp-profile=avpf "
        f"{cfg.media} ! queue name=mediaq max-size-buffers=4 leaky=downstream ! "
        f"rtph264pay name=pay pt={PAYLOAD_TYPE} config-interval=1 mtu={cfg.mtu} ssrc={cfg.ssrc} ! "
        f"capsfilter name=payout caps=\"{rtp_caps}\" "
        f"{_enc_fragment(cfg.srtp.cipher)} srtpdec name=sdec "
        f"udpsink name=rtpout host={cfg.peer_ip} port={cfg.peer_port} sync=false async=false "
        f"udpsink name=rtcpout host={cfg.peer_ip} port={cfg.rtcp_peer_port} sync=false async=false "
        f"udpsrc name=rtcpin address={_bind_addr(cfg.peer_ip)} port={cfg.rtcp_local_port} "
        f"caps=application/x-srtcp"
    )


def build_rx_description(cfg: RxConfig) -> str:
    """Строка описания RX без ключа. Именованные элементы: rtpin, rtcpin, rtcpout, rtpbin, depay, dec."""
    in_caps = (f"application/x-srtp,media=video,encoding-name=H264,payload={PAYLOAD_TYPE},clock-rate={CLOCK_RATE},"
               f"rtcp-fb-nack-pli=true,{_srtp_caps_fields(cfg.srtp.cipher)}")
    return (
        f"rtpbin name=rtpbin rtp-profile=avpf latency={cfg.jitter_latency_ms} do-lost=true "
        f"{_enc_fragment(cfg.srtp.cipher)} srtpdec name=sdec "
        f"udpsrc name=rtpin address={_bind_addr(cfg.peer_ip)} port={cfg.local_port} caps=\"{in_caps}\" "
        f"udpsrc name=rtcpin address={_bind_addr(cfg.peer_ip)} port={cfg.rtcp_local_port} "
        f"caps=application/x-srtcp "
        f"udpsink name=rtcpout host={cfg.peer_ip} port={cfg.rtcp_peer_port} sync=false async=false "
        f"rtph264depay name=depay request-keyframe=true ! h264parse name=parse ! {cfg.decoder} name=dec ! "
        f"{cfg.sink}"
    )


def _bind_addr(peer_ip: str) -> str:
    """Адрес привязки приёмных сокетов: loopback остаётся loopback, иначе все интерфейсы (фильтр — nftables, контракт §8)."""
    import ipaddress
    ip = ipaddress.ip_address(peer_ip)
    if ip.is_loopback:
        return str(ip)
    return "::" if ip.version == 6 else "0.0.0.0"


# ---------------------------------------------------------------------------------------------------------------
# Часть с GStreamer. Импорт gi — лениво, чтобы unit-тесты без gi работали.
# ---------------------------------------------------------------------------------------------------------------
_gst_lock = threading.Lock()
_GST = None


def gst():
    """Возвращает (Gst, GLib, GObject), инициализируя GStreamer один раз."""
    global _GST
    with _gst_lock:
        if _GST is None:
            import gi
            gi.require_version("Gst", "1.0")
            gi.require_version("GstRtp", "1.0")
            from gi.repository import GLib, GObject, Gst, GstRtp
            Gst.init(None)
            _GST = (Gst, GLib, GObject, GstRtp)
        return _GST[:3]


def gi_available() -> bool:
    try:
        gst()
        return True
    except Exception:
        return False


EventCb = Callable[[str, dict], None]


class _Base:
    """Общая часть TX/RX: сборка, цикл шины в потоке, остановка, события."""

    role = "base"

    def __init__(self, cfg, on_event: Optional[EventCb] = None):
        self.cfg = cfg
        self.on_event = on_event
        self._pipe = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._started = False
        self._rtpbin = None
        self.t_start = 0.0
        self.counters = {"unknown_ssrc": 0, "pli_sent": 0, "pli_received": 0, "keyframes_requested": 0}
        self.errors: list[str] = []

    # ---- события ----
    def _emit(self, kind: str, **detail) -> None:
        cb = self.on_event
        if cb is None:
            return
        try:
            cb(kind, detail)
        except Exception:  # колбэк пользователя не должен ронять потоки GStreamer
            pass

    # ---- жизненный цикл ----
    def _build(self):  # pragma: no cover - переопределяется
        raise NotImplementedError

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            Gst, GLib, GObject = gst()
            self._stop.clear()
            self._pipe = self._build()
            self.t_start = time.monotonic()
            ret = self._pipe.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                self._pipe.set_state(Gst.State.NULL)
                self._pipe = None
                raise RuntimeError("конвейер не перешёл в PLAYING")
            self._started = True
            self._thread = threading.Thread(target=self._bus_loop, name=f"videosvc-{self.role}-{self.cfg.stream}",
                                            daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
            self._stop.set()
            pipe, th = self._pipe, self._thread
            self._pipe = None
            self._thread = None
        Gst, _, _ = gst()
        if pipe is not None:
            pipe.set_state(Gst.State.NULL)
            pipe.get_state(3 * Gst.SECOND)
        if th is not None and th is not threading.current_thread():
            th.join(timeout=5)
        self._rtpbin = None

    def _bus_loop(self) -> None:
        # Опрос шины в собственном потоке вместо GLib.MainLoop: у каждого объекта своя шина, общий default-контекст
        # GLib двумя циклами из разных потоков не делится.
        Gst, _, _ = gst()
        pipe = self._pipe
        bus = pipe.get_bus()
        mask = Gst.MessageType.ERROR | Gst.MessageType.EOS
        while not self._stop.is_set():
            msg = bus.timed_pop_filtered(200 * Gst.MSECOND, mask)
            if msg is None:
                continue
            if msg.type == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                text = err.message
                self.errors.append(text)
                self._emit("error", message=text, source=msg.src.get_name() if msg.src else "")
                return
            if msg.type == Gst.MessageType.EOS:
                self._emit("eos")
                return

    # ---- SRTP ----
    def _setup_srtp(self, pipe, accept_ssrcs) -> None:
        """Ключ в srtpenc — свойством (Gst.Buffer); в srtpdec — ответом на сигнал request-key и только для своих SSRC."""
        Gst, _, _ = gst()
        p = self.cfg.srtp
        enc = pipe.get_by_name("senc")
        if enc is not None:
            enc.set_property("key", Gst.Buffer.new_wrapped(p.key))
        dec = pipe.get_by_name("sdec")
        if dec is None:
            return
        key = Gst.Buffer.new_wrapped(p.key)
        auth = CIPHER_AUTH[p.cipher]

        def request_key(_el, ssrc):
            if ssrc not in accept_ssrcs:  # чужой SSRC: ключа нет, srtpdec сбрасывает пакеты
                self.counters["unknown_ssrc"] += 1
                return None
            # Структура собирается до caps: в привязках GStreamer 1.26 (платы) caps.get_structure() отдаёт копию, и поля,
            # заданные через неё, в caps не попадали (srtpdec: «Could not set stream with SSRC», все пакеты сброшены).
            s = Gst.Structure.new_empty("application/x-srtp-key")
            s.set_value("srtp-key", key)
            for name, val in (("srtp-cipher", p.cipher), ("srtp-auth", auth),
                              ("srtcp-cipher", p.cipher), ("srtcp-auth", auth)):
                s.set_value(name, val)
            caps = Gst.Caps.new_empty()
            caps.append_structure(s)
            return caps

        dec.connect("request-key", request_key)

    @staticmethod
    def _req(el, name):
        pad = el.request_pad_simple(name)
        if pad is None:
            raise RuntimeError(f"{el.get_name()}: нет площадки {name}")
        return pad

    @staticmethod
    def _plink(src_pad, sink_pad) -> None:
        Gst, _, _ = gst()
        if src_pad.link(sink_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"не удалось соединить {src_pad.get_name()} с {sink_pad.get_name()}")

    def _link_srtp_out(self, src_el, src_pad_name, enc, enc_sink, enc_src, sink_el) -> None:
        """src_el.<площадка> -> enc.<enc_sink>; enc.<enc_src> -> sink_el."""
        sp = src_el.get_static_pad(src_pad_name) or self._req(src_el, src_pad_name)
        self._plink(sp, self._req(enc, enc_sink))
        self._plink(enc.get_static_pad(enc_src), sink_el.get_static_pad("sink"))

    def _link_srtp_in(self, src_el, dec, dec_sink, dec_src, rtpbin, rtpbin_sink) -> None:
        """src_el -> dec.<dec_sink>; dec.<dec_src> -> rtpbin.<rtpbin_sink>."""
        self._plink(src_el.get_static_pad("src"), dec.get_static_pad(dec_sink))
        out = dec.get_static_pad(dec_src)
        self._plink(out, self._req(rtpbin, rtpbin_sink))

    # ---- статистика ----
    def _session_stats(self) -> dict:
        """Структура stats rtpsession 0 -> dict (без ключей и адресов)."""
        out = {"sources": []}
        pipe, rb = self._pipe, self._rtpbin
        if pipe is None or rb is None:
            return out
        try:
            sess = rb.emit("get-session", 0)
            st = sess.get_property("stats")
        except Exception:
            return out
        if st is None:
            return out
        out["bitrate"] = _sfield(st, "rtx-drop-count", None)
        arr = st.get_value("source-stats") if st.has_field("source-stats") else None
        for s in (arr or []):
            d = {}
            for f in ("ssrc", "internal", "validated", "sender", "packets-received", "packets-lost", "jitter",
                      "bitrate", "octets-received", "packets-sent", "octets-sent", "have-rb", "rb-packetslost",
                      "rb-jitter", "rb-fractionlost", "sent-pli-count", "sent-fir-count"):
                if s.has_field(f):
                    d[f] = s.get_value(f)
            out["sources"].append(d)
        return out


def _sfield(st, name, default):
    return st.get_value(name) if st.has_field(name) else default


def _set_every_packet(ext) -> bool:
    """Включить расширение rtphdrextntp64 на каждом пакете; False, если у элемента нет такого свойства."""
    try:
        if ext.find_property("every-packet") is None:
            return False
        ext.set_property("every-packet", True)
        return bool(ext.get_property("every-packet"))
    except Exception:
        return False


NTP_UNIX_OFFSET_S = 2208988800.0   # секунд между эпохой NTP (1900) и Unix (1970)
LATENCY_WINDOW = 600               # кадров в окне оценки задержки
OFFSET_RESET_S = 0.001             # смещение часов изменилось больше — прежние отсчёты задержки сбрасываются


def ntp_ns_to_unix(ts: int) -> float:
    """Метка GstReferenceTimestampMeta (timestamp/x-ntp) — наносекунды от эпохи NTP (1900) -> секунды Unix."""
    return ts / 1e9 - NTP_UNIX_OFFSET_S


def frame_latency_ms(now_unix: float, capture_unix: float, offset_s: float) -> float:
    """Задержка кадра, мс: время на пульте минус метка захвата на блоке, приведённая к часам пульта.
    offset_s — «часы блока минус часы пульта» (set-clock-offset, контракт §4/§6): метка блока в часах пульта —
    capture − offset_s."""
    return (now_unix - (capture_unix - offset_s)) * 1000.0


def latency_summary(samples) -> dict:
    """p50/p95/max по окну отсчётов (мс), ближайший ранг; пустое окно — все None."""
    xs = sorted(samples)
    if not xs:
        return {"p50": None, "p95": None, "max": None, "samples": 0}

    def pct(q):
        return round(xs[min(len(xs) - 1, max(0, int(q * len(xs) + 0.999999) - 1))], 3)

    return {"p50": pct(0.50), "p95": pct(0.95), "max": round(xs[-1], 3), "samples": len(xs)}


class SrtpTx(_Base):
    """Передатчик SRTP. start()/stop() идемпотентны; stats() — словарь полей контракта §4."""

    role = "tx"

    def __init__(self, cfg: TxConfig, on_event: Optional[EventCb] = None):
        super().__init__(cfg, on_event)
        self._first_done = False

    def _build(self):
        Gst, GLib, GObject = gst()
        try:
            pipe = Gst.parse_launch(build_tx_description(self.cfg))
        except GLib.Error as e:
            raise RuntimeError(f"конвейер TX не собран: {e.message}") from None
        rb = self._rtpbin = pipe.get_by_name("rtpbin")
        enc, dec = pipe.get_by_name("senc"), pipe.get_by_name("sdec")
        self._setup_srtp(pipe, {rtcp_ssrc_for(self.cfg.ssrc)})
        self._first_done = False
        self._plink(pipe.get_by_name("payout").get_static_pad("src"), self._req(rb, "send_rtp_sink_0"))
        self._link_srtp_out(rb, "send_rtp_src_0", enc, "rtp_sink_0", "rtp_src_0", pipe.get_by_name("rtpout"))
        self._link_srtp_out(rb, "send_rtcp_src_0", enc, "rtcp_sink_0", "rtcp_src_0", pipe.get_by_name("rtcpout"))
        self._link_srtp_in(pipe.get_by_name("rtcpin"), dec, "rtcp_sink", "rtcp_src", rb, "recv_rtcp_sink_0")
        pay = pipe.get_by_name("pay")
        self._attach_ntp_ext(pay)
        sinkpad = pay.get_static_pad("sink")
        # один пробник на первый буфер (потом снимается) и события вверх (force-key-unit), не на пакеты
        sinkpad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_UPSTREAM, self._pay_probe)
        # сессия создана запросом площадок выше; подписка на RTCP-обратную связь (счётчик PLI)
        self._hook_session(self._rtpbin, None)
        return pipe

    def _attach_ntp_ext(self, pay) -> None:
        Gst, _, _ = gst()
        try:
            ext = Gst.ElementFactory.make("rtphdrextntp64", None)
            if ext is None:
                self.ntp64 = False
                return
            ext.set_id(1)
            # Метка NTP нужна на КАЖДОМ пакете (по умолчанию её получает только первый): задержку считает приёмник
            # по каждому кадру. Свойства every-packet нет в старых GStreamer — тогда метка редкая, это видно в stats().
            self.ntp64_every_packet = _set_every_packet(ext)
            pay.emit("add-extension", ext)
            self.ntp64 = True
        except Exception:
            self.ntp64 = False

    ntp64 = False
    ntp64_every_packet = False

    def _hook_session(self, rtpbin, internal_ssrc):
        sess = rtpbin.emit("get-session", 0)
        if sess is None:
            raise RuntimeError("сессия rtpbin не создана")
        sess.get_property("internal-session").connect("on-feedback-rtcp", self._on_feedback)

    def _on_feedback(self, _sess, rtcp_type, fbtype, sender_ssrc, media_ssrc, fci):
        # PSFB (206) + PLI (1) / FIR (4): запрос ключевого кадра от приёмника
        if rtcp_type == 206 and fbtype in (1, 4):
            self.counters["pli_received"] += 1

    def _pay_probe(self, pad, info):
        Gst, _, _ = gst()
        if info.type & Gst.PadProbeType.BUFFER:
            if not self._first_done:
                self._first_done = True
                self._emit("first-frame")
            return Gst.PadProbeReturn.OK
        ev = info.get_event()
        if ev is not None and ev.type == Gst.EventType.CUSTOM_UPSTREAM:
            st = ev.get_structure()
            if st is not None and st.get_name() == "GstForceKeyUnit":
                self.counters["keyframes_requested"] += 1
                self._emit("keyframe")
        return Gst.PadProbeReturn.OK

    def request_keyframe(self) -> bool:
        """Принудительный ключевой кадр кодеру (GstForceKeyUnit вверх по конвейеру). False, если TX не запущен."""
        Gst, _, _ = gst()
        with self._lock:
            pipe = self._pipe
            if pipe is None:
                return False
            pay = pipe.get_by_name("pay")
        ev = Gst.Event.new_custom(Gst.EventType.CUSTOM_UPSTREAM, Gst.Structure.new_from_string(
            "GstForceKeyUnit, all-headers=(boolean)true"))
        return bool(pay.get_static_pad("sink").push_event(ev))

    def stats(self) -> dict:
        age = time.monotonic() - self.t_start if self.t_start else 0.0
        src = {}
        for s in self._session_stats().get("sources", []):
            if s.get("internal"):
                src = s
        c = self.counters
        return {
            "packets": src.get("packets-sent"), "lost": None, "jitter_ms": None, "fps": None, "frames": None,
            "bitrate_kbps": (src["bitrate"] / 1000.0 if src.get("bitrate") else None),
            "pli_sent": None, "pli_received": c["pli_received"], "keyframes_requested": c["keyframes_requested"],
            "latency_ms": None, "unknown_ssrc": c["unknown_ssrc"], "ntp64": self.ntp64,
            "ntp64_every_packet": self.ntp64_every_packet, "uptime_s": round(age, 2),
        }


class SrtpRx(_Base):
    """Приёмник SRTP. Кадры считаются одним пробником на выходе декодера."""

    role = "rx"

    def __init__(self, cfg: RxConfig, on_event: Optional[EventCb] = None):
        super().__init__(cfg, on_event)
        self.frames = 0
        self.last_frame = 0.0
        self._hist: list[tuple[float, int]] = []
        # Смещение часов (приёмник минус передатчик, с); пока None, задержка не считается (latency_ms = null).
        self.clock_offset_s: Optional[float] = None
        self._lat: collections.deque = collections.deque(maxlen=LATENCY_WINDOW)
        self._lat_lock = threading.Lock()
        self._ntp_caps = None

    def set_clock_offset(self, offset_s: Optional[float]) -> None:
        """Смещение часов приёмника относительно передатчика. Пульт повторяет его каждые 5 с: прежние отсчёты
        задержки сбрасываются, только если смещение изменилось больше OFFSET_RESET_S (иначе окно обнулялось бы
        каждые 5 с и число отсчётов зависело бы от момента чтения)."""
        new = None if offset_s is None else float(offset_s)
        with self._lat_lock:
            old = self.clock_offset_s
            self.clock_offset_s = new
            if new is None or old is None or abs(new - old) > OFFSET_RESET_S:
                self._lat.clear()

    def request_keyframe(self) -> bool:
        """Запросить ключевой кадр у передатчика: GstForceKeyUnit вверх от depay, rtpbin превращает его в PLI."""
        Gst, _, _ = gst()
        with self._lock:
            pipe = self._pipe
            if pipe is None:
                return False
            depay = pipe.get_by_name("depay")
        ev = Gst.Event.new_custom(Gst.EventType.CUSTOM_UPSTREAM, Gst.Structure.new_from_string(
            "GstForceKeyUnit, all-headers=(boolean)true"))
        return bool(depay.get_static_pad("sink").push_event(ev))

    def _build(self):
        Gst, GLib, GObject = gst()
        try:
            pipe = Gst.parse_launch(build_rx_description(self.cfg))
        except GLib.Error as e:
            raise RuntimeError(f"конвейер RX не собран: {e.message}") from None
        self.frames = 0
        self.last_frame = 0.0
        self._hist = [(time.monotonic(), 0)]
        self._ntp_caps = Gst.Caps.from_string("timestamp/x-ntp")
        with self._lat_lock:
            self._lat.clear()
        rtpbin = self._rtpbin = pipe.get_by_name("rtpbin")
        rx_rtcp = rtcp_ssrc_for(self.cfg.ssrc)
        enc, dec = pipe.get_by_name("senc"), pipe.get_by_name("sdec")
        self._setup_srtp(pipe, {self.cfg.ssrc})
        depay = pipe.get_by_name("depay")
        self._attach_ntp_ext_rx(depay)
        self._link_srtp_in(pipe.get_by_name("rtpin"), dec, "rtp_sink", "rtp_src", rtpbin, "recv_rtp_sink_0")
        self._link_srtp_in(pipe.get_by_name("rtcpin"), dec, "rtcp_sink", "rtcp_src", rtpbin, "recv_rtcp_sink_0")
        self._link_srtp_out(rtpbin, "send_rtcp_src_0", enc, "rtcp_sink_0", "rtcp_src_0", pipe.get_by_name("rtcpout"))

        def pad_added(_b, pad):
            if pad.get_name().startswith("recv_rtp_src_0"):
                pad.link(depay.get_static_pad("sink"))

        rtpbin.connect("pad-added", pad_added)
        self._hook_session(rtpbin, rx_rtcp)
        rtpbin.connect("request-pt-map", self._pt_map)
        dec = pipe.get_by_name("dec")
        dec.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._frame_probe)
        return pipe

    def _pt_map(self, _b, sess, pt):
        Gst, _, _ = gst()
        return Gst.Caps.from_string(
            f"application/x-rtp,media=video,encoding-name=H264,payload={PAYLOAD_TYPE},clock-rate={CLOCK_RATE},"
            f"rtcp-fb-nack-pli=true")

    def _attach_ntp_ext_rx(self, depay) -> None:
        Gst, _, _ = gst()
        self.ntp64 = False
        try:
            ext = Gst.ElementFactory.make("rtphdrextntp64", None)
            if ext is not None:
                ext.set_id(1)
                self.ntp64_every_packet = _set_every_packet(ext)
                depay.emit("add-extension", ext)
                self.ntp64 = True
        except Exception:
            pass

    ntp64 = False
    ntp64_every_packet = False

    def _hook_session(self, rtpbin, internal_ssrc):
        sess = rtpbin.emit("get-session", 0)
        if sess is None:
            raise RuntimeError("сессия rtpbin не создана")
        # собственный SSRC приёмника задаём сами, чтобы передатчик отдавал ключ SRTCP только для него
        isess = sess.get_property("internal-session")
        isess.set_property("internal-ssrc", internal_ssrc)
        isess.connect("on-sending-rtcp", self._on_sending_rtcp)

    def _on_sending_rtcp(self, _sess, buf, early):
        # PLI (PSFB fmt=1) и FIR (fmt=4) считаются разбором заголовков составного пакета RTCP. Фильтра по early нет:
        # на плате (GStreamer 1.26.2) обратная связь уходит и в обычном (не раннем) пакете, и счётчик оставался нулём.
        try:
            ok, mi = buf.map(1)  # Gst.MapFlags.READ
            if ok:
                try:
                    data = bytes(mi.data)
                finally:
                    buf.unmap(mi)
                off = 0
                while off + 4 <= len(data):
                    fmt, pt = data[off] & 0x1F, data[off + 1]
                    length = (int.from_bytes(data[off + 2:off + 4], "big") + 1) * 4
                    if pt == 206 and fmt in (1, 4):
                        self.counters["pli_sent"] += 1
                    off += length
        except Exception:
            pass
        return False

    def _frame_probe(self, pad, info):
        Gst, _, _ = gst()
        now = time.monotonic()
        self.frames += 1
        self.last_frame = now
        if self.frames == 1:
            self._emit("first-frame")
        off = self.clock_offset_s
        if off is not None:
            self._sample_latency(info.get_buffer(), off)
        return Gst.PadProbeReturn.OK

    def _sample_latency(self, buf, off: float) -> None:
        """Задержка кадра, мс (frame_latency_ms): сейчас (Unix) − метка NTP из расширения RTP в часах пульта."""
        try:
            meta = buf.get_reference_timestamp_meta(self._ntp_caps)
            if meta is None:
                return
            ms = frame_latency_ms(time.time(), ntp_ns_to_unix(meta.timestamp), off)
        except Exception:
            return
        with self._lat_lock:
            self._lat.append(ms)

    def stats(self) -> dict:
        now = time.monotonic()
        # окно для fps: последние 30 с по обращениям к stats()
        self._hist.append((now, self.frames))
        self._hist = [h for h in self._hist if now - h[0] <= 30.0]
        fps = None
        if len(self._hist) >= 2 and self._hist[-1][0] > self._hist[0][0]:
            fps = round((self._hist[-1][1] - self._hist[0][1]) / (self._hist[-1][0] - self._hist[0][0]), 2)
        src = {}
        for s in self._session_stats().get("sources", []):
            if not s.get("internal") and s.get("ssrc") == self.cfg.ssrc:
                src = s
        c = self.counters
        with self._lat_lock:
            lat = latency_summary(list(self._lat)) if self.clock_offset_s is not None else None
        return {
            "packets": src.get("packets-received"),
            "lost": (max(0, src["packets-lost"]) if src.get("packets-lost") is not None else None),
            "jitter_ms": (src["jitter"] * 1000.0 / CLOCK_RATE if src.get("jitter") is not None else None),
            "fps": fps, "frames": self.frames,
            "bitrate_kbps": (src["bitrate"] / 1000.0 if src.get("bitrate") else None),
            # PLI/FIR: разбор исходящего RTCP и счётчики источника rtpsession (берётся большее: на плате разбор давал 0)
            "pli_sent": max(c["pli_sent"], int(src.get("sent-pli-count") or 0) + int(src.get("sent-fir-count") or 0)),
            "pli_received": None, "keyframes_requested": None,
            "latency_ms": lat, "unknown_ssrc": c["unknown_ssrc"], "ntp64": self.ntp64,
            "ntp64_every_packet": self.ntp64_every_packet,
            "last_frame_age_s": (round(now - self.last_frame, 3) if self.last_frame else None),
        }


# ---------------------------------------------------------------------------------------------------------------
# selftest-CLI: сценарии для системных тестов (печатают одну строку JSON)
# ---------------------------------------------------------------------------------------------------------------
_TEST_MEDIA = ("videotestsrc is-live=true pattern=ball ! video/x-raw,width=640,height=360,framerate=30/1 ! "
               "x264enc tune=zerolatency speed-preset=ultrafast bitrate=2000 key-int-max={kimax} ! "
               "identity name=kfwatch ! video/x-h264,stream-format=byte-stream,alignment=au")
# Плата (Pi 4, аппаратные кодер и декодер): SHEREGESH_SELFTEST_MEDIA — свой фрагмент источника с {kimax} и identity
# name=kfwatch, SHEREGESH_SELFTEST_DECODER — v4l2h264dec. Пример фрагмента — _BOARD_MEDIA.
_BOARD_MEDIA = ("videotestsrc is-live=true pattern=ball ! video/x-raw,format=YUY2,width=1280,height=720,framerate=30/1 ! "
                "v4l2h264enc extra-controls=controls,repeat_sequence_header=1,video_bitrate=6000000,"
                "h264_i_frame_period={kimax} ! video/x-h264,level=(string)4 ! h264parse ! identity name=kfwatch ! "
                "video/x-h264,stream-format=byte-stream,alignment=au")


def _selftest_pair(tx_key, rx_key, tx_ssrc, rx_ssrc, base_port, cipher="aes-128-gcm", kimax=300, via_port=None,
                   jitter_latency_ms=None):
    ke = KEY_LENGTHS[cipher]
    tx = SrtpTx(TxConfig(stream="main", ssrc=tx_ssrc, peer_ip="127.0.0.1", peer_port=via_port or base_port,
                         rtcp_local_port=base_port + 3, rtcp_peer_port=base_port + 2,
                         media=os.environ.get("SHEREGESH_SELFTEST_MEDIA", _TEST_MEDIA).format(kimax=kimax), srtp=SrtpParams(cipher, tx_key[:ke].ljust(ke, b"\0"))))
    rx = SrtpRx(RxConfig(stream="main", ssrc=rx_ssrc, peer_ip="127.0.0.1", local_port=base_port,
                         rtcp_local_port=base_port + 2, rtcp_peer_port=base_port + 3,
                         decoder=os.environ.get("SHEREGESH_SELFTEST_DECODER", "avdec_h264"),
                         sink="fakesink sync=false", srtp=SrtpParams(cipher, rx_key[:ke].ljust(ke, b"\0")),
                         **({} if jitter_latency_ms is None else {"jitter_latency_ms": jitter_latency_ms})))
    return tx, rx


def _keyframe_counter(tx):
    Gst, _, _ = gst()
    cnt = {"idr": 0}

    def probe(pad, info):
        buf = info.get_buffer()
        if not buf.has_flags(Gst.BufferFlags.DELTA_UNIT):
            cnt["idr"] += 1
        return Gst.PadProbeReturn.OK

    tx._pipe.get_by_name("kfwatch").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, probe)
    return cnt


def _run_pair(tx, rx, seconds):
    events = []
    tx.on_event = lambda k, d: events.append(("tx", k))
    rx.on_event = lambda k, d: events.append(("rx", k))
    rx.start()
    tx.start()
    cnt = _keyframe_counter(tx)
    t0 = time.monotonic()
    first = None
    while time.monotonic() - t0 < seconds:
        time.sleep(0.05)
        if first is None and rx.frames > 0:
            first = time.monotonic() - t0
    return events, cnt, first


def _udp_lossy_proxy(listen_port, fwd_port, drop_after_s, drop_for_s, stop):
    """Переправляет UDP 127.0.0.1:listen -> fwd, на drop_for_s секунд после drop_after_s теряет все пакеты (только в selftest)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", listen_port))
    s.settimeout(0.1)
    o = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    t0 = time.monotonic()
    dropped = [0]
    while not stop.is_set():
        try:
            data, _ = s.recvfrom(65535)
        except socket.timeout:
            continue
        el = time.monotonic() - t0
        if drop_after_s <= el < drop_after_s + drop_for_s:
            dropped[0] += 1
            continue
        o.sendto(data, ("127.0.0.1", fwd_port))
    s.close()
    o.close()
    return dropped


def _udp_impair_proxy(listen_port, fwd_port, loss, reorder, dup, seed, stop, cnt):
    """UDP 127.0.0.1:listen -> fwd с искажениями канала (только selftest): потеря с вероятностью loss, перестановка
    с соседним пакетом — reorder, повтор — dup. Счётчики — в cnt."""
    import random
    import socket
    rnd = random.Random(seed)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", listen_port))
    s.settimeout(0.1)
    o = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    held = None
    while not stop.is_set():
        try:
            data, _ = s.recvfrom(65535)
        except socket.timeout:
            if held is not None:                     # тишина — отложенный пакет не держать
                o.sendto(held, ("127.0.0.1", fwd_port))
                held = None
            continue
        cnt["in"] += 1
        if rnd.random() < loss:
            cnt["dropped"] += 1
            continue
        if held is None and rnd.random() < reorder:
            held = data                              # уйдёт после следующего пакета
            cnt["reordered"] += 1
            continue
        o.sendto(data, ("127.0.0.1", fwd_port))
        if held is not None:
            o.sendto(held, ("127.0.0.1", fwd_port))
            held = None
        if rnd.random() < dup:
            o.sendto(data, ("127.0.0.1", fwd_port))
            cnt["duplicated"] += 1
    s.close()
    o.close()


def _impair(key, ssrc, base, cipher, args):
    """Сценарий impair: поток TX -> RX seconds секунд через искажающий прокси (--via=proxy, по умолчанию) или напрямую
    (--via=direct: искажения задаёт снаружи tc netem на lo); --jb — задержка джиттер-буфера rx, мс. Итог — кадры, частота после первого кадра, наибольший
    разрыв между кадрами, PLI, потери rtpbin, счётчики прокси."""
    opt = {"loss": 0.01, "reorder": 0.05, "dup": 0.0, "seconds": 12.0, "seed": 1, "jb": 20}
    via = "proxy"
    for a in args:
        k, _, v = a.removeprefix("--").partition("=")
        if k in opt:
            opt[k] = float(v)
        elif k == "via":
            via = v
    stop = threading.Event()
    cnt = {"in": 0, "dropped": 0, "reordered": 0, "duplicated": 0}
    tx, rx = _selftest_pair(key, key, ssrc, ssrc, base, cipher, kimax=3000,
                            via_port=base + 1 if via == "proxy" else None, jitter_latency_ms=int(opt["jb"]))
    if via != "proxy":                               # искажения задаёт tc netem снаружи — параметры прокси не действуют
        opt.update(loss=None, reorder=None, dup=None, seed=None)
    if via == "proxy":
        threading.Thread(target=_udp_impair_proxy, args=(base + 1, base, opt["loss"], opt["reorder"], opt["dup"],
                                                         int(opt["seed"]), stop, cnt), daemon=True).start()
    events = []
    tx.on_event = lambda k, d: events.append(("tx", k))
    rx.on_event = lambda k, d: events.append(("rx", k))
    rx.start()
    tx.start()
    t0 = time.monotonic()
    first, max_gap, last_n, last_t = None, 0.0, 0, None
    while time.monotonic() - t0 < opt["seconds"]:
        time.sleep(0.02)
        now, n = time.monotonic(), rx.frames
        if n and first is None:
            first, last_t, last_n = now - t0, now, n
        elif first is not None:
            if n != last_n:
                last_n, last_t = n, now
            max_gap = max(max_gap, now - last_t)
    stop.set()
    st_rx, st_tx = rx.stats(), tx.stats()
    span = opt["seconds"] - first if first is not None else None
    res = {"via": via, **opt, "frames": rx.frames, "first_frame_s": first,
           "fps_after_first": round(rx.frames / span, 2) if span else None, "max_frame_gap_s": round(max_gap, 3),
           "rx": st_rx, "tx": st_tx, "proxy": cnt if via == "proxy" else None, "events": events,
           "errors": rx.errors + tx.errors}
    tx.stop()
    rx.stop()
    return res


def selftest(name: str) -> dict:
    import secrets
    cipher = "aes-128-gcm"
    for a in sys.argv[3:]:
        if a.startswith("--cipher="):
            cipher = a.split("=", 1)[1]
    key = secrets.token_bytes(KEY_LENGTHS[cipher])
    other = secrets.token_bytes(KEY_LENGTHS[cipher])
    ssrc = secrets.randbelow(0xFFFFFFFE) + 1
    res = {"scenario": name, "cipher": cipher}
    base = 17000 + secrets.randbelow(2000) * 4
    threads_before = threading.active_count()
    if name == "loopback":
        tx, rx = _selftest_pair(key, key, ssrc, ssrc, base, cipher)
        ev, cnt, first = _run_pair(tx, rx, 3.0)
        st_rx, st_tx = rx.stats(), tx.stats()
        res.update(frames=rx.frames, first_frame_s=first, rx=st_rx, tx=st_tx, events=ev, errors=rx.errors + tx.errors)
        tx.stop(); rx.stop()
    elif name == "wrongkey":
        tx, rx = _selftest_pair(key, other, ssrc, ssrc, base, cipher)
        ev, cnt, first = _run_pair(tx, rx, 3.0)
        res.update(frames=rx.frames, rx=rx.stats(), events=ev, errors=rx.errors + tx.errors)
        tx.stop(); rx.stop()
    elif name == "wrongssrc":
        tx, rx = _selftest_pair(key, key, ssrc, ssrc ^ 0x55555555 or 1, base, cipher)
        ev, cnt, first = _run_pair(tx, rx, 3.0)
        res.update(frames=rx.frames, rx=rx.stats(), events=ev, errors=rx.errors + tx.errors)
        tx.stop(); rx.stop()
    elif name == "stop":
        tx, rx = _selftest_pair(key, key, ssrc, ssrc, base, cipher)
        _run_pair(tx, rx, 1.5)
        tx.stop(); rx.stop(); tx.stop(); rx.stop()  # идемпотентность
        time.sleep(0.5)
        res.update(threads_before=threads_before, threads_after=threading.active_count(),
                   thread_names=[t.name for t in threading.enumerate()])
        # повторный старт после стопа работает
        tx2, rx2 = _selftest_pair(key, key, ssrc, ssrc, base + 8, cipher)
        _run_pair(tx2, rx2, 1.0)
        res["restart_frames"] = rx2.frames
        tx2.stop(); rx2.stop()
        time.sleep(0.3)
        res["threads_final"] = threading.active_count()
    elif name == "pli":
        # потеря пакетов на пути TX -> RX (прокси на 127.0.0.1) обрывает поток; ключевой кадр кодер даёт только по PLI
        stop = threading.Event()
        tx, rx = _selftest_pair(key, key, ssrc, ssrc, base, cipher, kimax=3000, via_port=base + 1)
        px = threading.Thread(target=_udp_lossy_proxy, args=(base + 1, base, 2.0, 0.15, stop), daemon=True)
        px.start()
        ev, cnt, first = _run_pair(tx, rx, 6.0)
        stop.set()
        res.update(frames=rx.frames, idr_total=cnt["idr"], rx=rx.stats(), tx=tx.stats(), events=ev,
                   errors=rx.errors + tx.errors)
        tx.stop(); rx.stop()
    elif name == "impair":
        res.update(_impair(key, ssrc, base, cipher, sys.argv[3:]))
    else:
        res["error"] = f"неизвестный сценарий {name}"
    return res


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) >= 2 and argv[0] == "selftest":
        try:
            out = selftest(argv[1])
        except Exception as e:  # selftest всегда печатает JSON
            out = {"scenario": argv[1], "error": f"{type(e).__name__}: {e}"}
        print(json.dumps(out, default=str), flush=True)
        return 0
    print("использование: python3 -m sheregesh.videosvc.pipelines selftest <loopback|wrongkey|wrongssrc|stop|pli|impair>"
          " [--loss=P --reorder=P --dup=P --seconds=S --jb=MS --via=proxy|direct]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
