# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Формат внешнего модуля в SHRG-LAB 1.3 (ADR-0012, раздел 6): status.module и module-cmd.

Чистый Python без зависимостей: импортируют клиент пульта (sheregesh.link.client), представление пульта и приёмка.
Данные второй стороны недоверенные: sanitize_module оставляет только известные поля, типы и длины;
check_command отвергает любое тело команды, кроме перечисленного в разделе 6.3 (каналов, значений органов
управления и команд «взвести» в протоколе нет). Выход управления в S6 отсутствует: output — только "none".
"""
import re

STATUS_V = 1
STATES = ("DISCONNECTED", "DETECTED", "INITIALIZING", "READY", "FAULT", "RECOVERING", "DISABLED")
OPS = ("status", "reinit", "reset", "configure", "lab")
RESULTS = ("ok", "pending", "bad-request", "unknown-op", "forbidden", "disabled", "busy", "invalid", "not-saved",
           "not-running", "error")
LAB_SCENARIOS = ("attach", "detach", "iface-error", "hang", "heal", "slow-init", "crash", "emu-unavailable",
                 "emu-available")
ERROR_CODES = ("MOD-IFACE-ERROR", "MOD-NO-RESPONSE", "MOD-INIT-TIMEOUT", "MOD-INIT-FAILED", "MOD-PROFILE-MISMATCH",
               "MOD-RECOVER-TIMEOUT", "EMU-START-FAILED", "EMU-EXITED", "EMU-NO-KEEPALIVE", "EMU-UNAVAILABLE")
BACKEND_KINDS = ("lab-emulator", "hardware")
BACKEND_STATES = ("starting", "running", "restarting", "stopped", "unavailable")
SETTINGS_SOURCES = ("store", "factory", "recovered", "reset", "memory")
OUTPUT = "none"                                    # выхода управления нет при любом состоянии (раздел 12.3)
PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
# configure: только эти ключи и типы (раздел 6.3); значения сверх типа проверяют схема remote-1 и реестр профилей
CONFIGURE_KEYS = {"enabled": bool, "auto_init": bool, "auto_recover": bool, "profile": str}
REQ_MAX = 2 ** 31 - 1
HISTORY_MAX = 8
PROFILES_MAX = 8
MAX_STATUS_BYTES = 4096                            # status.module в JSON; строка SHRG-LAB — до 64 КиБ
NUM_MAX = 1e9


class BadCommand(ValueError):
    """Тело module-cmd не прошло проверку: result — bad-request | unknown-op; req — из тела, если он верен (иначе 0)."""

    def __init__(self, result, text, req=0, op=""):
        super().__init__(text)
        self.result, self.text, self.req, self.op = result, text, req, op


def _req_ok(x):
    return isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= REQ_MAX


def check_command(body):
    """Тело module-cmd → (op, args, req). Иначе BadCommand: лишние ключи тела или args, неверный req, тип значения,
    неизвестный сценарий — bad-request; op не из OPS — unknown-op."""
    if not isinstance(body, dict):
        raise BadCommand("bad-request", "body is not an object")
    req = body.get("req")
    req_ok = req if _req_ok(req) else 0
    op = body.get("op")
    op_ok = op if op in OPS else ""
    extra = sorted(set(body) - {"req", "op", "args"})
    if extra:
        raise BadCommand("bad-request", f"unknown body keys: {', '.join(map(str, extra))[:80]}", req_ok, op_ok)
    if not _req_ok(req):
        raise BadCommand("bad-request", "req must be an integer 0…2^31-1", 0, op_ok)
    if not isinstance(op, str):
        raise BadCommand("bad-request", "op must be a string", req)
    if op not in OPS:
        raise BadCommand("unknown-op", f"unknown op {op[:32]!r}", req)
    args = body.get("args", {})
    if not isinstance(args, dict):
        raise BadCommand("bad-request", "args must be an object", req, op)
    if op in ("status", "reinit", "reset"):
        if args:
            raise BadCommand("bad-request", f"{op}: no arguments allowed", req, op)
    elif op == "configure":
        extra = sorted(set(args) - set(CONFIGURE_KEYS))
        if extra or not args:
            raise BadCommand("bad-request", "configure: allowed keys are " + ", ".join(CONFIGURE_KEYS)
                             + (f" (unknown: {', '.join(map(str, extra))[:60]})" if extra else ""), req, op)
        for k, v in args.items():
            kind = CONFIGURE_KEYS[k]
            if not isinstance(v, kind) or (kind is str and not PROFILE_ID.fullmatch(v)):
                raise BadCommand("bad-request", f"configure: bad value for {k}", req, op)
    else:                                            # lab
        if set(args) != {"scenario"} or args["scenario"] not in LAB_SCENARIOS:
            raise BadCommand("bad-request", "lab: args must be {\"scenario\": one of " + ", ".join(LAB_SCENARIOS)
                             + "}", req, op)
    return op, dict(args), req


# ---------- status.module ----------
def _num(x, *, integer=False, null=False):
    if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x:      # x == x: не NaN
        x = min(max(x, 0), NUM_MAX)
        return int(x) if integer else round(float(x), 3)
    return None if null else 0


def _text(x, n):
    return x[:n] if isinstance(x, str) else ""


def _profile_brief(p):
    if not isinstance(p, dict):
        return {"id": "", "name": ""}
    pid = p.get("id")
    return {"id": pid if isinstance(pid, str) and PROFILE_ID.fullmatch(pid) else "", "name": _text(p.get("name"), 64)}


def _profile(p):
    out = _profile_brief(p)
    if isinstance(p, dict):
        out.update(family=_text(p.get("family"), 16), interface=_text(p.get("interface"), 16))
    else:
        out.update(family="", interface="")
    return out


def _backend(b):
    b = b if isinstance(b, dict) else {}
    last = b.get("last_exit")
    return {"kind": b.get("kind") if b.get("kind") in BACKEND_KINDS else "", "lab": b.get("lab") is True,
            "state": b.get("state") if b.get("state") in BACKEND_STATES else "",
            "pid": _num(b.get("pid"), integer=True), "restarts": _num(b.get("restarts"), integer=True),
            "restart_limit": _num(b.get("restart_limit"), integer=True),
            "last_exit": last if isinstance(last, int) and not isinstance(last, bool) and -255 <= last <= 255 else None}


def _identity(i):
    if not isinstance(i, dict):
        return None
    return {k: _text(i.get(k), 32) for k in ("vendor", "model", "firmware", "serial")}


def _error(e):
    if not isinstance(e, dict) or e.get("code") not in ERROR_CODES:
        return None
    return {"code": e["code"], "text": _text(e.get("text"), 200), "since_s": _num(e.get("since_s")),
            "state": e.get("state") if e.get("state") in STATES else "", "active": e.get("active") is True}


def _recovery(r):
    r = r if isinstance(r, dict) else {}
    return {"auto": r.get("auto") is True, "attempt": _num(r.get("attempt"), integer=True),
            "next_s": _num(r.get("next_s"), null=True), "exhausted": r.get("exhausted") is True}


def _settings(s):
    s = s if isinstance(s, dict) else {}
    pid = s.get("profile")
    return {"enabled": s.get("enabled") is True, "profile": pid if isinstance(pid, str) and PROFILE_ID.fullmatch(pid)
            else "", "backend": s.get("backend") if s.get("backend") in BACKEND_KINDS else "",
            "auto_init": s.get("auto_init") is True, "auto_recover": s.get("auto_recover") is True,
            "revision": _text(s.get("revision"), 16),
            "source": s.get("source") if s.get("source") in SETTINGS_SOURCES else "", "notice": _text(s.get("notice"), 64)}


def _diag(d):
    d = d if isinstance(d, dict) else {}
    return {"init_ms": _num(d.get("init_ms"), integer=True), "pong_age_s": _num(d.get("pong_age_s"), null=True),
            "faults": _num(d.get("faults"), integer=True), "recoveries": _num(d.get("recoveries"), integer=True),
            "transitions": _num(d.get("transitions"), integer=True)}


def _history(h):
    if not isinstance(h, list):
        return []
    out = []
    for e in h[:HISTORY_MAX]:
        if not isinstance(e, dict) or e.get("state") not in STATES:
            continue
        out.append({"seq": _num(e.get("seq"), integer=True),
                    "from": e.get("from") if e.get("from") in STATES else "", "state": e["state"],
                    "reason": _text(e.get("reason"), 32),
                    "code": e.get("code") if e.get("code") in ERROR_CODES else "", "ago_s": _num(e.get("ago_s"))})
    return out


def _cmd(c):
    if not isinstance(c, dict) or c.get("result") not in RESULTS or not _req_ok(c.get("req")):
        return None
    return {"req": c["req"], "op": c.get("op") if c.get("op") in OPS else "", "result": c["result"],
            "error": _text(c.get("error"), 200)}


def sanitize_module(v):
    """status.module второй стороны → dict раздела 6.2 или None (нет объекта, v != 1, неизвестное состояние).

    Отсутствующие и неверные поля заменяются значениями по умолчанию; output, отличный от "none", — "unknown";
    cmd — только при верных req и result (op не из OPS — пустая строка)."""
    if not isinstance(v, dict) or v.get("v") != STATUS_V or isinstance(v.get("v"), bool) or v.get("state") not in STATES:
        return None
    profiles = v.get("profiles")
    out = {"v": STATUS_V, "state": v["state"], "reason": _text(v.get("reason"), 32), "since_s": _num(v.get("since_s")),
           "seq": _num(v.get("seq"), integer=True), "profile": _profile(v.get("profile")),
           "profiles": [_profile_brief(p) for p in profiles[:PROFILES_MAX]] if isinstance(profiles, list) else [],
           "backend": _backend(v.get("backend")), "identity": _identity(v.get("identity")),
           "error": _error(v.get("error")), "recovery": _recovery(v.get("recovery")),
           "settings": _settings(v.get("settings")), "diag": _diag(v.get("diag")),
           "output": OUTPUT if v.get("output") == OUTPUT else "unknown", "history": _history(v.get("history"))}
    cmd = _cmd(v.get("cmd"))
    if cmd is not None:
        out["cmd"] = cmd
    return out
