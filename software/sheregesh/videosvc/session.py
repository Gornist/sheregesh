# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Сеанс, поколение, ключ и сообщения видеосеанса SHRG-LAB 1.5 (ADR-0016, контракт §5–§6).

Чистая логика без транспорта: сообщения — это dict-ы body, которые кодирует `link/codec.py`.
Ключ SRTP живёт только в теле сообщения и в `Offer.key`; прежние ключи не хранятся (только
SHA-256 отпечатки, чтобы гарантировать «новое поколение = новый ключ»). Ни в `repr`, ни в
исключениях ключ не встречается; при записи сообщения в журнал его пропускают через `mask()`.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import re
import secrets
from dataclasses import dataclass, field

# Длина ключа с солью (байт) по шифру: GCM — 16 + 12, ICM — 16 + 14 (контракт §5). `null` запрещён.
KEY_LEN = {"aes-128-gcm": 28, "aes-128-icm": 30}
MASK = "***"
_STREAM_RE = re.compile(r"[a-z0-9-]{1,16}")
_SESSION_RE = re.compile(r"[0-9a-f]{32}")
_CODEC_RE = re.compile(r"[a-z0-9]{1,16}")
_REASON_RE = re.compile(r"[ -~]{1,64}")  # печатный ASCII
_MAX32 = 2**32 - 1
ACK_ERRORS = frozenset({"stale", "bad-request", "unsupported"})


class SchemaError(ValueError):
    """Нарушение схемы сообщения. `code` — код для video-key-ack.error: bad-request | unsupported.

    Текст исключения не содержит значений полей (в них может быть ключ).
    """

    def __init__(self, message: str, code: str = "bad-request") -> None:
        super().__init__(message)
        self.code = code


# --- генерация -------------------------------------------------------------------------------------------------

def new_session() -> str:
    """128 бит случайных: 32 hex-символа."""
    return secrets.token_hex(16)


def new_key(cipher: str) -> bytes:
    if cipher not in KEY_LEN:
        raise ValueError("неизвестный шифр")
    return secrets.token_bytes(KEY_LEN[cipher])


def new_ssrc(prev: int | None = None) -> int:
    """SSRC в 1..2^32-1, не равный прежнему."""
    while True:
        v = secrets.randbits(32)
        if v != 0 and v != prev:
            return v


