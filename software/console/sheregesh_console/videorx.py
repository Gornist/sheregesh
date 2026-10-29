# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Видеосеанс SRTP на стороне пульта: клиент службы sheregesh-video-rx (ADR-0016, контракт v5 §1, §3, §6, §8).

Без Qt. Канал управления (`PeerClient`) отдаёт сюда сообщения video-key и video-stop, начало и конец сеанса; GUI берёт
отсюда состояние (`active`, `stats()`) и путь сокета кадров. Порядок на video-key: `RxSessions.accept` → адрес блока в
nftables (`sheregesh-video-allow@<адрес>`) → `start` службы rx → `video-key-ack`. Откат решает пульт: нет первого кадра
за 5 с после ack или `start` не удался → `video-fallback` блоку, `stop` службе, `allow@none`; дальше TLS-видео (ADR-0011),
обратно на SRTP — только в новом сеансе управления.

Ключ SRTP живёт только в теле video-key и в аргументах `start`; в журнал сообщения идут через `session.mask`,
в `stats()`/ui-state ключа нет.
"""
import ipaddress
import json
import os
import queue
import subprocess
import threading
import time

from sheregesh.videosvc import session as vsession
from sheregesh.videosvc.ctl import CtlClient, CtlError

DEFAULT_RUNTIME = "/run/sheregesh-video"
RX_PORT = 7444                   # порт видео пульта (UDP SRTP); из link.json: video_port
RTCP_PORT = 7445
FIRST_FRAME_TIMEOUT_S = 5.0      # контракт v5 §6
OFFSET_EVERY_S = 5.0
RENEW_AFTER_S = 5.0              # кадров нет дольше — просим новое поколение (video-renew)
RENEW_EVERY_S = 10.0             # video-renew не чаще
STATE_MAX_AGE_S = 5.0            # state.json старше — сведений нет (служба не пишет)
SYSTEMCTL_TIMEOUT_S = 10.0
STREAM = "main"


def runtime_dir():
    return os.environ.get("SHEREGESH_VIDEO_RUNTIME") or DEFAULT_RUNTIME


def frames_socket(runtime=None, stream=STREAM):
    """Сокет кадров службы rx (unixfdsink, контракт §1)."""
    return os.path.join(runtime or runtime_dir(), f"frames-{stream}.sock")


def link_video_prefs(path=None):
    """(транспорт, порт видео) из link.json: `video.transport` (или `video_transport`) = "tls" запрещает SRTP;
    порт — `video_port`. Нет файла/ошибка — ("auto", 7444)."""
    path = os.path.expanduser(path or "~/.config/sheregesh/link.json")
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return "auto", RX_PORT
    if not isinstance(raw, dict):
        return "auto", RX_PORT
    video = raw.get("video")
    transport = video.get("transport") if isinstance(video, dict) else raw.get("video_transport")
    port = raw.get("video_port")
    if not (isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535):
        port = RX_PORT
    return ("tls" if transport == "tls" else "auto"), port


def systemctl_start(unit):
    """`systemctl start <unit>` (polkit разрешает sheregesh-ui только шаблон allow@*). True — успех."""
    try:
        r = subprocess.run(["systemctl", "start", unit], capture_output=True, timeout=SYSTEMCTL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _ident(body):
    """(stream, session, gen) для ответа даже на плохое тело; значения подгоняются под схему ack."""
    b = body if isinstance(body, dict) else {}
    stream = b.get("stream") if isinstance(b.get("stream"), str) and vsession._STREAM_RE.fullmatch(b["stream"]) else STREAM
    sess = b.get("session") if isinstance(b.get("session"), str) and vsession._SESSION_RE.fullmatch(b["session"]) \
        else "0" * 32
    gen = b.get("gen") if isinstance(b.get("gen"), int) and not isinstance(b.get("gen"), bool) \
        and 1 <= b["gen"] <= 2 ** 32 - 1 else 1
    return stream, sess, gen



UNMEASURED_LATENCY = {"p50": None, "p95": None, "max": None, "samples": 0}


def _count(v):
    """Счётчик службы: целое ≥ 0 или None (не измерено) — без подмены нулём."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _measure(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

