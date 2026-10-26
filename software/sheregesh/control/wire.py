# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Формат команд органов управления в SHRG-LAB 1.4 (ADR-0013, раздел 3): rc и rc-state.

Чистый Python без зависимостей. Данные второй стороны недоверенные: check_rc отвергает любое тело rc, кроме
описанного в разделе 3.1 (лишние ключи, типы, диапазоны); sanitize_rc_state оставляет от rc-state только известные
поля, типы и длины. Каналы «Шерегеш» не трактует: 8 значений передаются 1:1. Выхода управления нет: output —
только "none".
"""
from dataclasses import dataclass

STATE_V = 1
CHANNELS = 8
CH_LIMIT = 1000                                    # значение канала: −1000…1000 (центр — 0)
BTN_MAX = 2 ** 24 - 1                              # 24 кнопки, бит i — i-я кнопка по возрастанию кода KEY
N_MAX = 2 ** 31 - 1                                # номер кадра в сеансе (50 Гц — больше года)
NAME_MAX = 128
STATES = ("IDLE", "ACTIVE", "FAILSAFE")
REASONS = ("start", "fresh", "disabled", "timeout", "link-lost", "device-lost")
OUTPUT = "none"
RC_KEYS = frozenset({"n", "en", "ch", "btn", "dev"})
DEV_KEYS = frozenset({"name", "ok"})
NUM_MAX = 1e9


@dataclass(frozen=True)
class RcFrame:
    n: int
    en: bool
    ch: tuple                                      # ровно CHANNELS целых −1000…1000
    btn: int
    dev_name: str
    dev_ok: bool


class BadRc(ValueError):
    """Тело rc не прошло проверку; text — причина для журнала (без данных тела сверх 80 символов)."""

    def __init__(self, text):
        super().__init__(text)
        self.text = text


def _int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def check_rc(body) -> RcFrame:
    """Тело rc → RcFrame. Иначе BadRc: не объект, лишние или недостающие ключи (тела и dev), n не 1…2^31−1,
    en/dev.ok не bool, ch — не 8 целых −1000…1000, btn не 0…2^24−1, dev.name не строка или длиннее NAME_MAX."""
    if not isinstance(body, dict):
        raise BadRc("body is not an object")
    keys = set(body)
    if keys != RC_KEYS:
        extra, missing = sorted(map(str, keys - RC_KEYS)), sorted(RC_KEYS - keys)
        raise BadRc(("unknown keys: " + ", ".join(extra) if extra else "missing keys: " + ", ".join(missing))[:80])
    n, en, ch, btn, dev = (body[k] for k in ("n", "en", "ch", "btn", "dev"))
    if not _int(n) or not 1 <= n <= N_MAX:
        raise BadRc("n must be an integer 1…2^31-1")
    if not isinstance(en, bool):
        raise BadRc("en must be a boolean")
    if not isinstance(ch, list) or len(ch) != CHANNELS \
            or not all(_int(v) and -CH_LIMIT <= v <= CH_LIMIT for v in ch):
        raise BadRc(f"ch must be {CHANNELS} integers -{CH_LIMIT}…{CH_LIMIT}")
    if not _int(btn) or not 0 <= btn <= BTN_MAX:
        raise BadRc("btn must be an integer 0…2^24-1")
    if not isinstance(dev, dict) or set(dev) != DEV_KEYS:
        raise BadRc("dev must be {\"name\": str, \"ok\": bool}")
    if not isinstance(dev["name"], str) or len(dev["name"]) > NAME_MAX or not isinstance(dev["ok"], bool):
        raise BadRc(f"dev: name must be a string up to {NAME_MAX} characters, ok a boolean")
    return RcFrame(n, en, tuple(ch), btn, dev["name"], dev["ok"])


def rc_body(frame: RcFrame) -> dict:
    """RcFrame → тело rc (обратное check_rc)."""
    return {"n": frame.n, "en": frame.en, "ch": list(frame.ch), "btn": frame.btn,
            "dev": {"name": frame.dev_name, "ok": frame.dev_ok}}


# ---------- rc-state ----------
def _num(x, *, integer=False, null=False):
    if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x:      # x == x: не NaN
        x = min(max(x, 0), NUM_MAX)
        return int(x) if integer else round(float(x), 3)
    return None if null else 0


def _ch(v):
    if not isinstance(v, list) or len(v) != CHANNELS:
        return [0] * CHANNELS
    return [min(max(int(x), -CH_LIMIT), CH_LIMIT) if _int(x) else 0 for x in v]


def _dev(d):
    d = d if isinstance(d, dict) else {}
    return {"name": d["name"][:NAME_MAX] if isinstance(d.get("name"), str) else "", "ok": d.get("ok") is True}


def sanitize_rc_state(v):
    """rc-state второй стороны → dict раздела 3.2 или None (не объект, v != 1, неизвестное состояние).

    Отсутствующие и неверные поля заменяются значениями по умолчанию: числа — 0 (age_ms — None: кадров не было),
    reason не из REASONS — "", ch — 8 нулей, btn вне 0…2^24−1 — 0; output, отличный от "none", — "unknown"."""
    if not isinstance(v, dict) or v.get("v") != STATE_V or isinstance(v.get("v"), bool) or v.get("state") not in STATES:
        return None
    btn = v.get("btn")
    return {"v": STATE_V, "state": v["state"], "reason": v.get("reason") if v.get("reason") in REASONS else "",
            "since_s": _num(v.get("since_s")), "n": _num(v.get("n"), integer=True),
            "age_ms": _num(v.get("age_ms"), integer=True, null=True), "rate_hz": _num(v.get("rate_hz")),
            "gaps": _num(v.get("gaps"), integer=True), "stale": _num(v.get("stale"), integer=True),
            "bad": _num(v.get("bad"), integer=True), "late_max_ms": _num(v.get("late_max_ms"), integer=True),
            "failsafes": _num(v.get("failsafes"), integer=True), "timeout_ms": _num(v.get("timeout_ms"), integer=True),
            "ch": _ch(v.get("ch")), "btn": btn if _int(btn) and 0 <= btn <= BTN_MAX else 0, "dev": _dev(v.get("dev")),
            "output": OUTPUT if v.get("output") == OUTPUT else "unknown"}
