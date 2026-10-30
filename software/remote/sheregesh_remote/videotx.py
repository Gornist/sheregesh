# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Видеосеанс блока по SRTP: агент как клиент службы sheregesh-video-tx (ADR-0016, контракт v5 §3, §5, §6, §8).

Агент не строит конвейер SRTP: он ведёт сообщения SHRG-LAB 1.5 (video-key, video-key-ack, video-stop, video-fallback),
хранит поколения ключей (`TxSessions`), открывает адрес пульта в nftables (`sheregesh-video-allow@<адрес>.service`) и
командует службе по локальному сокету (`start`, `stop`, `status`). Ключ живёт только в теле video-key и в команде
`start`; в журнал, status и текст ошибок он не попадает (всё сообщение видеосеанса проходит через `session.mask`).

Источник у кадров один владелец — служба tx. Другого видеотранспорта нет (TLS-видео ADR-0011 удалено): пульт без SRTP
или блок без службы tx — сеанс управления без видео (режим off, причина — в status.video.srtp.error). `video-fallback`
текущего поколения от старого пульта останавливает службу; видео в этом сеансе управления больше не предлагается.

Блокирующие вызовы (сокет службы, systemctl) выполняются в потоке (`asyncio.to_thread`), состояние защищено замком.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import os
import subprocess
import time

from sheregesh.link.codec import TYPES
from sheregesh.videosvc.ctl import CtlClient, CtlError, CtlUnavailable
from sheregesh.videosvc.session import TxSessions, mask

STREAM = "main"
CIPHER = "aes-128-gcm"
RTCP_PORT = 7445
DEFAULT_PROFILE = {"width": 1280, "height": 720, "fps": 30, "bitrate": 6000}   # bitrate — кбит/с
RUNTIME_ENV = "SHEREGESH_VIDEO_RUNTIME"
DEFAULT_RUNTIME = "/run/sheregesh-video"
SYSTEMCTL_ENV = "SHEREGESH_SYSTEMCTL"                 # испытания: подменная команда вместо systemctl
MIN_GEN_INTERVAL_S = 2.0                              # новое поколение не чаще
MAX_FAILS = 5                                         # подряд неудач до паузы
PAUSE_S = 30.0
ACK_TIMEOUT_S = 10.0                                  # пульт не ответил на video-key — поколение считается неудачным
PROBE_TIMEOUT_S = 1.5


def runtime_dir() -> str:
    return os.environ.get(RUNTIME_ENV) or DEFAULT_RUNTIME


def ctl_path() -> str:
    return os.path.join(runtime_dir(), "ctl.sock")