class VideoRx:
    """Логика видеосеанса SRTP пульта. Все внешние эффекты подменяемы (ctl_factory, systemctl, clock)."""

    def __init__(self, send, peer_ip, *, runtime=None, ctl_factory=None, systemctl=None, clock=time.monotonic,
                 wall=time.time, clock_offset=None, log=None, transport_pref="auto", rx_port=RX_PORT, stream=STREAM,
                 first_frame_timeout=FIRST_FRAME_TIMEOUT_S):
        """send(тип, body) → bool — отправка блоку (PeerClient.send_video); peer_ip() → адрес блока или None;
        clock_offset() → смещение часов блок−пульт (с) или None."""
        self._send, self._peer_ip = send, peer_ip
        self.runtime = runtime or runtime_dir()
        self._ctl_factory = ctl_factory or (lambda path: CtlClient(path, timeout=1.0))
        self._systemctl = systemctl or systemctl_start
        self._clock, self._wall = clock, wall
        self._clock_offset = clock_offset
        self._log = log or (lambda level, text: None)
        self.transport_pref, self.rx_port, self.stream = transport_pref, int(rx_port), stream
        self.first_frame_timeout = first_frame_timeout
        self._rx = vsession.RxSessions()
        self._op = threading.RLock()            # операции с внешним миром идут по одной
        self._mu = threading.Lock()             # короткая защита полей, которые читает GUI
        self._phase = "idle"                    # idle | starting | streaming | fallback
        self._ident = None                      # (stream, session, gen) запущенного приёма
        self._renew_at = None                   # время последнего video-renew
        self._codec = "h264"
        self._ack_at = None
        self._last_offset_at = None
        self._reason = ""
        self._state = {}                        # последнее чтение state.json (поток)
        self._q = queue.Queue()
        self._threads = []
        self._stop = threading.Event()

    # ---------- жизненный цикл ----------
    def start(self):
        for target, name in ((self._worker, "videorx-worker"), (self._ticker, "videorx-tick")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self):
        self._stop.set()
        self._q.put(None)
        for t in self._threads:
            t.join(timeout=3)

    def _worker(self):
        while not self._stop.is_set():
            item = self._q.get()
            if item is None:
                return
            try:
                self.handle(*item)
            except Exception as e:                     # noqa: BLE001 — текст исключения может содержать ключ
                self._log("error", f"video session: internal error {type(e).__name__}")

    def _ticker(self):
        while not self._stop.wait(0.5):
            try:
                self.tick()
            except Exception as e:                     # noqa: BLE001
                self._log("error", f"video session tick: {type(e).__name__}")

    # ---------- интерфейс канала (PeerClient.video_session) ----------
    def available(self):
        """Служба rx отвечает на status."""
        try:
            self._ctl_factory(os.path.join(self.runtime, "ctl.sock")).request("status")
            return True
        except (CtlError, OSError):
            return False

    def hello_fields(self):
        if self.transport_pref != "tls" and self.available():
            return {"video_transports": ["srtp", "tls"], "video_rx_port": self.rx_port}
        return {"video_transports": ["tls"]}

    def on_message(self, mtype, body):
        """Из потока канала: только в очередь, сеть и systemctl — в рабочем потоке."""
        self._q.put((mtype, body))

    def on_session_start(self):
        self._q.put(("session-start", {}))

    def on_session_end(self):
        self._q.put(("session-end", {}))

    # ---------- состояние для GUI ----------
    @property
    def active(self):
        """Идёт видеосеанс SRTP (ключ принят, служба запущена, отката не было)."""
        with self._mu:
            return self._phase in ("starting", "streaming")

    @property
    def phase(self):
        with self._mu:
            return self._phase

    @property
    def codec(self):
        return self._codec

    @property
    def frames_path(self):
        return frames_socket(self.runtime, self.stream)

    def stats(self):
        """Метрики канала для ui-state/metrics (поля как у VideoClient.stats; ключа нет). Контракт видео v9, §4:
        null службы остаётся None (не измерено), измеренный ноль — 0. latency_ms службы — «захват → декодирован»,
        поэтому capture_to_decoded_ms; transport/decode_latency_ms — границы пути TLS, у SRTP не измеряются."""
        with self._mu:
            st, phase, reason = dict(self._state), self._phase, self._reason
        rtp, srtp = st.get("rtp") or {}, st.get("srtp") or {}
        lat = st.get("latency_ms") or {}
        lost = _count(rtp.get("lost"))
        fps = st.get("fps")
        return {"transport": "srtp", "phase": phase, "service_state": st.get("state", ""),
                "connected": st.get("state") in ("streaming", "degraded", "recovering"),
                "error": reason or st.get("error", ""), "gen": st.get("gen"),
                "packets": _count(rtp.get("packets")), "seq_gaps": lost, "lost_packets": lost,
                "jitter_ms": _measure(rtp.get("jitter_ms")), "reordered": _count(rtp.get("reordered")),
                "auth_fail": _count(srtp.get("auth_fail")), "replay_fail": _count(srtp.get("replay_fail")),
                "received_fps_5s": round(float(fps), 1) if isinstance(fps, (int, float)) else 0.0,
                "capture_to_decoded_ms": {"p50": _measure(lat.get("p50")), "p95": _measure(lat.get("p95")),
                                          "max": _measure(lat.get("max")), "samples": _count(lat.get("samples")) or 0},
                "transport_latency_ms": dict(UNMEASURED_LATENCY),
                "decode_latency_ms": {**UNMEASURED_LATENCY, "desync_resets": 0}}

    # ---------- обработка ----------
    def handle(self, mtype, body):
        """Синхронная обработка (рабочий поток; в тестах вызывается напрямую)."""
        with self._op:
            if mtype == "session-start":
                self._reset()
            elif mtype == "session-end":
                self._shutdown("session-end")
                self._reset()
            elif mtype == "video-key":
                self._log_msg("<-", mtype, body)
                self._video_key(body)
            elif mtype == "video-stop":
                self._log_msg("<-", mtype, body)
                self._video_stop(body)

    def _log_msg(self, arrow, mtype, body):
        self._log("info", f"video {arrow} {mtype} {json.dumps(vsession.mask(body), sort_keys=True)}")

    def _set(self, phase, reason=""):
        with self._mu:
            self._phase, self._reason = phase, reason

    def _reset(self):
        self._rx.reset()
        with self._mu:
            self._phase, self._reason, self._state = "idle", "", {}
        self._ident = self._ack_at = self._last_offset_at = None

    def _ack(self, body, ok, error=None):
        stream, sess, gen = _ident(body)
        out = self._rx.ack_body(stream, sess, gen, ok, error)
        self._log_msg("->", "video-key-ack", out)
        self._send("video-key-ack", out)

    def _ctl(self, cmd, args=None):
        c = self._ctl_factory(os.path.join(self.runtime, "ctl.sock"))
        try:
            return c.request(cmd, args or {})
        finally:
            try:
                c.close()
            except Exception:                          # noqa: BLE001 — подменные клиенты без close
                pass

    def _allow(self, address):
        return self._systemctl(f"sheregesh-video-allow@{address}.service")

    def _video_key(self, body):
        if self.phase == "fallback":                   # внутри сеанса управления обратно на SRTP нельзя
            self._ack(body, False, "unsupported")
            return
        ok, err = self._rx.accept(body)
        if not ok:
            self._ack(body, False, err or "bad-request")
            return
        ident = (body["stream"], body["session"], body["gen"])
        if self._ident == ident and self.phase in ("starting", "streaming"):
            self._ack(body, True)                      # повтор того же video-key: ничего не запускаем
            return
        try:
            ip = str(ipaddress.ip_address(str(self._peer_ip() or "").split("%")[0]))
        except ValueError:
            ip = None
        if ip is None or not self._allow(ip):
            self._ack(body, False, "unsupported")
            self._fallback(ident, "firewall-allow-failed")
            return
        args = {"stream": body["stream"], "session": body["session"], "gen": body["gen"], "ssrc": body["ssrc"],
                "peer": {"ip": ip}, "local": {"port": self.rx_port, "rtcp_port": RTCP_PORT},
                "srtp": dict(body["srtp"]), "codec": body["codec"]}
        try:
            self._ctl("start", args)
        except (CtlError, OSError) as e:
            self._log("warning", f"video rx start failed: {type(e).__name__} {getattr(e, 'code', '')}".strip())
            self._fallback(ident, "start-failed")
            return
        self._ident, self._codec = ident, body["codec"]
        self._ack_at = self._clock()
        self._last_offset_at = None
        self._set("starting")
        self._ack(body, True)

    def _video_stop(self, body):
        if self._rx.stop(body):
            self._shutdown("video-stop")

    def _shutdown(self, reason):
        """Остановить приём и закрыть порт (если что-то было запущено/разрешено)."""
        started = self._ident is not None
        self._ident = None
        if started:
            try:
                self._ctl("stop", {"stream": self.stream, "reason": reason})
            except (CtlError, OSError):
                pass
            self._allow("none")
        with self._mu:
            if self._phase in ("starting", "streaming"):
                self._phase = "idle"
            self._state = {}

    def _fallback(self, ident, reason):
        """Откат на TLS-видео (контракт v5 §6): video-fallback блоку, stop службе, allow@none."""
        out = vsession.fallback_body(*ident, reason)
        self._log_msg("->", "video-fallback", out)
        self._send("video-fallback", out)
        try:
            self._ctl("stop", {"stream": ident[0], "reason": reason})
        except (CtlError, OSError):
            pass
        self._allow("none")
        self._ident = None
        self._set("fallback", reason)

    # ---------- периодическое ----------
    def read_state(self):
        """Поток из state.json службы rx; {} — файла нет, он старый или не читается."""
        try:
            with open(os.path.join(self.runtime, "state.json"), encoding="utf-8") as f:
                doc = json.load(f)
            if abs(self._wall() - float(doc.get("time", 0))) > STATE_MAX_AGE_S:
                return {}
            st = (doc.get("streams") or {}).get(self.stream)
            return st if isinstance(st, dict) else {}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def tick(self):
        """Раз в ~0,5 с: обновить состояние службы, проверить первый кадр, передать смещение часов."""
        st = self.read_state()
        with self._mu:
            self._state = st
            phase = self._phase
        if phase not in ("starting", "streaming"):
            return
        now = self._clock()
        with self._op:
            if self._ident is None:
                return
            lost = "gen" in st and (st.get("session") != self._ident[1] or st.get("gen") != self._ident[2])
            stalled = (phase == "streaming" and st.get("state") in ("degraded", "recovering", "error", "stuck")
                       and (st.get("last_frame_age_s") is None or st.get("last_frame_age_s") > RENEW_AFTER_S))
            if lost or stalled:
                # Приёмник потерял поколение: служба rx перезапущена или долгий обрыв. Тот же ключ повторять нельзя —
                # счётчик переполнений ROC SRTP уже не восстановить (после ~65 тыс. пакетов, ≈ 2 мин при 6 Мбит/с,
                # приёмник с ROC = 0 отбрасывает всё). Просим у блока новое поколение (video-renew).
                if self._renew_at is None or now - self._renew_at >= RENEW_EVERY_S:
                    self._renew_at = now
                    why = "rx lost generation" if lost else f"no frames ({st.get('state')})"
                    out = vsession.renew_body(*self._ident, why)
                    self._log_msg("->", "video-renew", out)
                    self._send("video-renew", out)
                return
            if phase == "starting":
                if st.get("state") == "streaming":
                    self._set("streaming")
                elif now - self._ack_at > self.first_frame_timeout:
                    self._fallback(self._ident, "no-first-frame")
                    return
            if self._clock_offset is not None and (self._last_offset_at is None
                                                   or now - self._last_offset_at >= OFFSET_EVERY_S):
                off = self._clock_offset()
                if off is not None:
                    self._last_offset_at = now
                    try:
                        self._ctl("set-clock-offset", {"offset_s": float(off)})
                    except (CtlError, OSError):
                        pass
