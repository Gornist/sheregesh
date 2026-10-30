#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Приёмка S8 «выход CRSF удалённого блока» (ADR-0014 §4, CR-01…CR-10) на двух платах с эмулятором ELRS TX вместо
модуля, ≈ 4–6 мин (docs/acceptance/s8-crsf/PLAN.md). С машины владельца по SSH от sheregesh-ui, без sudo.

  python3 tools/probe/s8_crsf_accept.py --plan                                    # только план, без SSH
  python3 tools/probe/s8_crsf_accept.py --console 192.168.1.92 --remote 192.168.1.171 --out DIR \\
      [--only CR-04,CR-07,…] [--window 10] [--no-freeze]

К pi1 по USB подключена RadioMaster Boxer (EdgeTX, USB Joystick), как в S7, или (--sticks-emu) — эмулятор стиков
`sheregesh_emulators.sticks_uinput` на pi1 от root (виртуальное устройство uinput, профиль Boxer, заданные оси):
источник стиков записывается в summary («sticks_source»). Модуля нет: backend `crsf-pty-lab` —
эмулятор ELRS TX (`sheregesh_emulators.elrs_tx`) на псевдотерминале, дочерний процесс sheregesh-remote-net.

Все команды и выборки пульта — один опросчик на pi1 (HELPER, по SSH через `python3 -c`, без файлов на плате, время —
часы pi1): `python3 -m sheregesh.link.modulectl … --via console` (configure backend, lab silent/heal — тот же путь, что
кнопки экрана «Модуль»), команда `sticks` локального канала пульта (status | enable с "confirmed": true | disable),
`module status` того же канала (status.module после sanitize: state, output, settings.backend, crsf = снимок
CrsfOutput, crsf.emu — что принял эмулятор). Сокет пульта — sheregesh.link.videoselect.socket_path(), как в S7.
Блок: link-status.json pi2 (module, control), журнал sheregesh-remote-net (переходы `module: A -> B`, `control: A -> B`,
`crsf: A -> B`, строки эмулятора) — интервалы на блоке по часам pi2. Часы плат не сравниваются.

  CR-01  configure backend=crsf-pty-lab → READY ≤ 5 с, device «RM Nomad X-Band (emu)»
  CR-02  crsf.synced, period_us = период эмулятора (20 000 мкс ± 1 %)
  CR-03  передача стиков выключена → output neutral, каналы эмулятора = 992, взведение false
  CR-04  sticks enable → output live; каналы эмулятора 1–8 = оси пульта (−1000…1000 → 992 ± 820) ±1 шаг 11 бит (и не
         меньше шума осей за окно), 9–16 = 992, взведение true. Стики в окне НЕ ТРОГАТЬ
  CR-05  то же окно: эмулятор принимает 0x16 с частотой 50 Гц ± 2 %, p99 джиттера CrsfOutput ≤ 2 мс, эмулятора ≤ 5 мс
         (включает его собственные задержки, ADR-0014 §4),
         ошибок CRC за окно 0 (обе стороны)
  CR-06  при live: systemctl stop sheregesh-console (страховочный start) → FAILSAFE → `crsf: live -> neutral`
         ≤ 600 мс от последнего кадра (журнал pi2); пульт запущен → output neutral, эмулятор получает центр
  CR-07  lab silent → FAULT ≤ no_response_s + 1 с, output none; lab heal → READY
  CR-08  (а) заморозка агента pi2 (systemctl kill --kill-whom=main SIGSTOP на 2,5 с, страховочный SIGCONT): эмулятор
         `uart-disconnected` ≤ 1,5 с (журнал); (б) systemctl restart sheregesh-remote-net → READY и output neutral
  CR-09  configure backend=lab-emulator → поведение S6: READY, output none, процесс jrmodule один, elrs_tx нет
  CR-10  OTA-state как JR-10 (s6_jr_accept.boards/judge_ota)