def mask(obj):
    """Глубокая копия, где `key_b64` на любом уровне (dict, list, tuple) заменён на «***»."""
    if isinstance(obj, dict):
        return {k: (MASK if k == "key_b64" else mask(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask(v) for v in obj]
    return copy.copy(obj)


def _fp(key: bytes) -> bytes:
    return hashlib.sha256(key).digest()


# --- схемы -----------------------------------------------------------------------------------------------------

def _is_int(v, lo: int, hi: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _exact(body, keys: set[str], what: str, optional: frozenset[str] = frozenset()) -> None:
    if not isinstance(body, dict):
        raise SchemaError(f"{what}: не объект")
    got = set(body)
    if not (keys <= got <= keys | optional):
        raise SchemaError(f"{what}: набор полей не соответствует схеме")


def _check_ident(body: dict, what: str) -> None:
    if not isinstance(body["stream"], str) or not _STREAM_RE.fullmatch(body["stream"]):
        raise SchemaError(f"{what}: неверное имя потока")
    if not isinstance(body["session"], str) or not _SESSION_RE.fullmatch(body["session"]):
        raise SchemaError(f"{what}: неверный session")
    if not _is_int(body["gen"], 1, _MAX32):
        raise SchemaError(f"{what}: неверный gen")


def video_key_body(stream: str, session: str, gen: int, ssrc: int, cipher: str, key: bytes,
                   *, codec: str = "h264", profile: dict | None = None, rtcp_port: int = 7445) -> dict:
    """Собрать body сообщения video-key (блок → пульт). Ключ кодируется в base64."""
    if profile is None:
        profile = {"width": 1280, "height": 720, "fps": 30, "bitrate": 6000}
    return {"stream": stream, "session": session, "gen": gen, "transport": "srtp", "ssrc": ssrc,
            "codec": codec, "profile": dict(profile),
            "srtp": {"cipher": cipher, "key_b64": base64.b64encode(key).decode("ascii")},
            "rtcp_port": rtcp_port}


def validate_video_key(body) -> None:
    """Строгая проверка body video-key; нарушение — SchemaError (code: bad-request | unsupported)."""
    _exact(body, {"stream", "session", "gen", "transport", "ssrc", "codec", "profile", "srtp", "rtcp_port"},
           "video-key")
    _check_ident(body, "video-key")
    if body["transport"] != "srtp":
        raise SchemaError("video-key: transport должен быть srtp")
    if not _is_int(body["ssrc"], 1, _MAX32):
        raise SchemaError("video-key: неверный ssrc")
    if not isinstance(body["codec"], str) or not _CODEC_RE.fullmatch(body["codec"]):
        raise SchemaError("video-key: неверный codec")
    if not _is_int(body["rtcp_port"], 1, 65535):
        raise SchemaError("video-key: неверный rtcp_port")
    p = body["profile"]
    _exact(p, {"width", "height", "fps", "bitrate"}, "video-key.profile")
    if not (_is_int(p["width"], 1, 16384) and _is_int(p["height"], 1, 16384)
            and _is_int(p["fps"], 1, 240) and _is_int(p["bitrate"], 1, 10_000_000)):
        raise SchemaError("video-key.profile: значения вне диапазона")
    s = body["srtp"]
    _exact(s, {"cipher", "key_b64"}, "video-key.srtp")
    cipher = s["cipher"]
    if cipher is None or not isinstance(cipher, str) or cipher not in KEY_LEN:
        raise SchemaError("video-key.srtp: шифр не поддерживается", "unsupported")
    kb = s["key_b64"]
    if not isinstance(kb, str):
        raise SchemaError("video-key.srtp: key_b64 не строка")
    try:
        raw = base64.b64decode(kb, validate=True)
    except (binascii.Error, ValueError):
        raise SchemaError("video-key.srtp: key_b64 не base64") from None
    if len(raw) != KEY_LEN[cipher]:
        raise SchemaError("video-key.srtp: неверная длина ключа")


def ack_body(stream: str, session: str, gen: int, ok: bool, error: str | None = None) -> dict:
    body = {"stream": stream, "session": session, "gen": gen, "ok": ok}
    if error is not None:
        body["error"] = error
    return body


def validate_ack(body) -> None:
    _exact(body, {"stream", "session", "gen", "ok"}, "video-key-ack", frozenset({"error"}))
    _check_ident(body, "video-key-ack")
    if not isinstance(body["ok"], bool):
        raise SchemaError("video-key-ack: ok не bool")
    if "error" in body:
        if body["ok"]:
            raise SchemaError("video-key-ack: error при ok=true")
        if body["error"] not in ACK_ERRORS:
            raise SchemaError("video-key-ack: неизвестный код error")


def stop_body(stream: str, session: str, gen: int, reason: str) -> dict:
    return {"stream": stream, "session": session, "gen": gen, "reason": reason}


def validate_stop(body) -> None:
    _exact(body, {"stream", "session", "gen", "reason"}, "video-stop")
    _check_ident(body, "video-stop")
    if not isinstance(body["reason"], str) or not _REASON_RE.fullmatch(body["reason"]):
        raise SchemaError("video-stop: неверный reason")


def fallback_body(stream: str, session: str, gen: int, reason: str) -> dict:
    """video-fallback (пульт → блок, контракт v5 §6): пульт переходит на TLS до конца сеанса управления."""
    return {"stream": stream, "session": session, "gen": gen, "reason": reason}


def validate_fallback(body) -> None:
    _exact(body, {"stream", "session", "gen", "reason"}, "video-fallback")
    _check_ident(body, "video-fallback")
    if not isinstance(body["reason"], str) or not _REASON_RE.fullmatch(body["reason"]):
        raise SchemaError("video-fallback: неверный reason")


# --- сторона блока ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Offer:
    """Предложение нового поколения. Ключ не попадает в repr/str."""
    stream: str
    session: str
    gen: int
    ssrc: int
    cipher: str
    key: bytes = field(repr=False)

    def body(self, *, codec: str = "h264", profile: dict | None = None, rtcp_port: int = 7445) -> dict:
        return video_key_body(self.stream, self.session, self.gen, self.ssrc, self.cipher, self.key,
                              codec=codec, profile=profile, rtcp_port=rtcp_port)


class TxSessions:
    """Поколения ключей на стороне блока. Ключи не хранятся; для гарантии новизны — отпечатки."""

    def __init__(self) -> None:
        self.session = new_session()
        self._reset_state()

    def _reset_state(self) -> None:
        self._gen: dict[str, int] = {}
        self._cur: dict[str, tuple[int, bool]] = {}   # stream -> (gen, получено ack ok)
        self._used_ssrc: set[int] = set()
        self._used_keys: set[bytes] = set()

    def new_control_session(self) -> str:
        """Новый сеанс управления: новый session id, все потоки и история сброшены."""
        self.session = new_session()
        self._reset_state()
        return self.session

    def begin(self, stream: str, cipher: str) -> Offer:
        if not isinstance(stream, str) or not _STREAM_RE.fullmatch(stream):
            raise ValueError("неверное имя потока")
        if not isinstance(cipher, str) or cipher not in KEY_LEN:
            raise ValueError("неизвестный шифр")
        gen = self._gen.get(stream, 0) + 1
        while True:
            key = new_key(cipher)
            if _fp(key) not in self._used_keys:
                break
        ssrc = new_ssrc(None)
        while ssrc in self._used_ssrc:
            ssrc = new_ssrc(None)
        self._gen[stream] = gen
        self._cur[stream] = (gen, False)
        self._used_keys.add(_fp(key))
        self._used_ssrc.add(ssrc)
        return Offer(stream, self.session, gen, ssrc, cipher, key)

    def ack(self, body) -> bool:
        """True только для ok-ack с совпадающими (stream, session, gen) текущего поколения."""
        try:
            validate_ack(body)
        except SchemaError:
            return False
        cur = self._cur.get(body["stream"])
        if cur is None or body["session"] != self.session or body["gen"] != cur[0] or not body["ok"]:
            return False
        self._cur[body["stream"]] = (cur[0], True)
        return True

    def acked(self, stream: str) -> bool:
        cur = self._cur.get(stream)
        return bool(cur and cur[1])

    def current_gen(self, stream: str) -> int | None:
        cur = self._cur.get(stream)
        return cur[0] if cur else None

    def fallback_matches(self, body) -> bool:
        """video-fallback относится к текущему поколению (stream, session, gen); иначе его игнорируют (контракт v5)."""
        try:
            validate_fallback(body)
        except SchemaError:
            return False
        cur = self._cur.get(body["stream"])
        return cur is not None and body["session"] == self.session and body["gen"] == cur[0]

    def stop(self, stream: str, reason: str) -> dict:
        cur = self._cur.get(stream)
        if cur is None:
            raise ValueError("нет активного поколения")
        body = stop_body(stream, self.session, cur[0], reason)
        validate_stop(body)                   # при ошибке поколение остаётся активным
        del self._cur[stream]
        return body


# --- сторона пульта --------------------------------------------------------------------------------------------

class RxSessions:
    """Правила приёма video-key на пульте (контракт §6). Хранит только gen, ssrc и отпечаток ключа."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Новый сеанс управления: первый video-key снова задаёт session."""
        self.session: str | None = None
        self._last_gen: dict[str, int] = {}
        self._last: dict[str, tuple[int, bytes]] = {}     # stream -> (ssrc, отпечаток ключа) последнего принятого
        self._active: dict[str, int] = {}                 # stream -> gen, не остановленный video-stop

    def accept(self, body) -> tuple[bool, str | None]:
        try:
            validate_video_key(body)
        except SchemaError as e:
            return False, e.code
        stream, session, gen = body["stream"], body["session"], body["gen"]
        if self.session is not None and session != self.session:
            return False, "stale"
        fp = _fp(base64.b64decode(body["srtp"]["key_b64"]))
        last = self._last_gen.get(stream, 0)
        if gen == last and self._last.get(stream) == (body["ssrc"], fp) and stream in self._active:
            return True, None                              # повтор того же video-key: идемпотентен
        if gen <= last:
            return False, "stale"
        self.session = session
        self._last_gen[stream] = gen
        self._last[stream] = (body["ssrc"], fp)
        self._active[stream] = gen
        return True, None

    def ack_body(self, stream: str, session: str, gen: int, ok: bool, error: str | None = None) -> dict:
        return ack_body(stream, session, gen, ok, error)

    def stop(self, body) -> bool:
        """video-stop действует только на текущее принятое (stream, session, gen)."""
        try:
            validate_stop(body)
        except SchemaError:
            return False
        s = body["stream"]
        if body["session"] != self.session or self._active.get(s) != body["gen"]:
            return False
        del self._active[s]
        return True
