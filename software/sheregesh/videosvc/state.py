# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""state.json видеослужбы: схема v1, атомарная запись, кольцо переходов (ADR-0016, контракт §4).

Ключа в состоянии нет никогда: `update` отказывает полям с именами ключей, `validate_state` их ищет.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time

log = logging.getLogger("sheregesh.videosvc.state")

SCHEMA_VERSION = 1
ROLES = ("tx", "rx")
STATES = ("idle", "starting", "streaming", "degraded", "recovering", "error", "stuck")
RING = 20
MIN_INTERVAL_S = 0.2
_STREAM_RE = re.compile(r"[a-z0-9-]{1,16}")
_SESSION_RE = re.compile(r"[0-9a-f]{32}")


def _default_stream() -> dict:
    return {
        "state": "idle", "transport": "srtp", "session": "", "gen": 0, "ssrc": 0, "peer": "",
        "since_s": 0.0, "frames": 0, "fps": 0.0, "bitrate_kbps": 0,
        "rtp": {"packets": 0, "lost": 0, "jitter_ms": 0.0, "reordered": 0},
        "srtp": {"auth_fail": 0, "replay_fail": 0, "unknown_ssrc": 0},
        "rtcp": {"pli_sent": 0, "pli_received": 0, "keyframes_requested": 0, "keyframe_unsupported": False},
        "latency_ms": {"p50": None, "p95": None, "max": None, "samples": 0},
        "last_frame_age_s": None, "restarts": 0, "error": "",
        "source": {}, "nft_dropped": None, "transitions": [],
    }


_NESTED = ("rtp", "srtp", "rtcp", "latency_ms")   # обновляются слиянием по подполям
_SETTABLE = set(_default_stream()) - {"state", "since_s", "transitions"}


def _secret_name(name: str) -> bool:
    n = str(name).lower()
    return n == "key" or n.endswith("_key") or "key_b64" in n or "secret" in n or "password" in n