Из s6_jr_accept импортом: preflight, boards/judge_ota, start_guard/systemctl/safety_start, процессы блока; из
s7_sticks_accept: Stand (выборки, sticks), restore, разбор журнала сторожа. Не трогает OTA, слоты, видео, образ, юниты,
polkit. systemctl: stop/start sheregesh-console (CR-06), restart sheregesh-remote-net (CR-08), kill SIGSTOP/SIGCONT
главному процессу блока (CR-08, правило 50 — глагол kill), start остановленной службы в finally. finally: SIGCONT
блоку, службы запущены, backend lab-emulator, передача стиков выключена (блок снова IDLE, если был). Вердикты PASS |
FAIL | ERROR | BLOCKED | SKIP; коды выхода 0 PASS, 1 FAIL, 2 ERROR, 3 BLOCKED.
"""
import argparse
import base64
import json
import math
import re
import shlex
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/probe"))
sys.path.insert(0, str(ROOT / "tools/deploy/ota-check"))
import s6_jr_accept as S6  # noqa: E402
import s7_sticks_accept as S7  # noqa: E402

E = S6.E
STEPS = ("CR-01", "CR-02", "CR-03", "CR-04", "CR-05", "CR-06", "CR-07", "CR-08", "CR-09", "CR-10")
BLOCKED = S6.BLOCKED
REMOTE_UNIT, CONSOLE_UNIT = S6.REMOTE_UNIT, S6.CONSOLE_UNIT
REL = S6.REL
BACKEND_LAB, BACKEND_UART, BACKEND_S6 = "crsf-pty-lab", "crsf-uart", "lab-emulator"
EMU_NAME = "RM Nomad X-Band (emu)"
ELRS_MARK = "sheregesh_emulators.elrs_tx"
CENTER, CH_MIN, CH_MAX, SPAN = 992, 172, 1811, 820          # ADR §3.1: −1000…1000 → 992 ± 820, 11 бит 172…1811
AXES, CHANNELS = 8, 16
# ADR §4 и свои: ready_s (CR-01), live_s (ACTIVE ≤ 1 с + статус модуля к пульту ≤ 1 с + опрос), sync_wait_s (sync раз
# в 200 мс), heal_s/s6_ready_s (как recover_s/ready_s S6), restart_ready_s (как service_restart_s S6), freeze_s (меньше
# WatchdogSec=15 и тайм-аута канала пульта), no_response_s — профиля radiomaster-nomad (в телеметрии его нет).
LIMITS = {"ready_s": 5.0, "period_us": 20000, "period_tol": 0.01, "rate_hz": 50.0, "rate_tol": 0.02,
          "jitter_p99_us": 2000, "emu_jitter_p99_us": 5000, "failsafe_ms": 600.0, "fault_extra_s": 1.0, "no_response_s": 2.0,
          "uart_disc_s": 1.5, "window_s": 10.0, "settle_s": 1.0, "live_s": 3.0, "sync_wait_s": 3.0,
          "neutral_s": 3.0, "heal_s": 20.0, "s6_ready_s": 15.0, "restart_ready_s": 30.0, "freeze_s": 2.5,
          "stop_detect_s": 10.0, "console_start_s": 60.0}
STEP_TOL = 1                       # CR-04: ±1 шаг 11 бит
REST_SPREAD_MAX = S7.REST_SPREAD_MAX
POLL_S = 0.1                       # опрос module status (статус модуля к пульту — не чаще раза в 0,5 с)
LOG = None

# Пути в ответах канала пульта: module status → {"result", "module": M}, M = ModuleView.snapshot() (support, stale,
# session, telemetry = status.module после sanitize_module). Блок crsf — снимок CrsfOutput (ADR §3.1).
P = {"support": "module.support", "stale": "module.stale", "state": "module.telemetry.state",
     "output": "module.telemetry.output", "backend": "module.telemetry.settings.backend", "seq": "module.telemetry.seq",
     "history": "module.telemetry.history", "crsf": "module.telemetry.crsf", "profile": "module.telemetry.profile"}
# Что принял эмулятор (последние каналы, взведение, частота, джиттер, CRC): первый найденный из путей внутри crsf.
EMU_KEYS = ("emu", "emulator", "peer", "lab")
REQUESTS = {"sticks": {"cmd": "sticks", "op": "status"},
            "enable": {"cmd": "sticks", "op": "enable", "confirmed": True},
            "disable": {"cmd": "sticks", "op": "disable"},
            "mod": {"cmd": "module", "op": "status"}}

# Опросчик на pi1: argv[1] — запросы канала пульта, argv[2] — программа [{"op": имя запроса} | {"op": "ctl", "args":
# "configure backend=…"} (python3 -m sheregesh.link.modulectl ARGS --via console) | {"op": "poll", "s": с, "period": с,
# "ask": [имена], "until": {"имя:путь": значение}}]. Строка на ответ: {"t0", "t" (time.time pi1), "op", "r"}.
HELPER = r'''
import json, shlex, subprocess, sys, time
from sheregesh.link.videoselect import ConsoleAbsent, ask_console, socket_path
REQ, PROG = json.loads(sys.argv[1]), json.loads(sys.argv[2])
path = socket_path()
def get(d, p):
    for k in p.split("."):
        d = d.get(k) if isinstance(d, dict) else None
    return d
def slim(r):
    tel = get(r, "module.telemetry")
    if isinstance(tel, dict):
        tel.pop("profiles", None)
    lat = get(r, "sticks.latency")
    if isinstance(lat, dict):
        lat.pop("recent_ms", None)
    return r
def out(t0, op, r):
    print(json.dumps({"t0": round(t0, 4), "t": round(time.time(), 4), "op": op, "r": slim(r)}, ensure_ascii=False),
          flush=True)
    return r
def ask(op):
    t0 = time.time()
    try:
        r = ask_console(path, REQ[op], REQ[op].get("wait", 0) + 5.0)
    except ConsoleAbsent as e:
        r = {"result": "not-connected", "detail": str(e)}
    except (OSError, ValueError) as e:
        r = {"result": "error", "detail": type(e).__name__ + ": " + str(e)}
    return out(t0, op, r)
def ctl(args):
    t0 = time.time()
    p = subprocess.run([sys.executable, "-m", "sheregesh.link.modulectl", *shlex.split(args), "--via", "console"],
                       capture_output=True, text=True)
    dec, text, r = json.JSONDecoder(), p.stdout, None
    i = text.find("{")
    while i >= 0 and r is None:
        try:
            v, _e = dec.raw_decode(text[i:])
            r = v if isinstance(v, dict) and "result" in v else None
        except ValueError:
            pass
        i = text.find("{", i + 1)
    r = r or {"result": "error", "detail": (p.stderr or p.stdout)[-300:]}
    r["rc"], r["args"] = p.returncode, args
    return out(t0, "ctl", r)
for st in PROG:
    if st["op"] == "ctl":
        ctl(st["args"])
        continue
    if st["op"] != "poll":
        ask(st["op"])
        continue
    end, nxt = time.time() + st["s"], time.time()
    while True:
        last = {op: ask(op) for op in st.get("ask", ["mod"])}
        u = st.get("until")
        if (u and all(get(last.get(k.split(":", 1)[0]), k.split(":", 1)[1]) == v for k, v in u.items())) or \
                time.time() >= end:
            break
        nxt += st.get("period", 0.1)
        time.sleep(max(0.0, nxt - time.time()))
'''


def log(msg):
    line = time.strftime("%H:%M:%S") + " " + msg
    print(line, flush=True)
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------------------------------------------------------------- чистые функции (тесты: test_s8_crsf_accept.py)


def verdict_of(results):
    return S6.verdict_of(results)


agg = S6.agg
num = S7.num
get = S7.get
percentile = S7.percentile


def g(r, key):
    return get(r, P[key])


def helper_cmd(prog, requests=None):
    """Команда SSH для pi1: опросчик HELPER из выпуска пульта (DEV deploy или образ), без файлов на плате."""
    b64 = base64.b64encode(HELPER.encode()).decode()
    return (f"{REL}; PYTHONPATH=$C/software python3 -c \"$(echo {b64} | base64 -d)\" "
            f"{shlex.quote(json.dumps(requests or REQUESTS))} {shlex.quote(json.dumps(prog))} 2>&1")


parse_rows = S7.parse_rows


def replies(rows, op, t_from=None, t_to=None):
    """(t, ответ) строк op с result ok, полученных в [t_from, t_to] по часам pi1."""
    return [(r["t"], r["r"]) for r in rows if r.get("op") == op and r["r"].get("result") == "ok"
            and (t_from is None or r["t"] >= t_from) and (t_to is None or r["t"] <= t_to)]


def mods(rows, t_from=None, t_to=None):
    """(t, ответ module status) с support ok и свежей телеметрией."""
    return [(t, r) for t, r in replies(rows, "mod", t_from, t_to)
            if g(r, "support") == "ok" and not g(r, "stale") and isinstance(get(r, "module.telemetry"), dict)]


def op_row(rows, op):
    return next((r for r in rows if r.get("op") == op), None)


def crsf_of(r):
    c = g(r, "crsf")
    return c if isinstance(c, dict) else {}


def emu_of(r):
    """Что принял эмулятор (crsf.emu и т. п.) — {} если блок этого не сообщает."""
    c = crsf_of(r)
    for k in EMU_KEYS:
        if isinstance(c.get(k), dict):
            return c[k]
    return {}


def backend_of(r):
    b = g(r, "backend")
    return b if isinstance(b, str) else None


def mode_of(r):
    """Режим выхода: telemetry.output и crsf.mode должны совпадать; расхождение — строка «a/b»."""
    o, m = g(r, "output"), crsf_of(r).get("mode")
    if m is None or m == o:
        return o
    return f"{o}/{m}"


def device_name(r):
    return get(crsf_of(r), "device.name")


def to_crsf(v):
    """Ось −1000…1000 → отсчёт CRSF (ADR §3.1: 992 ± 820, 172…1811), как sheregesh.crsf.frames.axis_to_crsf:
    округление от нуля."""
    x = max(-1000, min(1000, int(v)))
    q = (abs(x) * SPAN + 500) // 1000
    return max(CH_MIN, min(CH_MAX, CENTER + (q if x >= 0 else -q)))


def emu_channels(e):
    """Каналы, принятые эмулятором, в отсчётах CRSF (11 бит) → список int или None. В status.module.crsf.emu.ch они в
    шкале пульта −1000…1000 (module/crsf._emu_view, wire._crsf_emu) — переводятся обратно (to_crsf), сравнение —
    в шагах 11 бит."""
    v = (e or {}).get("ch")
    if isinstance(v, list) and len(v) >= AXES and all(num(x) for x in v):
        return [to_crsf(x) for x in v]
    return None


def raw_scale_hint(e):
    """Похоже, что в crsf.emu.ch пришли сырые отсчёты 11 бит (992 = центр) вместо шкалы −1000…1000."""
    v = (e or {}).get("ch")
    return isinstance(v, list) and len(v) >= AXES and all(num(x) and CH_MIN <= x <= 1000 for x in v) and \
        sum(1 for x in v if x == CENTER) >= AXES


def time_to(rows, t_from, pred, op="mod"):
    """Секунды от t_from (pi1) до первого ответа op (момент получения — верхняя оценка), где pred(ответ)."""
    for t, r in (mods(rows, t_from) if op == "mod" else replies(rows, op, t_from)):
        if pred(r):
            return round(t - t_from, 3)
    return None


def is_ready(backend=None):
    return lambda r: g(r, "state") == "READY" and (backend is None or backend_of(r) in (backend, None))


def distinct_emu(samples):
    """Снимки эмулятора без повторов (опрос 10 Гц видит один статус модуля несколько раз)."""
    seen, out = set(), []
    for t, r in samples:
        e = emu_of(r)
        key = json.dumps(e, sort_keys=True)
        if e and key not in seen:
            seen.add(key)
            out.append((t, r))
    return out


# Журнал блока (journalctl -o short-unix): переходы менеджера модуля, выхода CRSF, строки эмулятора, команды.
MODULE_TR = re.compile(r"^(\d+(?:\.\d+)?)\s.*?module: (\w+) -> (\w+) \(([\w-]+)\)(.*)$")
CRSF_TR = re.compile(r"^(\d+(?:\.\d+)?)\s.*?crsf: (none|neutral|live) -> (none|neutral|live)\b(.*)$")
STAMP = re.compile(r"^(\d+(?:\.\d+)?)\s(.*)$")


def parse_module_transitions(text):
    out = []
    for ln in (text or "").splitlines():
        m = MODULE_TR.match(ln.strip())
        if m:
            out.append({"t": float(m.group(1)), "from": m.group(2), "to": m.group(3), "reason": m.group(4),
                        "rest": m.group(5).strip()})
    return sorted(out, key=lambda x: x["t"])


def parse_crsf_transitions(text):
    out = []
    for ln in (text or "").splitlines():
        m = CRSF_TR.match(ln.strip())
        if m:
            out.append({"t": float(m.group(1)), "from": m.group(2), "to": m.group(3), "rest": m.group(4).strip()})
    return sorted(out, key=lambda x: x["t"])


def first_line(text, pattern, t_from=None):
    """Время (pi2) первой строки журнала с pattern (regex) не раньше t_from → (t, строка) или (None, None)."""
    rx = re.compile(pattern)
    for ln in (text or "").splitlines():
        m = STAMP.match(ln.strip())
        if m and rx.search(m.group(2)) and (t_from is None or float(m.group(1)) >= t_from):
            return float(m.group(1)), m.group(2)
    return None, None


def neutral_delay(fs, tr):
    """CR-06: мс от последнего кадра пульта до `crsf: live -> neutral` по часам pi2. fs — переход сторожа в FAILSAFE
    (age_ms — возраст последнего кадра в момент перехода), tr — переход выхода. None — нечем считать."""
    if not fs or not tr or not num(fs.get("age_ms")):
        return None
    return round((tr["t"] - (fs["t"] - fs["age_ms"] / 1000.0)) * 1000)


def counter_delta(samples, key):
    """Прирост счётчика за окно (последний − первый) по снимкам dict; None — нет значений."""
    v = [s.get(key) for s in samples if num(s.get(key))]
    return (v[-1] - v[0]) if v else None


def echo_check(sticks_rows, emu_samples):
    """CR-04: оси пульта (неподвижные стики) против каналов эмулятора. → {samples, spread, axes_ref, expect, emu (число
    снимков с каналами), diff_max (1–8), tail (снимков с каналами 9–16 — блок S8 сообщает только 1–8), tail_bad
    (из них 9–16 ≠ 992), tol}."""
    axes = [S7.g(S7.sticks_of(r), "axes") for _t, r in sticks_rows]
    axes = [a for a in axes if isinstance(a, list) and len(a) == AXES and all(num(x) for x in a)]
    res = {"samples": len(axes), "spread": None, "axes_ref": None, "expect": None, "emu": 0, "diff_max": None,
           "tail": 0, "tail_bad": 0, "tol": STEP_TOL}
    if not axes:
        return res
    cols = list(zip(*axes))
    res["spread"] = max(max(c) - min(c) for c in cols)
    res["axes_ref"] = [statistics.median_low(c) for c in cols]
    res["expect"] = [to_crsf(v) for v in res["axes_ref"]]
    # Шум оси в покое (прогон S7 2026-10-26: 3) в шкале CRSF — до ceil(разброс · 0,82): допуск не меньше него.
    res["tol"] = STEP_TOL + math.ceil(res["spread"] * SPAN / 1000)
    diffs = []
    for _t, r in emu_samples:
        ch = emu_channels(emu_of(r))
        if ch is None:
            continue
        res["emu"] += 1
        diffs.append(max(abs(a - b) for a, b in zip(ch[:AXES], res["expect"])))
        if len(ch) > AXES:
            res["tail"] += 1
            res["tail_bad"] += any(x != CENTER for x in ch[AXES:CHANNELS])
    res["diff_max"] = max(diffs) if diffs else None
    return res


# ---------------------------------------------------------------- суждения


def judge_enable(m):
    """CR-01: configure ok; READY ≤ ready_s (по журналу блока, иначе верхняя оценка по пульту); backend crsf-pty-lab;
    device = «RM Nomad X-Band (emu)»."""
    if m.get("result") is None:
        return "ERROR", f"опросчик на pi1 не ответил: {m.get('detail') or ''}"
    if m.get("result") != "ok":
        return "FAIL", f"configure backend={BACKEND_LAB}: {m.get('result')!r} {m.get('detail') or ''}"
    p = []
    rs = m.get("ready_block_s") if num(m.get("ready_block_s")) else m.get("ready_s")
    src = "журнал pi2" if num(m.get("ready_block_s")) else "пульт, верхняя оценка"
    if rs is None:
        p.append(f"READY не наблюдался за {m.get('timeout_s')} с (состояние {m.get('state')!r})")
    elif rs > LIMITS["ready_s"]:
        p.append(f"READY через {rs} с ({src}; предел {LIMITS['ready_s']:.0f})")
    if m.get("backend") not in (BACKEND_LAB, None):
        p.append(f"settings.backend {m.get('backend')!r}")
    if m.get("device") != EMU_NAME:
        p.append(f"device {m.get('device')!r}, ожидалось «{EMU_NAME}»")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"READY за {rs} с ({src}; пульт {m.get('ready_s')} с), device «{m['device']}», backend "
        f"{m.get('backend') or '(в телеметрии нет)'}")


def judge_sync(snaps, period_us=LIMITS["period_us"]):
    """CR-02: снимки crsf после READY: есть synced; последние (до 3) — synced и period_us = period_us ± period_tol."""
    snaps = [c for c in snaps if c]
    if not snaps:
        return "ERROR", "нет снимков crsf (блок не S8 или модуль не READY)"
    synced = [c for c in snaps if c.get("synced") is True]
    if not synced:
        return "FAIL", f"synced ни в одном из {len(snaps)} снимков (period_us {snaps[-1].get('period_us')!r})"
    tail, tol = snaps[-3:], period_us * LIMITS["period_tol"]
    p = []
    for c in tail:
        if c.get("synced") is not True:
            p.append(f"synced {c.get('synced')!r} после синхронизации")
        elif not num(c.get("period_us")) or abs(c["period_us"] - period_us) > tol:
            p.append(f"period_us {c.get('period_us')!r} (≠ {period_us} ± {tol:.0f})")
    return ("FAIL" if p else "PASS"), "; ".join(sorted(set(p))) or (
        f"synced, period_us {tail[-1]['period_us']} (≈ {period_us} ± 1 %), sync_age_ms {tail[-1].get('sync_age_ms')}")


def judge_neutral(samples):
    """CR-03 (и центр после CR-06): output neutral во всех снимках после первого neutral + settle_s; каналы
    эмулятора все = 992; взведение false. samples — (t, ответ module status)."""
    if not samples:
        return "ERROR", "нет снимков module status"
    t1 = next((t for t, r in samples if mode_of(r) == "neutral"), None)
    if t1 is None:
        return "FAIL", f"output neutral не наблюдался: {sorted({str(mode_of(r)) for _t, r in samples})}"
    after = [(t, r) for t, r in samples if t >= t1 + LIMITS["settle_s"]] or [x for x in samples if x[0] >= t1]
    p = []
    bad = sorted({str(mode_of(r)) for _t, r in after if mode_of(r) != "neutral"})
    if bad:
        p.append(f"output {bad} после neutral")
    emus = [emu_of(r) for _t, r in distinct_emu(after)]
    chans = [emu_channels(e) for e in emus if emu_channels(e)]
    if not chans:
        p.append("эмулятор не сообщает принятые каналы (crsf.emu.ch)")
    else:
        off = [ch for ch in chans if any(x != CENTER for x in ch)]
        if off:
            p.append(f"каналы эмулятора не центр в {len(off)} из {len(chans)}: {off[-1][:AXES]}" +
                     (" — crsf.emu.ch = 992…: сырые отсчёты 11 бит вместо шкалы −1000…1000 (elrs_tx «ch» против "
                      "module/crsf._emu_view)" if any(raw_scale_hint(e) for e in emus) else ""))
    armed = [e.get("armed") for e in emus if "armed" in e]
    if any(a is not False for a in armed):
        p.append(f"взведение {sorted(set(map(str, armed)))}")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"output neutral, каналы эмулятора = {CENTER} ({len(chans)} снимков), взведение false")


def judge_live(m):
    """CR-04: enable ok; output live ≤ live_s; в окне все снимки live; стики неподвижны (разброс ≤ REST_SPREAD_MAX);
    каналы эмулятора 1–8 = to_crsf(оси) ± tol, 9–16 = 992; взведение true."""
    if m.get("enable_result") is None:
        return "ERROR", f"опросчик на pi1 не ответил: {m.get('enable_detail') or ''}"
    if m.get("enable_result") != "ok":
        return "FAIL", f"sticks enable: {m.get('enable_result')!r} {m.get('enable_detail') or ''}"
    if m.get("live_s") is None or m["live_s"] > LIMITS["live_s"]:
        return "FAIL", f"output live через {m.get('live_s')} с (предел {LIMITS['live_s']:.0f}; режимы " \
                       f"{m.get('modes')})"
    e = m.get("echo") or {}
    if not e.get("samples"):
        return "ERROR", "нет выборок осей пульта"
    if e["spread"] > REST_SPREAD_MAX:
        return "ERROR", f"оси пульта менялись на {e['spread']} за окно (> {REST_SPREAD_MAX}) — стики трогали"
    p = []
    if m.get("not_live"):
        p.append(f"в окне output не live: {m['not_live']}")
    if not e.get("emu"):
        p.append("эмулятор не сообщает принятые каналы (crsf.emu.ch)")
    else:
        if e["diff_max"] > e["tol"]:
            p.append(f"каналы эмулятора 1–8 отличаются от осей пульта до {e['diff_max']} (±{e['tol']}); ожидалось "
                     f"{e['expect']}")
        if e.get("tail_bad"):
            p.append(f"каналы 9–16 ≠ {CENTER} в {e['tail_bad']} снимках")
    armed = m.get("armed") or []
    if not armed:
        p.append("эмулятор не сообщает взведение")
    elif any(a is not True for a in armed):
        p.append(f"взведение {sorted(set(map(str, armed)))}, ожидалось true")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"live за {m['live_s']} с; каналы эмулятора = оси ±{e['diff_max']} (допуск {e['tol']}, {e['emu']} снимков), "
        + (f"9–16 = {CENTER}, " if e.get("tail") else "9–16 блок не сообщает (только оси 1–8), ")
        + f"взведение true; оси {e['axes_ref']}")


def judge_rate(m):
    """CR-05: rate_hz эмулятора 50 ± 2 % во всех снимках окна; jitter_us_p99 CrsfOutput ≤ 2000 мкс; прирост
    crc_errors за окно — 0 у блока и у эмулятора.

    Джиттер, измеренный эмулятором (Python-процесс на той же pi2, чтение pty), включает его собственные задержки
    пробуждения и ограничивает сверху то, что увидит модуль; настоящий модуль метит время приёма на своём МК, поэтому
    норма 2 мс — для отправителя, а для эмулятора — четверть периода (5 мс). Прогон 2026-10-26: отправитель
    0,7–1,3 мс, эмулятор 1,2–3,6 мс."""
    if m.get("error"):
        return "ERROR", m["error"]
    p, rates = [], [x for x in m.get("emu_rates") or [] if num(x)]
    lo, hi = LIMITS["rate_hz"] * (1 - LIMITS["rate_tol"]), LIMITS["rate_hz"] * (1 + LIMITS["rate_tol"])
    if not rates:
        return "ERROR", "эмулятор не сообщает частоту принятых 0x16 (crsf.emu.rate_hz)"
    if min(rates) < lo or max(rates) > hi:
        p.append(f"частота 0x16 у эмулятора {min(rates)}…{max(rates)} Гц (≠ {LIMITS['rate_hz']:.0f} ± 2 %)")
    for src, key, lim in (("CrsfOutput", "out_jitter", "jitter_p99_us"), ("эмулятор", "emu_jitter", "emu_jitter_p99_us")):
        j = [x for x in m.get(key) or [] if num(x)]
        if key == "out_jitter" and not j:
            p.append("нет crsf.jitter_us_p99")
        elif j and max(j) > LIMITS[lim]:
            p.append(f"{src}: p99 джиттера до {max(j)} мкс (≤ {LIMITS[lim]})")
    for src, key in (("блок", "out_crc"), ("эмулятор", "emu_crc")):
        d = m.get(key)
        if d is None and key == "out_crc":
            p.append("нет crsf.crc_errors")
        elif d:
            p.append(f"{src}: ошибок CRC за окно {d}")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"0x16 у эмулятора {min(rates)}…{max(rates)} Гц ({len(rates)} снимков), p99 джиттера ≤ "
        f"{max(x for x in m['out_jitter'] if num(x))} мкс (эмулятор ≤ "
        f"{max((x for x in m.get('emu_jitter') or [] if num(x)), default='—')} мкс), CRC без ошибок; "
        f"кадров отправлено {m.get('frames_sent')}")


def judge_failsafe(m):
    """CR-06: stop пульта принят; на блоке FAILSAFE (timeout | link-lost) и `crsf: live -> neutral`; от последнего
    кадра до neutral ≤ failsafe_ms (журнал pi2); пульт снова отвечает; затем output neutral и центр у эмулятора."""
    if m.get("error"):
        return "ERROR", m["error"]
    if m.get("stop_rc") != 0:
        return "ERROR", f"systemctl stop {CONSOLE_UNIT}: rc {m.get('stop_rc')} {m.get('stop_out', '')}"
    if not m.get("journal_ok"):
        return "ERROR", "журнал sheregesh-remote-net недоступен sheregesh-ui — момент перехода не измерить"
    p = []
    fs, tr = m.get("failsafe") or {}, m.get("neutral") or {}
    if not fs:
        p.append("в журнале нет перехода сторожа в FAILSAFE")
    elif fs.get("reason") not in ("timeout", "link-lost"):
        p.append(f"причина FAILSAFE {fs.get('reason')!r}, ожидалась timeout или link-lost")
    if not tr:
        p.append("в журнале нет `crsf: live -> neutral`")
    elif m.get("delay_ms") is None:
        p.append("задержку посчитать нечем (нет age_ms в записи о FAILSAFE)")
    elif m["delay_ms"] > LIMITS["failsafe_ms"]:
        p.append(f"neutral через {m['delay_ms']} мс от последнего кадра (≤ {LIMITS['failsafe_ms']:.0f})")
    if m.get("start_rc") != 0:
        p.append(f"systemctl start {CONSOLE_UNIT}: rc {m.get('start_rc')}")
    elif m.get("console_back_s") is None:
        p.append(f"пульт не ответил за {LIMITS['console_start_s']:.0f} с после start")
    else:
        v, d = m.get("center") or ("ERROR", "центр не проверялся")
        if v != "PASS":
            p.append(f"после перезапуска пульта: {d}")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"FAILSAFE ({fs['reason']}), neutral через {m['delay_ms']} мс от последнего кадра; пульт снова через "
        f"{m['console_back_s']} с, эмулятор получает центр")


def judge_silent(m):
    """CR-07: lab silent ok → FAULT ≤ no_response_s + 1 с (журнал pi2, иначе верхняя оценка по пульту), при FAULT
    output none; lab heal ok → READY ≤ heal_s."""
    if m.get("silent_result") != "ok":
        return "FAIL", f"lab silent: {m.get('silent_result')!r} {m.get('silent_detail') or ''}"
    p = []
    lim = (m.get("no_response_s") or LIMITS["no_response_s"]) + LIMITS["fault_extra_s"]
    fs = m.get("fault_block_s") if num(m.get("fault_block_s")) else m.get("fault_s")
    src = "журнал pi2" if num(m.get("fault_block_s")) else "пульт, верхняя оценка"
    if fs is None:
        p.append(f"FAULT не наблюдался (состояние {m.get('state')!r})")
    elif fs > lim:
        p.append(f"FAULT через {fs} с ({src}; предел {lim:g})")
    if m.get("fault_modes") and any(x != "none" for x in m["fault_modes"]):
        p.append(f"при FAULT output {sorted(set(map(str, m['fault_modes'])))}")
    elif fs is not None and not m.get("fault_modes"):
        p.append("снимков FAULT с output нет")
    if m.get("heal_result") != "ok":
        p.append(f"lab heal: {m.get('heal_result')!r}")
    elif m.get("ready_s") is None or m["ready_s"] > LIMITS["heal_s"]:
        p.append(f"после heal READY через {m.get('ready_s')} с (предел {LIMITS['heal_s']:.0f})")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"FAULT через {fs} с ({src}; предел {lim:g}), output none; heal → READY {m['ready_s']} с")


def judge_restart(m):
    """CR-08: (а) заморозка: эмулятор `uart-disconnected` ≤ uart_disc_s от SIGSTOP (журнал), после SIGCONT READY;
    (б) restart: rc 0, READY ≤ restart_ready_s, output neutral, backend прежний (crsf-pty-lab)."""
    vs, p = [], []
    f = m.get("freeze")
    if f is None:
        note = ("заморозка не выполнялась (--no-freeze): uart-disconnected проверен только в "
                "tests/system/test_crsf_chain.py")
    else:
        note = ""
        if f.get("error"):
            vs.append("ERROR")
            p.append(f"заморозка: {f['error']}")
        else:
            q = []
            if not f.get("journal_ok"):
                vs.append("ERROR")
                p.append("заморозка: журнал недоступен — uart-disconnected не наблюдаем")
            elif f.get("disc_s") is None:
                q.append(f"эмулятор не сообщил uart-disconnected за {LIMITS['freeze_s']} с заморозки")
            elif f["disc_s"] > LIMITS["uart_disc_s"]:
                q.append(f"uart-disconnected через {f['disc_s']} с (≤ {LIMITS['uart_disc_s']})")
            if f.get("ready_s") is None:
                q.append("после SIGCONT READY не наблюдался")
            vs.append("FAIL" if q else "PASS")
            p += [f"заморозка: {x}" for x in q]
    r = m.get("restart") or {}
    q = []
    if r.get("rc") != 0:
        vs.append("ERROR")
        p.append(f"systemctl restart: rc {r.get('rc')} {r.get('out', '')}")
    else:
        if r.get("ready_s") is None or r["ready_s"] > LIMITS["restart_ready_s"]:
            q.append(f"после рестарта READY через {r.get('ready_s')} с (предел {LIMITS['restart_ready_s']:.0f})")
        if r.get("mode") != "neutral":
            q.append(f"output {r.get('mode')!r}, ожидался neutral")
        if r.get("backend") not in (BACKEND_LAB, None):
            q.append(f"backend {r.get('backend')!r} после рестарта")
        vs.append("FAIL" if q else "PASS")
        p += [f"рестарт: {x}" for x in q]
    if p:
        return agg(vs), "; ".join(p + ([note] if note else []))
    ok = (f"uart-disconnected через {f['disc_s']} с заморозки, после SIGCONT READY {f['ready_s']} с; " if f else "") + \
        f"после рестарта READY {r['ready_s']} с, output neutral, backend {r.get('backend') or '(в телеметрии нет)'}"
    return agg(vs), ok + (f"; {note}" if note else "")


def judge_back(m):
    """CR-09: configure backend=lab-emulator ok → READY ≤ s6_ready_s, output none, crsf.mode none или блока нет;
    процесс jrmodule ровно один, elrs_tx нет."""
    if m.get("result") is None:
        return "ERROR", f"опросчик на pi1 не ответил: {m.get('detail') or ''}"
    if m.get("result") != "ok":
        return "FAIL", f"configure backend={BACKEND_S6}: {m.get('result')!r} {m.get('detail') or ''}"
    p = []
    if m.get("ready_s") is None or m["ready_s"] > LIMITS["s6_ready_s"]:
        p.append(f"READY через {m.get('ready_s')} с (предел {LIMITS['s6_ready_s']:.0f})")
    if m.get("backend") not in (BACKEND_S6, None):
        p.append(f"settings.backend {m.get('backend')!r}")
    bad = sorted({str(x) for x in m.get("modes") or [] if x != "none"})
    if bad or not m.get("modes"):
        p.append(f"output {bad or 'нет снимков'}, ожидался none")
    if m.get("jr") != 1:
        p.append(f"процессов jrmodule {m.get('jr')}")
    if m.get("elrs"):
        p.append(f"процессы elrs_tx остались: {m['elrs']}")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"READY {m['ready_s']} с на {BACKEND_S6}, output none, jrmodule один, elrs_tx нет")


# ---------------------------------------------------------------- стенд


def ssh(host, cmd, timeout=60):
    return E.ssh(host, cmd, timeout)


class Stand(S7.Stand):
    """Выборки ui-state pi1 и link-status pi2, sticks (s7) и опросчик HELPER: modulectl, sticks, module status."""

    def __init__(self, a):
        super().__init__(a)
        self.asks = []

    def ask(self, prog, timeout=None):
        dur = sum(st.get("s", 0) for st in prog) + sum(35 for st in prog if st["op"] == "ctl")
        code, text = ssh(self.a.console, helper_cmd(prog), timeout or dur + 40)
        rows = parse_rows(text)
        ctls = [(r["r"].get("args"), r["r"].get("result")) for r in rows if r["op"] == "ctl"]
        cmds = {r["op"]: r["r"].get("result") for r in rows if r["op"] in ("enable", "disable")}
        self.asks.append({"prog": prog, "rc": code, "replies": len(rows), "ctl": ctls, "cmds": cmds,
                          **({"tail": text[-300:]} if not rows else {})})
        log(f"  опросчик {[st.get('args', st['op']) for st in prog]}: rc {code}, ответов {len(rows)}" +
            (f", {ctls}" if ctls else "") + (f", {cmds}" if cmds else "") + ("" if rows else f" — {text[-160:]!r}"))
        return rows

    def mod(self):
        """Последний ответ module status ({} — нет)."""
        rows = self.ask([{"op": "mod"}], 30)
        return rows[-1]["r"] if rows else {}


def journal(a, epoch, grep=None):
    """(журнал доступен sheregesh-ui, текст short-unix юнита блока с epoch по часам pi2)."""
    code, probe = ssh(a.remote, f"journalctl -u {REMOTE_UNIT} -n 3 -o cat --no-pager 2>&1")
    if code != 0 or not S6.journal_usable(probe):
        return False, ""
    flt = f" | grep -E {shlex.quote(grep)}" if grep else ""
    return True, E.out(a.remote, f"journalctl -u {REMOTE_UNIT} --since @{int(epoch) - 1} -o short-unix --no-pager "
                                 f"2>/dev/null{flt} || true")


def board_now(a):
    try:
        return float(E.out(a.remote, "date +%s.%N").strip())
    except ValueError:
        return None


def elrs_pids(snap):
    return sorted(p["pid"] for p in (snap or {}).get("procs", []) if ELRS_MARK in p["args"]
                  and "grep" not in p["args"].split()[:1])


def kill_main(a, sig):
    """Сигнал главному процессу блока через systemd (правило 50: глагол kill), не группе — эмулятор не трогается."""
    return ssh(a.remote, f"systemctl --no-ask-password kill --kill-whom=main -s {sig} {REMOTE_UNIT} 2>&1")


# ---------------------------------------------------------------- шаги


def step_enable(a, stand):
    """CR-01 (+ снимки для CR-02): configure backend=crsf-pty-lab, опрос до READY с device, затем sync_wait_s."""
    epoch = board_now(a) or 0.0
    prog = [{"op": "ctl", "args": f"configure backend={BACKEND_LAB} --wait 30"},
            {"op": "poll", "s": LIMITS["ready_s"] + 10, "period": POLL_S,
             "until": {"mod:" + P["state"]: "READY", "mod:" + P["crsf"] + ".device.name": EMU_NAME}},
            {"op": "poll", "s": LIMITS["sync_wait_s"], "period": 0.25,
             "until": {"mod:" + P["crsf"] + ".synced": True}},
            {"op": "poll", "s": 1.0, "period": 0.25}]
    rows = stand.ask(prog)
    c = op_row(rows, "ctl")
    m = {"result": c["r"].get("result") if c else None, "detail": c["r"].get("detail") if c else "нет ответа",
         "timeout_s": LIMITS["ready_s"] + 10}
    t0 = c["t0"] if c else None
    m["ready_s"] = time_to(rows, t0, lambda r: is_ready(BACKEND_LAB)(r) and device_name(r) == EMU_NAME) \
        if t0 else None
    ms = mods(rows, t0)
    last = ms[-1][1] if ms else {}
    m.update(state=g(last, "state"), backend=backend_of(last), device=device_name(last), mode=mode_of(last))
    ok, text = journal(a, epoch, r"configure \(req|module: \w+ -> ")
    m["journal_ok"] = ok
    t_cfg, _ = first_line(text, r"configure \(req \d+\):.*backend=" + re.escape(BACKEND_LAB), epoch - 1)
    tr = [x for x in parse_module_transitions(text) if t_cfg is not None and x["t"] >= t_cfg]
    rdy = next((x for x in tr if x["to"] == "READY"), None)
    m["ready_block_s"] = round(rdy["t"] - t_cfg, 2) if rdy else None
    m["journal"] = [f"{x['from']} -> {x['to']} ({x['reason']})" for x in tr]
    snaps = [crsf_of(r) for t, r in ms if g(r, "state") == "READY"]
    return judge_enable(m), m, snaps


def step_neutral(a, stand):
    """CR-03: передача выключена (disable, если включена) → neutral, центр, взведение false."""
    rows = stand.ask([{"op": "disable"}, {"op": "poll", "s": LIMITS["neutral_s"], "period": POLL_S,
                                          "ask": ["mod", "sticks"]}])
    d = op_row(rows, "disable")
    sts = replies(rows, "sticks")
    m = {"disable": d["r"].get("result") if d else None,
         "enabled": [S7.g(S7.sticks_of(r), "enabled") for _t, r in sts][-1:],
         "modes": sorted({str(mode_of(r)) for _t, r in mods(rows)})}
    ms = mods(rows, d["t"] if d else None)
    v, dd = judge_neutral(ms)
    if m["disable"] != "ok":
        v, dd = ("ERROR" if m["disable"] is None else "FAIL"), f"sticks disable: {m['disable']!r}; {dd}"
    last = ms[-1][1] if ms else {}
    m["emu"], m["crsf"] = emu_of(last), {k: crsf_of(last).get(k) for k in ("mode", "frames_sent", "period_us")}
    return (v, dd), m


def step_window(a, stand):
    """CR-04/05: enable → ACTIVE и live; окно window_s: sticks status и module status 10 Гц."""
    log(f"  СТИКИ И ПЕРЕКЛЮЧАТЕЛИ НЕ ТРОГАТЬ ≈ {a.window + LIMITS['live_s'] + 2:.0f} с (окно CR-04/05)")
    prog = [{"op": "enable"},
            {"op": "poll", "s": LIMITS["live_s"] + 2, "period": POLL_S, "until": {"mod:" + P["output"]: "live"}},
            {"op": "poll", "s": LIMITS["settle_s"] + a.window, "period": POLL_S, "ask": ["sticks", "mod"]}]
    rows = stand.ask(prog, a.window + 60)
    en = op_row(rows, "enable")
    m4 = {"enable_result": en["r"].get("result") if en else None,
          "enable_detail": en["r"].get("detail") if en else "опросчик не ответил"}
    m4["live_s"] = time_to(rows, en["t0"], lambda r: mode_of(r) == "live") if en else None
    m4["modes"] = sorted({str(mode_of(r)) for _t, r in mods(rows)})
    t_live = en["t0"] + m4["live_s"] if en and m4["live_s"] is not None else None
    t_w = t_live + LIMITS["settle_s"] if t_live is not None else None
    win = mods(rows, t_w) if t_w else []
    sts = replies(rows, "sticks", t_w) if t_w else []
    m4["window_s"] = round(win[-1][0] - win[0][0], 1) if len(win) > 1 else 0
    m4["not_live"] = sorted({str(mode_of(r)) for _t, r in win if mode_of(r) != "live"})
    ue = distinct_emu(win)
    m4["echo"] = echo_check(sts, ue)
    m4["armed"] = [emu_of(r).get("armed") for _t, r in ue if "armed" in emu_of(r)]
    m5 = {}
    if not win:
        m5["error"] = "окна live не было — частоту мерить не на чем"
    else:
        emus = [emu_of(r) for _t, r in ue]
        crs = [crsf_of(r) for _t, r in win]
        m5.update(emu_rates=[e.get("rate_hz") for e in emus if num(e.get("rate_hz"))],
                  emu_jitter=[e.get("jitter_us_p99") for e in emus if num(e.get("jitter_us_p99"))],
                  out_jitter=[c.get("jitter_us_p99") for c in crs if num(c.get("jitter_us_p99"))],
                  out_crc=counter_delta(crs, "crc_errors"), emu_crc=counter_delta(emus, "crc_errors"),
                  collisions=counter_delta(crs, "collisions"), frames_sent=counter_delta(crs, "frames_sent"),
                  window_s=m4["window_s"])
    return (judge_live(m4), m4), (judge_rate(m5), m5)


def ensure_live(stand):
    rows = stand.ask([{"op": "enable"}, {"op": "poll", "s": LIMITS["live_s"] + 3, "period": POLL_S,
                                         "until": {"mod:" + P["output"]: "live"}}])
    return any(mode_of(r) == "live" for _t, r in mods(rows))


def step_failsafe(a, stand):
    """CR-06: при live — страховочный start, stop пульта, журнал pi2 (FAILSAFE, crsf: live -> neutral), start пульта,
    центр у эмулятора."""
    m = {}
    if not ensure_live(stand):
        m["error"] = "output не стал live перед остановкой пульта"
        return judge_failsafe(m), m
    time.sleep(1.0)
    epoch = board_now(a) or 0.0
    S6.safety_start(a.console, CONSOLE_UNIT, 120)
    m["stop_rc"], out = S6.systemctl(a.console, "stop", CONSOLE_UNIT)
    m["stop_out"] = out[-200:]
    time.sleep(2.0)
    end = time.monotonic() + LIMITS["stop_detect_s"]
    while True:
        ok, text = journal(a, epoch, "control: |crsf: ")
        fs = next((x for x in S7.parse_transitions(text) if x["to"] == "FAILSAFE" and x["t"] >= epoch), None)
        tr = next((x for x in parse_crsf_transitions(text) if x["from"] == "live" and x["to"] == "neutral"
                   and x["t"] >= epoch), None)
        if not ok or (fs and tr) or time.monotonic() >= end:
            break
        time.sleep(1.0)
    m.update(journal_ok=ok, failsafe=fs, neutral=tr, delay_ms=neutral_delay(fs, tr))
    t1 = time.monotonic()
    m["start_rc"], _ = S6.systemctl(a.console, "start", CONSOLE_UNIT)
    m["console_back_s"] = None
    while time.monotonic() - t1 < LIMITS["console_start_s"]:
        r = stand.mod()
        if r.get("result") == "ok" and g(r, "support") == "ok":
            m["console_back_s"] = round(time.monotonic() - t1, 1)
            break
        time.sleep(2)
    if m["console_back_s"] is not None:
        rows = stand.ask([{"op": "poll", "s": LIMITS["neutral_s"], "period": POLL_S}])
        m["center"] = judge_neutral(mods(rows))
    return judge_failsafe(m), m


def no_response_s(r):
    """Тайм-аут молчания модуля из профиля (telemetry.profile.no_response_s), иначе LIMITS."""
    v = get(r, P["profile"] + ".no_response_s")
    return v if num(v) and v > 0 else LIMITS["no_response_s"]


def step_silent(a, stand):
    """CR-07: lab silent → FAULT (журнал pi2: строка команды → переход), output none; lab heal → READY."""
    r0 = stand.mod()
    m = {"no_response_s": no_response_s(r0)}
    epoch = board_now(a) or 0.0
    lim = m["no_response_s"] + LIMITS["fault_extra_s"]
    rows = stand.ask([{"op": "ctl", "args": "lab silent --wait 15"},
                      {"op": "poll", "s": lim + 6, "period": POLL_S, "until": {"mod:" + P["state"]: "FAULT"}},
                      {"op": "poll", "s": 1.5, "period": 0.25}])
    c = op_row(rows, "ctl")
    m["silent_result"] = c["r"].get("result") if c else None
    m["silent_detail"] = c["r"].get("detail") if c else "нет ответа"
    m["fault_s"] = time_to(rows, c["t0"], lambda r: g(r, "state") == "FAULT") if c else None
    ms = mods(rows)
    m["state"] = g(ms[-1][1], "state") if ms else None
    m["fault_modes"] = [mode_of(r) for _t, r in ms if g(r, "state") == "FAULT"]
    ok, text = journal(a, epoch, r"silent|module: \w+ -> ")
    t_cmd, line = first_line(text, r"silent", epoch - 1)
    tr = [x for x in parse_module_transitions(text) if t_cmd is not None and x["t"] >= t_cmd]
    flt = next((x for x in tr if x["to"] == "FAULT"), None)
    m.update(journal_ok=ok, cmd_line=line, fault_block_s=round(flt["t"] - t_cmd, 2) if flt else None,
             fault_code=flt["rest"] if flt else None)
    rows = stand.ask([{"op": "ctl", "args": "lab heal --wait 15"},
                      {"op": "poll", "s": LIMITS["heal_s"] + 2, "period": 0.25,
                       "until": {"mod:" + P["state"]: "READY"}}])
    c = op_row(rows, "ctl")
    m["heal_result"] = c["r"].get("result") if c else None
    m["ready_s"] = time_to(rows, c["t0"], is_ready()) if c else None
    return judge_silent(m), m


def step_restart(a, stand, freeze):
    """CR-08: (а) SIGSTOP главному процессу блока на freeze_s (страховочный SIGCONT на плате), журнал эмулятора;
    (б) systemctl restart блока → READY, neutral."""
    m = {"freeze": None, "restart": {}}
    if freeze:
        f = {}
        epoch = board_now(a) or 0.0
        # Страховка на плате: SIGCONT через freeze_s + 5 с, даже если SSH с Mac оборвётся (SIGCONT работающему — ничто).
        ssh(a.remote, f"nohup sh -c 'sleep {LIMITS['freeze_s'] + 5:.0f}; systemctl --no-ask-password kill "
                      f"--kill-whom=main -s SIGCONT {REMOTE_UNIT}' >/dev/null 2>&1 &")
        code, text = ssh(a.remote, f"date +%s.%N; systemctl --no-ask-password kill --kill-whom=main -s SIGSTOP "
                                   f"{REMOTE_UNIT} 2>&1; s=$?; [ $s = 0 ] && sleep {LIMITS['freeze_s']}; "
                                   f"systemctl --no-ask-password kill --kill-whom=main -s SIGCONT {REMOTE_UNIT} 2>&1; "
                                   f"echo stop=$s cont=$?", 30)
        lines = text.split()
        try:
            t_stop = float(lines[0])
        except (IndexError, ValueError):
            t_stop = None
        f["rc"], f["out"] = code, text[-200:]
        if code != 0 or t_stop is None or "stop=0 cont=0" not in text:
            f["error"] = f"SIGSTOP/SIGCONT не выполнены: rc {code} {text[-160:]!r}"
        else:
            f["journal_ok"], jt = journal(a, epoch, "uart-disconnected")
            t_d, line = first_line(jt, r"uart-disconnected", t_stop)
            f["disc_s"], f["line"] = (round(t_d - t_stop, 3) if t_d else None), line
            rows = stand.ask([{"op": "poll", "s": LIMITS["heal_s"], "period": 0.25,
                               "until": {"mod:" + P["state"]: "READY", "mod:" + P["output"]: "neutral"}}])
            ms = mods(rows)
            f["ready_s"] = round(ms[-1][0] - rows[0]["t0"], 1) if ms and is_ready()(ms[-1][1]) else None
        m["freeze"] = f
    r = m["restart"]
    t0 = time.monotonic()
    r["rc"], out = S6.systemctl(a.remote, "restart", REMOTE_UNIT)
    r["out"], ctl_s = out[-200:], time.monotonic() - t0
    rows = stand.ask([{"op": "poll", "s": LIMITS["restart_ready_s"], "period": 0.5,
                       "until": {"mod:" + P["state"]: "READY", "mod:" + P["output"]: "neutral"}},
                      {"op": "poll", "s": 1.0, "period": 0.25}], LIMITS["restart_ready_s"] + 60)
    ms = mods(rows)
    t_r = next((t for t, rr in ms if is_ready()(rr) and mode_of(rr) == "neutral"), None)
    # Время systemctl restart (часы Mac) + от первого запроса опросчика до READY и neutral (часы pi1): верхняя оценка.
    r["ready_s"] = round(ctl_s + t_r - rows[0]["t0"], 1) if t_r is not None else None
    last = ms[-1][1] if ms else {}
    r.update(mode=mode_of(last) if ms else None, backend=backend_of(last), state=g(last, "state"))
    return judge_restart(m), m


def step_back(a, stand):
    """CR-09: configure backend=lab-emulator → поведение S6."""
    rows = stand.ask([{"op": "ctl", "args": f"configure backend={BACKEND_S6} --wait 30"},
                      {"op": "poll", "s": LIMITS["s6_ready_s"] + 2, "period": 0.25,
                       "until": {"mod:" + P["state"]: "READY"}},
                      {"op": "poll", "s": 2.0, "period": 0.25}])
    c = op_row(rows, "ctl")
    m = {"result": c["r"].get("result") if c else None, "detail": c["r"].get("detail") if c else "нет ответа"}
    m["ready_s"] = time_to(rows, c["t0"], is_ready(BACKEND_S6)) if c else None
    ms = mods(rows, c["t0"] + m["ready_s"]) if c and m["ready_s"] is not None else []
    m["modes"] = [mode_of(r) if crsf_of(r) else g(r, "output") for _t, r in ms]
    m["backend"] = backend_of(ms[-1][1]) if ms else None
    snap = S6.procs(a)
    m["jr"], m["elrs"] = len(S6.emulator_pids(snap)), elrs_pids(snap)
    return judge_back(m), m


def restore(a, stand, initial_control, froze):
    """finally: SIGCONT блоку (если замораживали), службы запущены и передача стиков выключена (s7 restore, блок снова
    IDLE, если был), backend lab-emulator, ещё раз sticks disable."""
    out = {}
    if froze:
        out["sigcont"] = kill_main(a, "SIGCONT")[0]
    out["sticks"] = S7.restore(a, stand, initial_control)
    r = stand.mod()
    out["backend0"] = backend_of(r)
    if backend_of(r) != BACKEND_S6:
        rows = stand.ask([{"op": "ctl", "args": f"configure backend={BACKEND_S6} --wait 30"},
                          {"op": "poll", "s": LIMITS["s6_ready_s"], "period": 0.5,
                           "until": {"mod:" + P["state"]: "READY"}}])
        c = op_row(rows, "ctl")
        out["configure"] = c["r"].get("result") if c else None
        ms = mods(rows)
        out["state"] = g(ms[-1][1], "state") if ms else None
    rows = stand.ask([{"op": "disable"}, {"op": "mod"}, {"op": "sticks"}])
    d = op_row(rows, "disable")
    out["disable"] = d["r"].get("result") if d else None
    ms, sts = mods(rows), replies(rows, "sticks")
    out["final_backend"] = backend_of(ms[-1][1]) if ms else None
    out["final_enabled"] = S7.g(S7.sticks_of(sts[-1][1]), "enabled") if sts else None
    return out


# ---------------------------------------------------------------- план и запуск


def plan(a):
    fr = "SIGSTOP главному процессу блока на 2,5 с" if not a.no_freeze else "нет — --no-freeze"
    rows = [
        ("предпроверка", "серийные номера; OTA обеих плат в покое (s6_jr_accept.preflight); пульт отвечает module "
                         "status и sticks status; блок S8", ""),
        ("CR-10 до", "sheregesh-ota-client status, DEV-выпуск, образ, boot_id, failed — pi1 и pi2", "0,2"),
        ("CR-01", f"modulectl configure backend={BACKEND_LAB} → READY ≤ 5 с, device «{EMU_NAME}»", "0,3"),
        ("CR-02", "crsf.synced, period_us = 20 000 ± 1 %", ""),
        ("CR-03", "sticks disable → output neutral, каналы эмулятора = 992, взведение false", "0,1"),
        ("CR-04/05", f"sticks enable → output live; {a.window:.0f} с опроса 10 Гц (СТИКИ НЕ ТРОГАТЬ): каналы эмулятора "
                     f"= оси ±1 шаг, взведение true; 0x16 50 Гц ± 2 %, p99 джиттера ≤ 2 мс, CRC 0",
         f"{(a.window + 8) / 60:.1f}".replace(".", ",")),
        ("CR-06", "live → systemctl stop sheregesh-console → FAILSAFE, `crsf: live -> neutral` ≤ 600 мс от последнего "
                  "кадра (журнал pi2) → start пульта; центр у эмулятора", "1"),
        ("CR-07", "modulectl lab silent → FAULT ≤ no_response_s + 1 с, output none → lab heal → READY", "0,5"),
        ("CR-08", f"(а) заморозка: {fr} → uart-disconnected ≤ 1,5 с; (б) systemctl restart sheregesh-remote-net → "
                  "READY, output neutral", "1"),
        ("CR-09", f"modulectl configure backend={BACKEND_S6} → READY, output none, jrmodule один, elrs_tx нет", "0,3"),
        ("CR-10 после", "то же, что «до», должно совпасть", "0,2"),
    ]
    out = [f"S8 CR — приёмка выхода CRSF с эмулятором ELRS TX (ADR-0014 §4), pi1 {a.console} / pi2 {a.remote}, "
           f"≈ 4–6 мин:"]
    out += [f"  {k:<13} {t}" + (f"  ~{m} мин" if m else "") for k, t, m in rows]
    out += [f"  finally: SIGCONT блоку, службы запущены, backend {BACKEND_S6}, передача стиков выключена, блок IDLE.",
            "  Не трогает: OTA, слоты, видео, образ, юниты, polkit; без sudo; systemctl — stop/start пульта (CR-06), "
            "restart и kill SIGSTOP/SIGCONT блока (CR-08), лимит запусков — start_guard s6."]
    return "\n".join(out)


EMU_SRC = ROOT / "software/emulators/sheregesh_emulators/sticks_uinput.py"
EMU_STICKS_NAME = "Sheregesh sticks emulator (Boxer profile)"


class SticksEmu:
    """Эмулятор стиков на пульте (--sticks-emu): текст модуля по SSH от root (на плате ничего не ставится), устройство
    живёт, пока открыт канал; stop() закрывает stdin — эмулятор удаляет устройство и выходит (страховка — max-seconds)."""

    def __init__(self, host, axes, max_s=1800):
        self.host, self.axes, self.max_s, self.p = host, axes, max_s, None

    def command(self):
        b64 = base64.b64encode(EMU_SRC.read_bytes()).decode()
        prog = f"import base64;exec(compile(base64.b64decode('{b64}'),'sticks_uinput','exec'))"
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", f"root@{self.host}",
                f"python3 -c \"{prog}\" --axes {','.join(map(str, self.axes))} --max-seconds {self.max_s:.0f}"]

    def start(self, timeout=20):
        self.p = subprocess.Popen(self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            line = self.p.stdout.readline()
            if line.startswith("READY "):
                return line[6:].strip()
            if not line and self.p.poll() is not None:
                break
        self.stop()
        raise RuntimeError("эмулятор стиков не стартовал на пульте (нужен root по SSH и /dev/uinput)")

    def stop(self):
        if not self.p:
            return None
        try:
            self.p.stdin.close()
            return self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()
            return self.p.wait()
        finally:
            self.p = None


def parse_emu_axes(text):
    vals = [int(x) for x in text.split(",")]
    if len(vals) != AXES or any(not -1000 <= v <= 1000 for v in vals):
        raise argparse.ArgumentTypeError(f"--sticks-emu: {AXES} значений −1000…1000 через запятую")
    return vals


def main(argv=None):
    global LOG
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console", default="auto")
    ap.add_argument("--remote", default="auto")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--only", help="через запятую: CR-01…CR-09 (CR-10 «до/после» выполняется всегда; CR-01 — "
                                   "предусловие CR-02…CR-08 и выполняется, если запрошен любой из них)")
    ap.add_argument("--window", type=float, default=LIMITS["window_s"], help="CR-04/05: окно замера, с")
    ap.add_argument("--no-freeze", action="store_true", help="CR-08 без заморозки агента (только рестарт)")
    ap.add_argument("--sticks-emu", type=parse_emu_axes, metavar="A1,…,A8",
                    help="стики — эмулятор uinput на пульте с этими осями (−1000…1000), вместо аппаратуры")
    ap.add_argument("--plan", action="store_true")
    a = ap.parse_args(argv)
    if a.plan:
        print(plan(a))
        return 0
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # tools/: адреса плат по серийному номеру
    from stand import boards
    boards.resolve_args(a, {'console': 'pi1', 'remote': 'pi2'})
    if not a.out:
        ap.error("--out обязателен")
    only = set(STEPS) if not a.only else set(a.only.split(",")) & set(STEPS)
    if a.only and not only:
        ap.error(f"--only: нет известных шагов ({', '.join(STEPS)})")
    a.out.mkdir(parents=True, exist_ok=True)
    LOG = S6.LOG = S7.LOG = a.out / "run.log"
    emu = SticksEmu(a.console, a.sticks_emu) if a.sticks_emu else None
    if emu:
        try:
            log(f"эмулятор стиков на пульте: {emu.start()}, оси {a.sticks_emu}")
        except RuntimeError as e:
            log(f"ПРЕДПРОВЕРКА: {e} — без действий")
            (a.out / "summary.json").write_text(json.dumps({"preflight": [str(e)], "verdict": "ERROR"},
                                                           ensure_ascii=False, indent=1))
            return 2
    try:
        return run_steps(a, only, emu)
    finally:
        if emu:
            log(f"эмулятор стиков остановлен (код {emu.stop()})")


def run_steps(a, only, emu):
    ids, before, problems = S6.preflight(a)
    meta = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "boards": ids, "limits": LIMITS,
            "args": {k: str(v) for k, v in vars(a).items()}, "before": before,
            "sticks_source": f"uinput-emu {a.sticks_emu}" if emu else "hardware"}
    stand = Stand(a)
    if emu and not problems:                       # пульт находит устройство при пересканировании (раз в 1 с)
        t_e = time.monotonic()
        while time.monotonic() - t_e < 15 and S7.g(stand.status(), "present") is not True:
            time.sleep(0.5)
    if not problems:
        r = stand.mod()
        meta["module_before"] = {k: g(r, k) for k in ("support", "state", "output", "backend")}
        if r.get("result") != "ok" or g(r, "support") != "ok":
            problems.append(f"pi1: пульт не ответил module status (support ok): {r.get('result')!r} "
                            f"{g(r, 'support')!r} {str(r.get('detail') or '')[-160:]!r}")
        s = stand.status()
        meta["sticks_before"] = S7.sticks_of(s)
        if s.get("result") != "ok":
            problems.append(f"pi1: пульт не ответил sticks status (не S7/S8): {s.get('result')!r}")
    if problems:
        (a.out / "summary.json").write_text(json.dumps({**meta, "preflight": problems, "verdict": "ERROR"},
                                                       ensure_ascii=False, indent=1, default=str))
        log("ПРЕДПРОВЕРКА: " + "; ".join(problems) + " — без действий")
        return 2
    results, durations = {}, {}
    t_run = time.monotonic()

    def put(step, v, detail, extra=None, t0=None):
        if t0 is not None:
            durations[step] = round(time.monotonic() - t0, 1)
        results[step] = {"verdict": v, "detail": detail, **({"measured": extra} if extra else {}),
                         **({"duration_s": durations[step]} if step in durations else {})}
        log(f"{step}: {v}" + (f" — {detail}" if detail else ""))

    stand.ui.start()
    stand.rs.start()
    initial_control, froze = None, False
    try:
        stand.wait(stand.rs, lambda d: bool(d), time.monotonic(), 10)
        c0 = stand.control_after(time.monotonic() - 3, 4)
        initial_control = c0.get("state")
        meta["control_before"] = c0
        s0 = meta["sticks_before"]
        sticks_ok = S7.g(s0, "support") == "ok" and S7.g(s0, "present") is True
        no_sticks = None if sticks_ok else \
            f"аппаратура не видна пульту или пульт не S7 (support {S7.g(s0, 'support')!r}, present " \
            f"{S7.g(s0, 'present')!r})"
        need = only & {"CR-01", "CR-02", "CR-03", "CR-04", "CR-05", "CR-06", "CR-07", "CR-08"}
        blocked = None
        if need:
            t0 = time.monotonic()
            r1, m1, snaps = step_enable(a, stand)
            if "CR-01" in only:
                put("CR-01", *r1, m1, t0)
            if r1[0] != "PASS" and m1.get("state") != "READY":
                blocked = f"backend {BACKEND_LAB} не READY (CR-01: {r1[1][:120]})"
            if "CR-02" in only:
                if blocked:
                    put("CR-02", BLOCKED, blocked)
                else:
                    put("CR-02", *judge_sync(snaps), {"snaps": snaps[-3:]})

        def run(keys, fn, extra_block=None):
            keys = [k for k in keys if k in only]
            if not keys:
                return
            why = blocked or extra_block
            if why:
                for k in keys:
                    put(k, BLOCKED, why)
                return
            t0 = time.monotonic()
            for k, ((v, dd), mm) in fn().items():
                if k in keys:
                    put(k, v, dd, mm, t0)

        run(("CR-03",), lambda: {"CR-03": step_neutral(a, stand)})

        def window():
            r4, r5 = step_window(a, stand)
            return {"CR-04": r4, "CR-05": r5}

        run(("CR-04", "CR-05"), window, no_sticks)
        run(("CR-06",), lambda: {"CR-06": step_failsafe(a, stand)}, no_sticks)
        run(("CR-07",), lambda: {"CR-07": step_silent(a, stand)})

        def restart():
            nonlocal froze
            froze = not a.no_freeze
            return {"CR-08": step_restart(a, stand, not a.no_freeze)}

        run(("CR-08",), restart)
        if "CR-09" in only:
            t0 = time.monotonic()
            r9, m9 = step_back(a, stand)                    # ((вердикт, пояснение), замер) — как у прочих шагов
            put("CR-09", *r9, m9, t0)
    finally:
        try:
            meta["cleanup"] = restore(a, stand, initial_control, froze)
        except Exception as e:                                       # noqa: BLE001 — итог пишется в любом случае
            meta["cleanup"] = {"error": repr(e)}
        log(f"восстановление: {meta['cleanup']}")
        stand.ui.stop()
        stand.rs.stop()
    t0 = time.monotonic()
    after = S6.boards(a)
    meta["after"] = after
    v, p = S6.judge_ota(before, after)
    put("CR-10", v, "; ".join(p) or "OTA, слот, tryboot, образ, DEV-выпуск и boot_id прежние на обеих платах; "
                                    "failed пуст", {"before": before, "after": after}, t0)
    for k in STEPS:
        results.setdefault(k, {"verdict": "SKIP", "detail": "не запрошен (--only)"})
    meta["asks"] = stand.asks
    c = meta.get("cleanup") or {}
    if c.get("error") or c.get("final_enabled") is not False or c.get("final_backend") not in (BACKEND_S6, None):
        meta["cleanup_warning"] = "выключение передачи или возврат backend lab-emulator не подтверждены — см. cleanup"
        log("ВНИМАНИЕ: " + meta["cleanup_warning"])
    summary = {**meta, "duration_min": round((time.monotonic() - t_run) / 60, 1), "durations_s": durations,
               "results": {k: results[k] for k in STEPS}, "verdict": verdict_of(results)}
    (a.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    log(f"ИТОГ S8 CR {summary['verdict']} за {summary['duration_min']} мин: " +
        ", ".join(f"{k} {results[k]['verdict']}" for k in STEPS))
    return {"PASS": 0, "FAIL": 1, "ERROR": 2, BLOCKED: 3, "SKIP": 2}[summary["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