def systemctl_allow(target: str) -> None:
    """`systemctl start sheregesh-video-allow@<адрес|none>.service`. Подменяется в тестах. Отказ — исключение."""
    if target != "none":
        ipaddress.ip_address(target)                  # строгий IPv4/IPv6: имя экземпляра не собирается из чужого текста
    cmd = os.environ.get(SYSTEMCTL_ENV) or "systemctl"
    subprocess.run([cmd, "start", f"sheregesh-video-allow@{target}.service"], check=True, timeout=10,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def choose_transport(session: str | None, peer_hello, srtp_ok: bool) -> str:
    """SRTP — только в сеансе ≥ 1.5, если пульт объявил srtp и порт приёма, а у агента SRTP доступен; иначе видео нет
    («none» — с причиной в журнале и status)."""
    if not srtp_ok or "video-key" not in TYPES.get(session or "", ()) or not isinstance(peer_hello, dict):
        return "none"
    transports, port = peer_hello.get("video_transports"), peer_hello.get("video_rx_port")
    if isinstance(transports, list) and "srtp" in transports \
            and isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
        return "srtp"
    return "none"


class VideoTx:
    """Видеосеанс блока. Режимы: none (нет сеанса / до hello), srtp, off (сеанс управления без видео)."""

    def __init__(self, *, send, log, spec_fn, profile_fn=None, ctl=None, allow=None, clock=time.monotonic,
                 enabled=True):
        self._send, self._log = send, log             # send(mtype, body) — сопрограмма; log(str)
        self._spec_fn = spec_fn                       # текущий spec источника агента
        self._profile_fn = profile_fn or (lambda: dict(DEFAULT_PROFILE))
        self._ctl = ctl if ctl is not None else CtlClient(ctl_path(), timeout=PROBE_TIMEOUT_S)
        self._allow = allow or systemctl_allow
        self._clock = clock
        self.enabled = enabled
        self.srtp_ok = False                          # результат последней пробы сокета службы
        self.tx = TxSessions()
        self._lock: asyncio.Lock | None = None
        self._reset()

    def _reset(self) -> None:
        self.mode = "none"
        self.peer_ip: str | None = None
        self.rx_port = 0
        self.offer = None
        self.acked = False
        self.started = False
        self.allowed = False
        self.offer_at = 0.0
        self.last_gen_at = -1e9
        self.fails = 0
        self.pause_until = 0.0
        self.error = ""
        self.allow_error = ""
        self.svc: dict = {}                           # последнее состояние потока из status службы

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def transports(self) -> list[str]:
        return ["srtp"] if self.srtp_ok and self.enabled else []

    # --- обёртки над блокирующими вызовами -------------------------------------------------------------------

    async def _call(self, cmd: str, args: dict | None = None) -> dict:
        return await asyncio.to_thread(self._ctl.request, cmd, args)

    async def probe(self) -> bool:
        """Сокет службы tx доступен: запрос `status` успешен."""
        ok = False
        if self.enabled:
            try:
                await self._call("status")
                ok = True
            except (CtlError, CtlUnavailable, OSError):
                ok = False
        self.srtp_ok = ok
        return ok

    def _fail_text(self, what: str, e: Exception) -> str:
        # Только код/класс: текст исключения не должен нести значений запроса
        return f"{what}: {e.code if isinstance(e, CtlError) else type(e).__name__}"

    # --- сеанс управления ------------------------------------------------------------------------------------

    async def begin(self, peer_hello, peer_ip: str | None, session: str | None) -> str:
        """Начало сеанса управления (после hello пульта): выбор транспорта; для SRTP — первое поколение."""
        async with self.lock:
            if self.mode != "none":
                await self._teardown("hello-again", notify=True)
            ok = await self.probe()
            transport = choose_transport(session, peer_hello, ok)
            if transport != "srtp" or not peer_ip:
                self.mode = "off"
                self.error = ("no video: " + ("console does not offer srtp" if ok else
                                              "video-tx service unavailable" if self.enabled else "video disabled"))
                self._log(f"video: {self.error}")
                return "none"
            self.mode, self.peer_ip, self.rx_port = "srtp", peer_ip, peer_hello["video_rx_port"]
            self.tx.new_control_session()
            self._log(f"video: transport srtp -> {peer_ip}:{self.rx_port}")
            await self._offer("session start")
            return "srtp"

    async def _offer(self, why: str) -> None:
        offer = self.tx.begin(STREAM, CIPHER)
        self.offer, self.acked, self.started = offer, False, False
        self.offer_at = self.last_gen_at = self._clock()
        self.svc = {}
        body = offer.body(profile=self._profile_fn(), rtcp_port=RTCP_PORT)
        self._log(f"video: video-key gen {offer.gen} ({why}): {mask(body)}")
        await self._send("video-key", body)

    async def on_message(self, mtype: str, body) -> None:
        async with self.lock:
            if self.mode != "srtp" or self.offer is None:
                return
            if mtype == "video-key-ack":
                await self._on_ack(body)
            elif mtype == "video-renew":
                if self.tx.renew_matches(body):
                    now = self._clock()
                    if now - self.last_gen_at >= MIN_GEN_INTERVAL_S:
                        self._log(f"video: video-renew: {mask(body)}")
                        await self._offer(f"renew: {body.get('reason', '')}"[:64])
                    else:
                        self._log("video: video-renew too soon after the last generation — ignored")
                else:
                    self._log("video: video-renew of another generation ignored")
            elif mtype == "video-fallback":
                if self.tx.fallback_matches(body):
                    self._log(f"video: video-fallback: {mask(body)}")
                    await self._fallback()
                else:
                    self._log("video: video-fallback of another generation ignored")

    async def _on_ack(self, body) -> None:
        if isinstance(body, dict) and body.get("ok") is False and body.get("session") == self.tx.session \
                and body.get("gen") == self.offer.gen and body.get("stream") == STREAM:
            self.error = f"ack refused: {body.get('error', '?')}"
            self._log(f"video: video-key-ack refused: {mask(body)}")      # ждём video-fallback или конец сеанса
            return
        if not self.tx.ack(body):
            self._log("video: video-key-ack of another generation ignored")
            return
        if self.started:
            return                                                       # повтор ack
        self.acked = True
        self._log(f"video: video-key-ack ok gen {self.offer.gen}")
        if not self.allowed:
            try:
                await asyncio.to_thread(self._allow, self.peer_ip)
                self.allowed = True
                self.allow_error = ""
            except Exception as e:                                       # noqa: BLE001 — видео всё равно пробуем
                self.allow_error = self._fail_text("allow", e)
                self._log(f"video: {self.allow_error}")
        await self._start_service()

    async def _start_service(self) -> None:
        o = self.offer
        args = {"stream": STREAM, "session": o.session, "gen": o.gen, "ssrc": o.ssrc,
                "peer": {"ip": self.peer_ip, "port": self.rx_port, "rtcp_port": RTCP_PORT},
                "srtp": {"cipher": o.cipher, "key_b64": base64.b64encode(o.key).decode("ascii")},
                "source": self._spec_fn(), "profile": self._profile_fn()}
        try:
            await self._call("start", args)
            self.started = True
            self.error = ""
            self._log(f"video: tx start gen {o.gen} ok")
        except (CtlError, CtlUnavailable, OSError) as e:
            self.error = self._fail_text("tx start", e)
            self._log(f"video: {self.error}")

    async def _stop_service(self, reason: str) -> None:
        try:
            await self._call("stop", {"stream": STREAM, "reason": reason})
        except (CtlError, CtlUnavailable, OSError) as e:
            self._log(f"video: {self._fail_text('tx stop', e)}")

    async def _set_allow_none(self) -> None:
        try:
            await asyncio.to_thread(self._allow, "none")
        except Exception as e:                                           # noqa: BLE001
            self._log(f"video: {self._fail_text('allow none', e)}")
        self.allowed = False

    async def _fallback(self) -> None:
        """Старый пульт просит откат на TLS-видео, которого больше нет: служба остановлена, сеанс — без видео."""
        await self._stop_service("fallback")
        await self._set_allow_none()
        self.mode, self.offer, self.started, self.acked = "off", None, False, False
        self.svc = {}
        self.error = "no video: console requested fallback, tls video removed"
        self._log(f"video: {self.error}")

    async def on_select(self) -> None:
        """video-select сменил источник: новое поколение (новый ключ и SSRC), `start` с новым spec — после ack."""
        async with self.lock:
            if self.mode == "srtp" and self.offer is not None:
                await self._offer("source changed")

    async def tick(self) -> None:
        """Раз в секунду: состояние потока службы; при need-new-gen/stuck, провале start или молчании пульта —
        новое поколение не чаще раза в 2 с, после 5 подряд неудач — пауза 30 с."""
        async with self.lock:
            if self.mode != "srtp" or self.offer is None:
                return
            now = self._clock()
            trouble = ""
            if not self.acked:
                if now - self.offer_at > ACK_TIMEOUT_S:
                    trouble = self.error = "ack timeout"
            elif not self.started:
                trouble = self.error or "tx start failed"
            else:
                try:
                    res = await self._call("status")
                except (CtlError, CtlUnavailable, OSError) as e:
                    self.error = self._fail_text("tx status", e)
                    trouble = self.error
                else:
                    st = (res.get("streams") or {}).get(STREAM) if isinstance(res, dict) else None
                    if isinstance(st, dict) and st.get("gen") != self.offer.gen and st.get("state") == "idle":
                        self.svc = {}                    # служба перезапущена и потеряла поток: нужен новый start
                        self.error = trouble = "tx lost stream"
                    elif isinstance(st, dict) and st.get("gen") == self.offer.gen:
                        self.svc = st
                        if st.get("state") == "streaming":
                            self.fails, self.error = 0, ""
                        elif st.get("state") == "stuck" or (st.get("state") == "error"
                                                            and "need-new-gen" in str(st.get("error", ""))):
                            trouble = f"tx {st.get('state')}"
                            self.error = f"{trouble}: {st.get('error', '')}"[:120]
            if trouble:
                await self._retry(now, trouble)

    async def _retry(self, now: float, why: str) -> None:
        if now < self.pause_until or now - self.last_gen_at < MIN_GEN_INTERVAL_S:
            return
        if self.fails >= MAX_FAILS:
            self.pause_until, self.fails = now + PAUSE_S, 0
            self.error = f"video failed {MAX_FAILS} times in a row: {why}"[:120]
            self._log(f"video: {self.error}; pause {PAUSE_S:.0f} s")
            return
        self.fails += 1
        await self._offer(why)

    async def end(self, reason: str) -> None:
        """Конец сеанса управления: `stop` службе, `video-stop` пульту (если канал жив), `allow@none`."""
        async with self.lock:
            await self._teardown(reason, notify=True)

    async def _teardown(self, reason: str, notify: bool) -> None:
        was_srtp, offer = self.mode == "srtp", self.offer
        self._reset()
        if not was_srtp:
            return
        await self._stop_service(reason)
        if notify and offer is not None:
            try:
                body = self.tx.stop(STREAM, reason)
                self._log(f"video: video-stop: {mask(body)}")
                await self._send("video-stop", body)
            except (ValueError, OSError) as e:
                self._log(f"video: video-stop not sent: {type(e).__name__}")
        await self._set_allow_none()

    # --- состояние для status.video ---------------------------------------------------------------------------

    def status(self) -> dict | None:
        """Дополнение к status.video; None — сеанса управления нет, поля прежние."""
        if self.mode == "off":
            return {"active": False, "srtp": {"state": "off", "error": self.error}}
        if self.mode != "srtp":
            return None
        st = self.svc or {}
        rtcp = st.get("rtcp") if isinstance(st.get("rtcp"), dict) else {}
        svc_state = st.get("state") if st.get("gen") == getattr(self.offer, "gen", None) else None
        state = svc_state or ("negotiating" if not self.started else "starting")
        error = self.error or str(st.get("error") or "")
        out = {"transport": "srtp", "active": state == "streaming",
               "srtp": {"state": state, "gen": getattr(self.offer, "gen", 0), "fps": st.get("fps"),
                        "bitrate_kbps": st.get("bitrate_kbps"),
                        "keyframes_requested": rtcp.get("keyframes_requested"),
                        "keyframe_unsupported": bool(rtcp.get("keyframe_unsupported")), "error": error,
                        "peer": f"{self.peer_ip}:{self.rx_port}"}}
        if self.allow_error:
            out["srtp"]["allow_error"] = self.allow_error
        return out