def _check_value(name: str, val) -> None:
    """Тип и диапазон нового значения поля — по тем же правилам, что и validate_state."""
    try:
        json.dumps(val, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: значение не сериализуется в JSON") from None
    if name in _NESTED_FIELDS:
        if not isinstance(val, dict):
            raise ValueError(f"{name}: ожидается объект")
        for sk, sv in val.items():
            ok = _NESTED_FIELDS[name].get(sk)
            if ok is None or not ok(sv) or _bad_float(sv):
                raise ValueError(f"{name}.{sk}: неизвестное поле или неверное значение")
    elif not _STREAM_FIELDS[name](val) or _bad_float(val):
        raise ValueError(f"{name}: неверное значение")


def _bad_float(v) -> bool:
    return isinstance(v, float) and (v != v or v in (float("inf"), float("-inf")))


def _scan_names(obj, path: str, out: list[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if _secret_name(k):
                out.append(f"{path}.{k}: поле ключа в состоянии запрещено")
            _scan_names(v, f"{path}.{k}", out)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _scan_names(v, f"{path}[{i}]", out)
    elif isinstance(obj, (bytes, bytearray)):
        out.append(f"{path}: байты в состоянии запрещены")


class StateWriter:
    """Накапливает состояние потоков и пишет его атомарно. Потокобезопасен."""

    def __init__(self, path: str, role: str, version: str, *, clock=time.time, mono=time.monotonic) -> None:
        if role not in ROLES:
            raise ValueError("role должна быть tx или rx")
        self.path = str(path)
        self.role = role
        self.version = str(version)
        self._clock = clock
        self._mono = mono
        self._streams: dict[str, dict] = {}
        self._since: dict[str, float] = {}
        self._lock = threading.Lock()
        self._last_write = float("-inf")
        self._dirty = False

    def _stream(self, name: str) -> dict:
        if not isinstance(name, str) or not _STREAM_RE.fullmatch(name):
            raise ValueError("неверное имя потока")
        st = self._streams.get(name)
        if st is None:
            st = self._streams[name] = _default_stream()
            self._since[name] = self._mono()
        return st

    def update(self, stream: str, **fields) -> None:
        """Обновить поля потока. Ключи не принимаются; state/transitions меняются только через transition()."""
        for name, val in fields.items():
            if _secret_name(name):
                raise ValueError("поле ключа в состоянии запрещено")
            if name not in _SETTABLE:
                raise ValueError(f"неизвестное поле состояния: {name}")
            _check_value(name, val)
            probe: list[str] = []
            _scan_names({name: val}, "", probe)
            if probe:
                raise ValueError("ключ или байты в состоянии запрещены")
        with self._lock:
            st = self._stream(stream)
            for name, val in fields.items():
                if name in _NESTED and isinstance(val, dict):
                    st[name].update(copy.deepcopy(val))
                else:
                    st[name] = copy.deepcopy(val)
            self._dirty = True
            self._maybe_write_locked()

    def transition(self, stream: str, state: str, reason: str) -> None:
        """Сменить состояние; запись гарантирована, переход попадает в кольцо из 20 последних."""
        if state not in STATES:
            raise ValueError("неизвестное состояние")
        with self._lock:
            st = self._stream(stream)
            if st["state"] != state:
                self._since[stream] = self._mono()
            st["state"] = state
            st["transitions"].append([self._clock(), state, str(reason)])
            del st["transitions"][:-RING]
            self._dirty = True
            self._write_locked(swallow=True)

    def snapshot(self) -> dict:
        """Текущий документ состояния (глубокая копия)."""
        with self._lock:
            return self._doc_locked()

    def write(self) -> None:
        """Записать безусловно (периодически раз в секунду из цикла службы). Ошибка записи — исключение."""
        with self._lock:
            self._write_locked(swallow=False)

    # --- внутреннее ---

    def _doc_locked(self) -> dict:
        now = self._mono()
        streams = copy.deepcopy(self._streams)
        for name, st in streams.items():
            st["since_s"] = round(now - self._since[name], 3)
        return {"v": SCHEMA_VERSION, "role": self.role, "version": self.version,
                "time": self._clock(), "streams": streams}

    def _maybe_write_locked(self) -> None:
        if self._mono() - self._last_write >= MIN_INTERVAL_S:
            self._write_locked(swallow=True)

    def _write_locked(self, swallow: bool) -> None:
        data = json.dumps(self._doc_locked(), ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
        tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.fchmod(fd, 0o644)
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self.path)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            if not swallow:
                raise
            log.warning("state: запись не удалась: %s", e)
            return
        self._last_write = self._mono()
        self._dirty = False


# --- проверка схемы --------------------------------------------------------------------------------------------

def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _opt_num(v) -> bool:
    return v is None or _num(v)


_STREAM_FIELDS = {
    "state": lambda v: v in STATES,
    "transport": lambda v: v == "srtp",
    "session": lambda v: isinstance(v, str) and (v == "" or bool(_SESSION_RE.fullmatch(v))),
    "gen": lambda v: _int(v) and v >= 0,
    "ssrc": lambda v: _int(v) and 0 <= v < 2**32,
    "peer": lambda v: isinstance(v, str),
    "since_s": lambda v: _num(v) and v >= 0,
    "frames": lambda v: _int(v) and v >= 0,
    "fps": lambda v: _num(v) and v >= 0,
    "bitrate_kbps": lambda v: _num(v) and v >= 0,
    "last_frame_age_s": _opt_num,
    "restarts": lambda v: _int(v) and v >= 0,
    "error": lambda v: isinstance(v, str),
    "source": lambda v: isinstance(v, dict),
    "nft_dropped": lambda v: v is None or (_int(v) and v >= 0),   # null: счётчик nftables доступен только root (контракт v3)
}
_NESTED_FIELDS = {
    "rtp": {"packets": _int, "lost": _int, "jitter_ms": _num, "reordered": _int},
    "srtp": {"auth_fail": _int, "replay_fail": _int, "unknown_ssrc": _int},
    "rtcp": {"pli_sent": _int, "pli_received": _int, "keyframes_requested": _int,
             "keyframe_unsupported": lambda v: isinstance(v, bool)},
    "latency_ms": {"p50": _opt_num, "p95": _opt_num, "max": _opt_num, "samples": _int},
}


def validate_state(obj) -> list[str]:
    """Список нарушений схемы v1; пустой список — документ корректен."""
    errs: list[str] = []
    if not isinstance(obj, dict):
        return ["корень: не объект"]
    for k in ("v", "role", "version", "time", "streams"):
        if k not in obj:
            errs.append(f"нет поля {k}")
    extra = set(obj) - {"v", "role", "version", "time", "streams"}
    if extra:
        errs.append(f"лишние поля: {sorted(extra)}")
    if "v" in obj and (not _int(obj["v"]) or obj["v"] != SCHEMA_VERSION):
        errs.append("v должно быть 1")
    if "role" in obj and obj["role"] not in ROLES:
        errs.append("role должна быть tx или rx")
    if "version" in obj and not isinstance(obj["version"], str):
        errs.append("version не строка")
    if "time" in obj and not _num(obj["time"]):
        errs.append("time не число")
    streams = obj.get("streams")
    if "streams" in obj and not isinstance(streams, dict):
        errs.append("streams не объект")
        streams = {}
    for name, st in (streams or {}).items():
        p = f"streams.{name}"
        if not isinstance(name, str) or not _STREAM_RE.fullmatch(name):
            errs.append(f"{p}: неверное имя потока")
        if not isinstance(st, dict):
            errs.append(f"{p}: не объект")
            continue
        want = set(_STREAM_FIELDS) | set(_NESTED_FIELDS) | {"transitions"}
        for k in sorted(want - set(st)):
            errs.append(f"{p}: нет поля {k}")
        for k in sorted(set(st) - want):
            errs.append(f"{p}: лишнее поле {k}")
        for k, ok in _STREAM_FIELDS.items():
            if k in st and not ok(st[k]):
                errs.append(f"{p}.{k}: неверное значение")
        for k, sub in _NESTED_FIELDS.items():
            if k not in st:
                continue
            d = st[k]
            if not isinstance(d, dict):
                errs.append(f"{p}.{k}: не объект")
                continue
            for sk in sorted(set(sub) - set(d)):
                errs.append(f"{p}.{k}: нет поля {sk}")
            for sk in sorted(set(d) - set(sub)):
                errs.append(f"{p}.{k}: лишнее поле {sk}")
            for sk, ok in sub.items():
                if sk in d and not ok(d[sk]):
                    errs.append(f"{p}.{k}.{sk}: неверное значение")
        tr = st.get("transitions")
        if "transitions" in st:
            if not isinstance(tr, list):
                errs.append(f"{p}.transitions: не список")
            else:
                if len(tr) > RING:
                    errs.append(f"{p}.transitions: больше {RING} записей")
                for i, t in enumerate(tr):
                    if not (isinstance(t, list) and len(t) == 3 and _num(t[0]) and t[1] in STATES
                            and isinstance(t[2], str)):
                        errs.append(f"{p}.transitions[{i}]: неверная запись")
    _scan_names(obj, "$", errs)
    return errs
