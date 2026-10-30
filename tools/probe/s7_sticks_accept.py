#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Приёмка S7 «стики и кнопки пульта → удалённый блок» (ADR-0013 §5, ST-01…ST-10) на двух платах за 3–5 мин
(docs/acceptance/s7-sticks/PLAN.md). С машины владельца по SSH от sheregesh-ui, без sudo.

  python3 tools/probe/s7_sticks_accept.py --plan                                  # только план, без SSH
  python3 tools/probe/s7_sticks_accept.py --console 192.168.1.92 --remote 192.168.1.171 --out DIR \\
      [--only ST-03,ST-06,…] [--window 30] [--manual]

К pi1 по USB подключена RadioMaster Boxer (EdgeTX, режим USB Joystick). Радиовыхода нет (output = "none").
Команды и состояние пульта — через локальный канал работающего пульта (control.py, команда sticks: status | enable
с "confirmed": true | disable — тот же слот SticksView.setEnabled, что переключатель экрана «Управление»): на pi1
запускается короткий опросчик (HELPER) — шлёт запросы в сокет пульта и печатает ответы с временем pi1 строками JSON.
Часы плат не сравниваются: интервалы на пульте — по часам pi1, на блоке — по часам pi2 (link-status, журнал юнита).

Источники: ответ `sticks status` (SticksView.snapshot(): support, present, enabled, device{name, axes, buttons}, axes,
buttons, remote = rc-state блока от клиента канала (+ rx_at, latency_ms по кольцу rc_sent и rc_state.n), latency{…}),
ui-state.json pi1 (ключ sticks: present, enabled, deviceName, …), link-status.json pi2 (ключ control =
ControlReceiver.health(): state, reason, since_s, age_ms, rate_hz, gaps, failsafes, output — без ch/btn), журнал
sheregesh-remote-net (`[remote] control: A -> B (reason) n=… age_ms=…`).

  ST-01  sticks status: present, device.axes = 8, device.buttons = 24, имя; ui-state показывает устройство
  ST-02  передача выключена по умолчанию: при старте прогона enabled=false (пульт и экран), блок IDLE; после
         перезапуска пульта (ST-06) снова enabled=false и блок не ACTIVE (ADR §2: из FAILSAFE — только по кадрам)
  ST-03  enable → remote.state ACTIVE ≤ 1 с (часы pi1); за окно --window: rate_hz ≥ 45 (эхо rc-state и link-status
         блока), gaps не растут
  ST-04  неподвижные стики: ch блока (rc-state) = оси пульта ±1, btn блока = кнопки пульта; движение стиков и
         переключатель — ручной шаг (--manual)
  ST-05  задержка за то же окно: remote.latency_ms каждого rc-state (приём − отправка кадра n по кольцу rc_sent):
         медиана ≤ 30 мс, p99 ≤ 100 мс; то же по сводке пульта latency.median_ms/p99_ms (окно 30 с)
  ST-06  systemctl stop sheregesh-console (страховочный start на pi1) → блок FAILSAFE (timeout | link-lost) ≤ 600 мс от
         последнего кадра (age_ms в записи журнала о переходе, иначе age_ms/since_s снимка), затем start пульта
  ST-07  disable → блок IDLE (disabled) ≤ 1 с, без FAILSAFE (эхо, link-status, журнал; failsafes не вырос)
  ST-08  ручной шаг (--manual): выдернуть USB → FAILSAFE device-lost, present=false, enabled=false; вернуть →
         устройство снова видно, передача выключена, блок не ACTIVE
  ST-09  output = none во всех снимках control блока и rc-state
  ST-10  OTA-state как в JR-10 (s6_jr_accept.boards/judge_ota): OTA, слот, tryboot, образ, DEV-выпуск, boot_id прежние;
         systemctl --failed пуст

Из s6_jr_accept берётся импортом: предпроверка (серийные номера, OTA в покое) и снимок «до», сравнение JR-10,
start_guard/systemctl/safety_start (юниты: 5 запусков за 300 с — скрипт держит не больше 4, с учётом журнала платы),
выборки ui-state/link-status (Stand). Не трогает OTA, слоты, видео, модуль, образ, юниты, polkit; systemctl — только
stop/start sheregesh-console (ST-06) и start остановленной службы в finally. finally: передача выключена (sticks
disable), службы запущены, блок возвращён в IDLE (enable → disable), если был в IDLE до прогона. Вердикты PASS | FAIL |
ERROR | BLOCKED | SKIP; коды выхода 0 PASS, 1 FAIL, 2 ERROR, 3 BLOCKED.
"""
import argparse
import base64
import json
import math
import re
import shlex
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/probe"))
sys.path.insert(0, str(ROOT / "tools/deploy/ota-check"))
import s6_jr_accept as S6  # noqa: E402

E, S4 = S6.E, S6.S4
STEPS = ("ST-01", "ST-02", "ST-03", "ST-04", "ST-05", "ST-06", "ST-07", "ST-08", "ST-09", "ST-10")
BLOCKED = S6.BLOCKED
REMOTE_UNIT, CONSOLE_UNIT = S6.REMOTE_UNIT, S6.CONSOLE_UNIT
REL = S6.REL
AXES, BUTTONS = 8, 24
OUTPUT = "none"
# ADR-0013 §5 и свои: idle_s (ST-07 — как ST-03), stop_detect_s, console_start_s (QML на Pi 4, как F-02 S6),
# device_lost_s/replug_s (ST-08: опрос устройства раз в 1 с), settle_s — rate_hz блока считается за 2 с, первые
# секунды после ACTIVE не в счёт.
LIMITS = {"active_s": 1.0, "rate_min_hz": 45.0, "window_s": 30.0, "settle_s": 2.5, "latency_median_ms": 30.0,
          "latency_p99_ms": 100.0, "failsafe_ms": 600.0, "stop_detect_s": 10.0, "idle_s": 1.0,
          "console_start_s": 60.0, "device_lost_s": 10.0, "replug_s": 20.0, "replug_hold_s": 3.0}
ECHO_TOL = 1                       # ST-04: ±1 (в шкале −1000…1000), но не меньше шума оси за окно
REST_SPREAD_MAX = 20               # ST-04: разброс оси пульта за окно больше — стики не неподвижны (ERROR)
MOVE_MIN = 300                     # ST-04 ручной: поза отличается от покоя хотя бы на столько по одной оси
LATENCY_MIN_SAMPLES = 50           # ST-05: меньше — перцентиль не доказателен (ERROR)
STABLE_RUN = 3                     # ST-04: эхо сравнивается там, где оси пульта не менялись 3 выборки подряд (≥ 100 мс)
WINDOW_PERIOD_S = 0.05             # опрос в окне 20 Гц: каждый rc-state (10 Гц) виден хотя бы раз
POSES = ("левый стик ВВЕРХ до упора", "правый стик ВПРАВО до упора",
         "любой переключатель (SA/SB) в другое положение или нажать и держать кнопку")
LOG = None

# Поля ответа `sticks status` (SticksView.snapshot(), control.py) — пути через точку; ui-state sticks — плоский.
P = {"support": "support", "present": "present", "enabled": "enabled", "name": "device.name",
     "axis_count": "device.axes", "button_count": "device.buttons", "axes": "axes", "buttons": "buttons",
     "rstate": "remote.state", "rreason": "remote.reason", "rch": "remote.ch", "rbtn": "remote.btn",
     "rate": "remote.rate_hz", "gaps": "remote.gaps", "failsafes": "remote.failsafes", "rn": "remote.n",
     "rx_at": "remote.rx_at", "rlat": "remote.latency_ms", "routput": "remote.output",
     "lat_median": "latency.median_ms", "lat_p99": "latency.p99_ms", "lat_samples": "latency.samples"}
REQUESTS = {"status": {"cmd": "sticks", "op": "status"},
            "enable": {"cmd": "sticks", "op": "enable", "confirmed": True},
            "disable": {"cmd": "sticks", "op": "disable"}}

# Опросчик на pi1: argv[1] — запросы, argv[2] — программа [{"op": status|enable|disable} | {"op": "poll", "s": с,
# "period": с, "until": {путь: значение}}]. Строка на ответ: {"t0": отправлен, "t": получен (time.time pi1), "op", "r"};
# latency.recent_ms (до 50 чисел) не печатается — объём вывода за окно 20 Гц.
HELPER = r'''
import json, sys, time
from sheregesh.link.videoselect import ConsoleAbsent, ask_console, socket_path
REQ, PROG = json.loads(sys.argv[1]), json.loads(sys.argv[2])
path = socket_path()
def get(d, p):
    for k in p.split("."):
        d = d.get(k) if isinstance(d, dict) else None
    return d
def ask(op):
    t0 = time.time()
    try:
        r = ask_console(path, REQ[op], 5.0)
    except ConsoleAbsent as e:
        r = {"result": "not-connected", "detail": str(e)}
    except (OSError, ValueError) as e:
        r = {"result": "error", "detail": type(e).__name__ + ": " + str(e)}
    lat = get(r, "sticks.latency")
    if isinstance(lat, dict):
        lat.pop("recent_ms", None)
    print(json.dumps({"t0": round(t0, 4), "t": round(time.time(), 4), "op": op, "r": r}, ensure_ascii=False),
          flush=True)
    return r
for st in PROG:
    if st["op"] != "poll":
        ask(st["op"])
        continue
    end, nxt = time.time() + st["s"], time.time()
    while True:
        r = ask("status")
        u = st.get("until")
        if (u and all(get(r, "sticks." + k) == v for k, v in u.items())) or time.time() >= end:
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


# ---------------------------------------------------------------- чистые функции (тесты: test_s7_sticks_accept.py)


def verdict_of(results):
    return S6.verdict_of(results)


agg = S6.agg


def num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def get(d, path):
    """Значение по пути через точку ("remote.state"); None — нет."""
    for k in path.split("."):
        d = d.get(k) if isinstance(d, dict) else None
    return d


def g(s, key):
    return get(s, P[key])


def helper_cmd(prog):
    """Команда SSH для pi1: опросчик HELPER из выпуска пульта (DEV deploy или образ), без файлов на плате."""
    b64 = base64.b64encode(HELPER.encode()).decode()
    return (f"{REL}; PYTHONPATH=$C/software python3 -c \"$(echo {b64} | base64 -d)\" "
            f"{shlex.quote(json.dumps(REQUESTS))} {shlex.quote(json.dumps(prog))} 2>&1")


def parse_rows(text):
    """Вывод HELPER → строки {t0, t, op, r}; прочие строки (traceback, предупреждения) пропускаются."""
    rows = []
    for ln in (text or "").splitlines():
        try:
            v = json.loads(ln)
        except ValueError:
            continue
        if isinstance(v, dict) and num(v.get("t")) and num(v.get("t0")) and isinstance(v.get("r"), dict):
            rows.append(v)
    return rows


def sticks_of(reply):
    s = (reply or {}).get("sticks")
    return s if isinstance(s, dict) else {}


def ctl_of(doc):
    """control из link-status.json блока (ControlReceiver.health()); {} — блок не S7."""
    c = (doc or {}).get("control")
    return c if isinstance(c, dict) else {}


def ui_sticks(doc):
    s = (doc or {}).get("sticks")
    return s if isinstance(s, dict) else {}


def statuses(rows, t_from=None, t_to=None):
    """(t, sticks) ответов status с result ok, полученных в [t_from, t_to] по часам pi1."""
    return [(r["t"], sticks_of(r["r"])) for r in rows if r.get("op") == "status" and r["r"].get("result") == "ok"
            and (t_from is None or r["t"] >= t_from) and (t_to is None or r["t"] <= t_to)]


def op_row(rows, op):
    return next((r for r in rows if r.get("op") == op), None)


def time_to(rows, t_from, pred):
    """Секунды от t_from (pi1) до первого ответа status (момент получения — верхняя оценка), где pred(sticks)."""
    for t, s in statuses(rows, t_from):
        if pred(s):
            return round(t - t_from, 3)
    return None


def remote_is(state, reason=None):
    return lambda s: g(s, "rstate") == state and (reason is None or g(s, "rreason") == reason)


def percentile(values, q):
    """Перцентиль q (0…100) по ближайшему рангу (без интерполяции: p99 из 300 выборок — 297-я по возрастанию)."""
    v = sorted(values)
    if not v:
        return None
    return v[max(0, math.ceil(q / 100 * len(v)) - 1)]


def latency_samples(sts):
    """ST-05: remote.latency_ms по одному на rc-state (ключ — rx_at приёма клиентом, иначе n) в ACTIVE: опрос 20 Гц
    видит один rc-state 1–3 раза, повтор не должен весить вдвое."""
    out, seen = [], set()
    for _t, s in sts:
        v = g(s, "rlat")
        if not num(v) or v < 0 or g(s, "rstate") != "ACTIVE":
            continue
        key = g(s, "rx_at") if num(g(s, "rx_at")) else ("n", g(s, "rn"))
        if key in seen:
            continue
        seen.add(key)
        out.append(float(v))
    return out


def fresh_docs(rows, t_from):
    """Документы выборки link-status, ЗАПИСАННЫЕ блоком не раньше t_from (Mac monotonic): момент записи = момент
    чтения − (часы pi2 при чтении − time документа). Прочитанный документ бывает старым (пишется раз в 2 с)."""
    out = []
    for mono, board, doc in rows:
        tm = (doc or {}).get("time")
        if num(tm) and num(board) and mono - (board - tm) >= t_from:
            out.append(doc)
    return out


def distinct(docs):
    """Документы link-status без повторов (один файл читается выборкой до 8 раз за период записи)."""
    seen, out = set(), []
    for d in docs:
        k = d.get("time")
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out


def window_docs(docs):
    """ST-03: снимки control в ACTIVE не раньше settle_s после перехода (rate_hz считается за 2 с), без повторов."""
    return [d for d in distinct(docs) if ctl_of(d).get("state") == "ACTIVE" and num(ctl_of(d).get("since_s"))
            and ctl_of(d)["since_s"] >= LIMITS["settle_s"]]


TRANSITION = re.compile(r"^(\d+(?:\.\d+)?)\s.*?control: (\w+) -> (\w+) \(([\w-]+)\)(.*)$")
AGE = re.compile(r"\bage_ms=(\d+)")


def parse_transitions(text):
    """journalctl -o short-unix юнита блока → переходы сторожа [{t (pi2), from, to, reason, age_ms, rest}]."""
    out = []
    for ln in (text or "").splitlines():
        m = TRANSITION.match(ln.strip())
        if m:
            age = AGE.search(m.group(5))
            out.append({"t": float(m.group(1)), "from": m.group(2), "to": m.group(3), "reason": m.group(4),
                        "age_ms": int(age.group(1)) if age else None, "rest": m.group(5).strip()})
    return sorted(out, key=lambda x: x["t"])


def failsafe_delay(ctl, doc_time, jr=None):
    """ST-06: (мс от последнего кадра до перехода в FAILSAFE, источник), всё по часам pi2. Первым — age_ms из записи
    журнала о переходе (ControlReceiver пишет возраст последнего кадра в момент перехода); иначе по снимку
    link-status: (time − since_s) − (time − age_ms), since_s округлён до 0,1 с. (None, "") — нечем считать."""
    if jr and num(jr.get("age_ms")):
        return jr["age_ms"], "журнал (age_ms перехода)"
    age, since = ctl.get("age_ms"), ctl.get("since_s")
    if not num(age) or not num(doc_time):
        return None, ""
    last = doc_time - age / 1000.0
    if jr and num(jr.get("t")):
        return round((jr["t"] - last) * 1000), "время записи журнала и age_ms снимка"
    if not num(since):
        return None, ""
    return round((doc_time - since - last) * 1000), "age_ms и since_s снимка (±50 мс)"


def spread(values):
    v = [x for x in values if num(x)]
    return (max(v) - min(v)) if v else None


def echo_compare(sts):
    """ST-04: оси и кнопки пульта против ch/btn блока (rc-state) при неподвижных (удерживаемых) стиках.
    → {samples, stable, spread (наибольший разброс оси пульта), echo_max (|axes − remote.ch| на стабильных выборках),
       btn_bad (стабильных выборок с remote.btn ≠ buttons), axes_ref (медиана осей пульта), buttons (маски пульта)}."""
    smp = [(g(s, "axes"), g(s, "rch"), g(s, "buttons"), g(s, "rbtn"), g(s, "rstate")) for _t, s in sts]
    smp = [x for x in smp if isinstance(x[0], list) and len(x[0]) == AXES]
    res = {"samples": len(smp), "stable": 0, "spread": None, "echo_max": None, "btn_bad": 0, "axes_ref": None,
           "buttons": None}
    if not smp:
        return res
    cols = list(zip(*[x[0] for x in smp]))
    res["spread"] = max(max(c) - min(c) for c in cols)
    res["axes_ref"] = [statistics.median_low(c) for c in cols]
    res["buttons"] = sorted({x[2] for x in smp if num(x[2])})
    diffs = []
    for i in range(STABLE_RUN - 1, len(smp)):
        win = smp[i - STABLE_RUN + 1:i + 1]
        axes, rch, btn, rbtn, rstate = smp[i]
        if rstate != "ACTIVE" or not isinstance(rch, list) or len(rch) != AXES or \
                any(w[0] != axes or w[2] != btn for w in win):
            continue
        diffs.append(max(abs(a - b) for a, b in zip(axes, rch)))
        if rbtn != btn:
            res["btn_bad"] += 1
    res["stable"], res["echo_max"] = len(diffs), (max(diffs) if diffs else None)
    return res


# ---------------------------------------------------------------- суждения


def judge_device(rep, ui):
    """ST-01: sticks status ok; present; 8 осей и 24 кнопки; имя устройства; ui-state пульта показывает устройство."""
    if not rep or rep.get("result") != "ok":
        return "ERROR", f"sticks status: {(rep or {}).get('result')!r} {(rep or {}).get('detail', '')}"
    s, p = sticks_of(rep), []
    if g(s, "present") is not True:
        p.append(f"present {g(s, 'present')!r} (support {g(s, 'support')!r}) — аппаратура не видна")
    na, nb = g(s, "axis_count"), g(s, "button_count")
    if na != AXES:
        p.append(f"осей {na!r}, ожидалось {AXES}")
    if nb != BUTTONS:
        p.append(f"кнопок {nb!r}, ожидалось {BUTTONS}")
    if not str(g(s, "name") or "").strip():
        p.append("имя устройства пустое")
    u = ui_sticks(ui)
    if not u:
        p.append("в ui-state.json пульта нет sticks (GUI не S7?)")
    elif u.get("present") is not True:
        p.append(f"ui-state sticks.present {u.get('present')!r}")
    return ("FAIL" if p else "PASS"), "; ".join(p) or f"«{g(s, 'name')}»: {na} осей, {nb} кнопок, на экране видно"


def judge_default_off(obs):
    """ST-02. obs — наблюдения {when, enabled, ui_enabled, block_state, block_reason, require_idle}: передача
    выключена (пульт и экран); блок IDLE (require_idle — старт прогона) или хотя бы не ACTIVE (после перезапуска
    пульта блок законно остаётся в FAILSAFE link-lost: ADR §2 — выход из FAILSAFE только по свежим кадрам)."""
    if not obs:
        return "ERROR", "нет наблюдений"
    p, ok, errs = [], [], 0
    for o in obs:
        w = o.get("when")
        if o.get("error"):
            errs += 1
            p.append(f"{w}: {o['error']}")
            continue
        q = []
        if o.get("enabled") is not False:
            q.append(f"enabled {o.get('enabled')!r}")
        if "ui_enabled" in o and o["ui_enabled"] is not False:
            q.append(f"на экране enabled {o.get('ui_enabled')!r}")
        st = o.get("block_state")
        if o.get("require_idle") and st != "IDLE":
            q.append(f"блок {st!r} ({o.get('block_reason')!r}), ожидался IDLE")
        elif st in (None, "ACTIVE"):
            q.append(f"блок {st!r}")
        p += [f"{w}: {x}" for x in q]
        ok.append(f"{w}: выключена, блок {st} ({o.get('block_reason')})")
    if p:
        return ("ERROR" if errs == len(p) else "FAIL"), "; ".join(p)
    return "PASS", "; ".join(ok)


def judge_rate(m):
    """ST-03: enable ok; ACTIVE на пульте ≤ active_s; за окно rate_hz ≥ rate_min_hz по эху rc-state и по link-status
    блока; gaps не изменились (ни эхо, ни блок); окно не короче запрошенного − 2 с."""
    if m.get("enable_result") is None:
        return "ERROR", f"опросчик на pi1 не ответил: {m.get('enable_detail') or ''}"
    if m.get("enable_result") != "ok":
        return "FAIL", f"enable: {m.get('enable_result')!r} {m.get('enable_detail') or ''}"
    p = []
    if m.get("active_s") is None or m["active_s"] > LIMITS["active_s"]:
        p.append(f"ACTIVE через {m.get('active_s')} с (предел {LIMITS['active_s']:.0f})")
        return "FAIL", "; ".join(p)
    want = m.get("window_req") or LIMITS["window_s"]
    if (m.get("window_s") or 0) < want - 2:
        return "ERROR", f"окно {m.get('window_s')} с короче {want:.0f} с — замер не доказателен"
    for src, rates, gaps in (("эхо", m.get("echo_rates"), m.get("echo_gaps")),
                             ("блок", m.get("block_rates"), m.get("block_gaps"))):
        rates = [r for r in rates or [] if num(r)]
        if not rates:
            p.append(f"{src}: нет rate_hz за окно")
            continue
        if min(rates) < LIMITS["rate_min_hz"]:
            p.append(f"{src}: rate_hz от {min(rates)} (≥ {LIMITS['rate_min_hz']:.0f})")
        sp = spread(gaps or [])
        if sp is None:
            p.append(f"{src}: нет gaps")
        elif sp:
            p.append(f"{src}: gaps выросли на {sp} ({min(gaps)} → {max(gaps)})")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"ACTIVE за {m['active_s']} с; {m['window_s']} с: rate_hz эхо ≥ {min(m['echo_rates'])} "
        f"({len(m['echo_rates'])}), блок ≥ {min(m['block_rates'])} ({len(m['block_rates'])} снимков), "
        f"gaps без изменений ({m['block_gaps'][-1]})")


def judge_echo_rest(e):
    """ST-04, автоматическая часть: стики неподвижны; ch блока = оси пульта ±1; btn блока = кнопки пульта."""
    if not e or not e.get("samples"):
        return "ERROR", "нет выборок осей пульта"
    if e["spread"] > REST_SPREAD_MAX:
        return "ERROR", f"оси пульта менялись на {e['spread']} за окно (> {REST_SPREAD_MAX}) — стики не неподвижны"
    p = []
    if not e.get("stable"):
        p.append("нет стабильных выборок в ACTIVE для сравнения с rc-state")
    elif e["echo_max"] > max(ECHO_TOL, e["spread"]):
        # Эхо (rc-state 10 Гц) отстаёт от опроса пульта на 12–150 мс; шум осей в покое (прогон 2026-10-26: 3)
        # даёт такое же расхождение без ошибки передачи — допуск не меньше разброса оси за окно.
        p.append(f"ch блока отличается от осей пульта до {e['echo_max']} (±{max(ECHO_TOL, e['spread'])})")
    if e.get("btn_bad"):
        p.append(f"btn блока ≠ кнопкам пульта в {e['btn_bad']} выборках (пульт {e.get('buttons')})")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"покой: ch блока ±{e['echo_max']} к осям ({e['stable']} выборок), btn = кнопки {e.get('buttons')}, "
        f"оси {e['axes_ref']}")


def judge_pose(pose, base):
    """ST-04, ручная поза: оси/кнопки отличаются от покоя; ch/btn блока совпадают с позой."""
    e = pose.get("echo") or {}
    if not e.get("samples"):
        return "ERROR", f"«{pose.get('pose')}»: нет выборок"
    p = []
    moved = max(abs(a - b) for a, b in zip(e["axes_ref"], base["axes_ref"])) if base.get("axes_ref") else 0
    if moved < MOVE_MIN and e.get("buttons") == base.get("buttons"):
        p.append(f"оси изменились на {moved} (< {MOVE_MIN}), кнопки те же — поза не принята")
    if not e.get("stable"):
        p.append("поза не удерживалась (нет стабильных выборок в ACTIVE)")
    elif e["echo_max"] > ECHO_TOL:
        p.append(f"ch блока отличается до {e['echo_max']}")
    if e.get("btn_bad"):
        p.append(f"btn блока ≠ пульта в {e['btn_bad']} выборках")
    return ("FAIL" if p else "PASS"), f"«{pose.get('pose')}»: " + ("; ".join(p) or (
        f"сдвиг {moved}, кнопки {e.get('buttons')}, ch блока ±{e['echo_max']}"))


def judge_echo(rest, poses, manual):
    v, d = judge_echo_rest(rest)
    if not manual:
        return v, d + "; движение стиков — ручной шаг, не выполнялся (--manual)"
    vs, ds = [v], [d]
    for pose in poses or []:
        pv, pd = judge_pose(pose, rest or {})
        vs.append(pv)
        ds.append(pd)
    return agg(vs), "; ".join(ds)


def judge_latency(samples, reported=None):
    """ST-05: по своим выборкам (remote.latency_ms на каждый rc-state) медиана ≤ 30 мс и p99 ≤ 100 мс, выборок не
    меньше LATENCY_MIN_SAMPLES; сводка пульта (latency.median_ms/p99_ms за 30 с), если есть, — в тех же пределах."""
    if len(samples) < LATENCY_MIN_SAMPLES:
        return "ERROR", f"выборок задержки {len(samples)} (< {LATENCY_MIN_SAMPLES}) — перцентиль не доказателен"
    med, p99 = round(statistics.median(samples), 1), round(percentile(samples, 99), 1)
    p = []
    if med > LIMITS["latency_median_ms"]:
        p.append(f"медиана {med} мс (≤ {LIMITS['latency_median_ms']:.0f})")
    if p99 > LIMITS["latency_p99_ms"]:
        p.append(f"p99 {p99} мс (≤ {LIMITS['latency_p99_ms']:.0f})")
    r = reported or {}
    if num(r.get("median_ms")) and r["median_ms"] > LIMITS["latency_median_ms"]:
        p.append(f"сводка пульта: медиана {r['median_ms']} мс")
    if num(r.get("p99_ms")) and r["p99_ms"] > LIMITS["latency_p99_ms"]:
        p.append(f"сводка пульта: p99 {r['p99_ms']} мс")
    rep = f"; пульт: медиана {r.get('median_ms')} мс, p99 {r.get('p99_ms')} мс ({r.get('samples')})" if r else ""
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"медиана {med} мс, p99 {p99} мс, max {max(samples):.0f} мс ({len(samples)} rc-state){rep}")


def judge_console_stop(m):
    """ST-06: stop пульта принят; блок FAILSAFE (timeout | link-lost); от последнего кадра ≤ failsafe_ms; пульт
    поднялся (sticks status ok, support ok) ≤ console_start_s."""
    if m.get("error"):
        return "ERROR", m["error"]
    if m.get("stop_rc") != 0:
        return "ERROR", f"systemctl stop {CONSOLE_UNIT}: rc {m.get('stop_rc')} {m.get('stop_out', '')}"
    p = []
    if m.get("failsafe_s") is None:
        p.append(f"FAILSAFE на блоке не наблюдался за {LIMITS['stop_detect_s']:.0f} с (состояние {m.get('state')!r} "
                 f"{m.get('reason')!r})")
    else:
        if m.get("reason") not in ("timeout", "link-lost"):
            p.append(f"причина {m.get('reason')!r}, ожидалась timeout или link-lost")
        if m.get("delay_ms") is None:
            p.append("задержку от последнего кадра посчитать нечем (нет age_ms в журнале и снимке)")
        elif m["delay_ms"] > LIMITS["failsafe_ms"]:
            p.append(f"FAILSAFE через {m['delay_ms']} мс от последнего кадра (≤ {LIMITS['failsafe_ms']:.0f})")
    if m.get("start_rc") != 0:
        p.append(f"systemctl start {CONSOLE_UNIT}: rc {m.get('start_rc')}")
    elif m.get("console_back_s") is None:
        p.append(f"пульт не ответил sticks status (support ok) за {LIMITS['console_start_s']:.0f} с после start")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"FAILSAFE ({m['reason']}) через {m['delay_ms']} мс от последнего кадра (по: {m.get('delay_src')}); "
        f"пульт снова отвечает через {m['console_back_s']} с")


def judge_disable(m):
    """ST-07: disable ok; IDLE (disabled) на пульте ≤ idle_s; ни одного FAILSAFE (эхо, link-status, журнал);
    failsafes блока не вырос; блок в link-status — IDLE (disabled)."""
    if m.get("error"):
        return "ERROR", m["error"]
    if m.get("disable_result") is None:
        return "ERROR", f"опросчик на pi1 не ответил: {m.get('disable_detail') or ''}"
    if m.get("disable_result") != "ok":
        return "FAIL", f"disable: {m.get('disable_result')!r} {m.get('disable_detail') or ''}"
    p = []
    if m.get("idle_s") is None or m["idle_s"] > LIMITS["idle_s"]:
        p.append(f"IDLE (disabled) на пульте через {m.get('idle_s')} с (предел {LIMITS['idle_s']:.0f})")
    if m.get("failsafe_seen"):
        p.append(f"FAILSAFE после disable: {m['failsafe_seen']}")
    f0, f1 = m.get("failsafes0"), m.get("failsafes1")
    if not num(f0) or not num(f1):
        p.append(f"failsafes блока {f0!r} → {f1!r} (не прочитаны)")
    elif f1 > f0:
        p.append(f"failsafes блока {f0} → {f1}")
    if (m.get("block_state"), m.get("block_reason")) != ("IDLE", "disabled"):
        p.append(f"блок {m.get('block_state')!r} ({m.get('block_reason')!r}), ожидался IDLE (disabled)")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"IDLE (disabled) за {m['idle_s']} с, без FAILSAFE, failsafes {f1}" +
        (f", журнал: {m['journal']}" if m.get("journal") else ""))


def judge_unplug(m):
    """ST-08 (ручной): после выдёргивания — блок FAILSAFE device-lost, пульт present=false и enabled=false; после
    возврата — present=true, enabled=false во всех выборках, блок не ACTIVE."""
    if m.get("error"):
        return "ERROR", m["error"]
    if not m.get("active_before"):
        return "ERROR", "передача не стала ACTIVE перед выдёргиванием — шаг не доказателен"
    p = []
    if m.get("lost_state") != "FAILSAFE" or m.get("lost_reason") != "device-lost":
        p.append(f"блок {m.get('lost_state')!r} ({m.get('lost_reason')!r}), ожидался FAILSAFE (device-lost)")
    if m.get("lost_present") is not False:
        p.append(f"пульт present {m.get('lost_present')!r} после выдёргивания")
    if m.get("lost_enabled") is not False:
        p.append(f"пульт enabled {m.get('lost_enabled')!r} после выдёргивания")
    if m.get("back_s") is None:
        p.append(f"устройство не вернулось за {LIMITS['replug_s']:.0f} с")
    else:
        if m.get("enabled_after"):
            p.append(f"после возврата enabled≠false в {m['enabled_after']} выборках — передача включилась сама")
        if m.get("active_after"):
            p.append(f"после возврата блок ACTIVE в {m['active_after']} выборках")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"FAILSAFE (device-lost), enabled=false; устройство вернулось через {m['back_s']} с, передача выключена, "
        f"блок {m.get('state_after')}")


def judge_output(values):
    """ST-09: output = "none" во всех снимках control блока и rc-state."""
    if not values:
        return "ERROR", "нет ни одного снимка control/rc-state блока"
    bad = [v for v in values if v != OUTPUT]
    return ("FAIL", f"output ≠ none: {sorted(set(map(str, bad)))} в {len(bad)} из {len(values)}") if bad else \
        ("PASS", f"output = none во всех {len(values)} снимках")


# ---------------------------------------------------------------- стенд


def ssh(host, cmd, timeout=60):
    return E.ssh(host, cmd, timeout)


class Stand(S6.Stand):
    """Выборки ui-state pi1 и link-status pi2 (s4/s6 Stand) и опросчик sticks на pi1."""

    def __init__(self, a):
        super().__init__(a)
        self.runs = []                 # каждый запуск опросчика: программа, код, ответов, итоги команд
        self.routputs = []             # remote.output из всех ответов (ST-09)

    def sticks(self, prog, timeout=None):
        """Программа опросчика на pi1 → строки ответов (t по часам pi1)."""
        dur = sum(st.get("s", 0) for st in prog)
        code, text = ssh(self.a.console, helper_cmd(prog), timeout or dur + 30)
        rows = parse_rows(text)
        cmds = {r["op"]: r["r"].get("result") for r in rows if r["op"] != "status"}
        self.routputs += [g(s, "routput") for _t, s in statuses(rows) if isinstance(get(s, "remote"), dict)]
        self.runs.append({"prog": prog, "rc": code, "replies": len(rows), "cmds": cmds,
                          **({"tail": text[-300:]} if not rows else {})})
        log(f"  sticks {[st['op'] for st in prog]}: rc {code}, ответов {len(rows)}" +
            (f", {cmds}" if cmds else "") + ("" if rows else f" — {text[-160:]!r}"))
        return rows

    def status(self):
        rows = self.sticks([{"op": "status"}], 20)
        return rows[-1]["r"] if rows else {}

    def control(self):
        """Последний control блока из выборки link-status."""
        r = self.rs.last()
        return ctl_of(r[2]) if r else {}

    def control_after(self, t_from, timeout):
        """Первый control, записанный блоком не раньше t_from (Mac monotonic); {} — не дождались."""
        end = time.monotonic() + timeout
        while True:
            docs = [d for d in fresh_docs(self.rs.since(t_from - 5), t_from) if ctl_of(d)]
            if docs or time.monotonic() >= end:
                return ctl_of(docs[0]) if docs else {}
            time.sleep(0.2)

    def outputs_all(self):
        return [ctl_of(d).get("output") for d in distinct([d for _m, _b, d in self.rs.since(0)]) if ctl_of(d)] + \
            self.routputs


def journal_transitions(a, epoch):
    """(журнал доступен sheregesh-ui, переходы сторожа с epoch по часам pi2)."""
    code, probe = ssh(a.remote, f"journalctl -u {REMOTE_UNIT} -n 3 -o cat --no-pager 2>&1")
    if code != 0 or not S6.journal_usable(probe):
        return False, []
    text = E.out(a.remote, f"journalctl -u {REMOTE_UNIT} --since @{int(epoch) - 1} -o short-unix --no-pager "
                           f"2>/dev/null | grep -F 'control: ' || true")
    return True, [t for t in parse_transitions(text) if t["t"] >= epoch - 0.5]


# ---------------------------------------------------------------- шаги


def ensure_active(stand):
    """enable и ожидание ACTIVE на пульте → (ok, строки)."""
    rows = stand.sticks([{"op": "enable"}, {"op": "poll", "s": LIMITS["active_s"] + 4, "period": 0.05,
                                            "until": {P["rstate"]: "ACTIVE"}}])
    return any(remote_is("ACTIVE")(s) for _t, s in statuses(rows)), rows


def observe_off(stand, when, require_idle):
    """ST-02: наблюдение «передача выключена» по пульту, экрану и блоку."""
    rep = stand.status()
    if rep.get("result") != "ok":
        return {"when": when, "error": f"sticks status: {rep.get('result')!r} {rep.get('detail', '')}"}
    s = sticks_of(rep)
    c = stand.control_after(time.monotonic(), 3)
    ui = stand.ui.last()
    o = {"when": when, "enabled": g(s, "enabled"), "remote_state": g(s, "rstate"), "block_state": c.get("state"),
         "block_reason": c.get("reason"), "require_idle": require_idle}
    u = ui_sticks(ui[2] if ui else {})
    if u:
        o["ui_enabled"] = u.get("enabled")
    return o


def step_window(a, stand):
    """ST-03/04/05: enable, ACTIVE, окно window_s с опросом 20 Гц → (r3, m3), rest, (r5, m5)."""
    m = {}
    t_mac = time.monotonic()
    prog = [{"op": "enable"},
            {"op": "poll", "s": LIMITS["active_s"] + 4, "period": 0.05, "until": {P["rstate"]: "ACTIVE"}},
            {"op": "poll", "s": LIMITS["settle_s"] + a.window, "period": WINDOW_PERIOD_S}]
    rows = stand.sticks(prog, a.window + 40)
    en = op_row(rows, "enable")
    m["enable_result"] = en["r"].get("result") if en else None
    m["enable_detail"] = en["r"].get("detail") if en else "опросчик не ответил"
    m["active_s"] = time_to(rows, en["t0"], remote_is("ACTIVE")) if en else None
    t_act = en["t0"] + m["active_s"] if en and m["active_s"] is not None else None
    sts = statuses(rows, t_act + LIMITS["settle_s"]) if t_act else []
    m["window_s"], m["window_req"] = (round(sts[-1][0] - sts[0][0], 1) if len(sts) > 1 else 0), a.window
    m["echo_rates"] = [g(s, "rate") for _t, s in sts if num(g(s, "rate")) and g(s, "rstate") == "ACTIVE"]
    m["echo_gaps"] = [g(s, "gaps") for _t, s in sts if num(g(s, "gaps")) and g(s, "rstate") == "ACTIVE"]
    m["echo_states"] = sorted({str(g(s, "rstate")) for _t, s in sts})
    docs = window_docs(fresh_docs(stand.rs.since(t_mac), t_mac))       # этого включения: записаны после t_mac
    m["block_rates"] = [ctl_of(d).get("rate_hz") for d in docs if num(ctl_of(d).get("rate_hz"))]
    m["block_gaps"] = [ctl_of(d).get("gaps") for d in docs if num(ctl_of(d).get("gaps"))]
    rest = echo_compare(sts)
    lat = latency_samples(sts)
    last = sts[-1][1] if sts else {}
    rep = {"median_ms": g(last, "lat_median"), "p99_ms": g(last, "lat_p99"), "samples": g(last, "lat_samples"),
           "source": get(last, "latency.source")} if sts else None
    m5 = {"samples": len(lat), "median_ms": round(statistics.median(lat), 1) if lat else None,
          "p99_ms": percentile(lat, 99), "max_ms": max(lat) if lat else None, "console": rep}
    return (judge_rate(m), m), rest, (judge_latency(lat, rep), m5)


def step_poses(a, stand, prompt):
    """ST-04, ручная часть: поза → Enter → опрос 2 с, пока поза удерживается (передача ACTIVE с окна)."""
    out = []
    for pose in POSES:
        prompt(f"ST-04: {pose.upper()}, держите и нажмите Enter (держать ещё ≈ 3 с)… ")
        rows = stand.sticks([{"op": "poll", "s": 2.0, "period": WINDOW_PERIOD_S}], 30)
        out.append({"pose": pose, "echo": echo_compare(statuses(rows))})
    prompt("ST-04: отпустите стики, переключатели верните как были; Enter… ")
    return out


def step_console_stop(a, stand):
    """ST-06: при ACTIVE — stop пульта (страховочный start), FAILSAFE на блоке, задержка по часам pi2, start пульта.
    → (вердикт, замеры, наблюдение ST-02 после перезапуска)."""
    m = {}
    ok, _rows = ensure_active(stand)
    if not ok:
        m["error"] = "передача не стала ACTIVE перед остановкой пульта"
        return judge_console_stop(m), m, None
    time.sleep(1.0)
    epoch = stand.board_epoch(stand.rs)
    S6.safety_start(a.console, CONSOLE_UNIT, 120)
    t0 = time.monotonic()
    m["stop_rc"], out = S6.systemctl(a.console, "stop", CONSOLE_UNIT)
    m["stop_out"] = out[-200:]
    dt, doc = stand.wait_doc(stand.rs, lambda d: ctl_of(d).get("state") == "FAILSAFE", t0, LIMITS["stop_detect_s"])
    m["failsafe_s"] = dt
    c = ctl_of(doc) if doc else stand.control()
    m.update(state=c.get("state"), reason=c.get("reason"), age_ms=c.get("age_ms"), since_s=c.get("since_s"),
             doc_time=(doc or {}).get("time"))
    m["journal_ok"], tr = journal_transitions(a, epoch)
    jr = next((x for x in tr if x["to"] == "FAILSAFE"), None)
    m["journal"] = jr
    if doc:
        m["delay_ms"], m["delay_src"] = failsafe_delay(c, doc.get("time"), jr)
    t1 = time.monotonic()
    m["start_rc"], _ = S6.systemctl(a.console, "start", CONSOLE_UNIT)
    m["console_back_s"] = None
    while time.monotonic() - t1 < LIMITS["console_start_s"]:
        rep = stand.status()
        if rep.get("result") == "ok" and g(sticks_of(rep), "support") == "ok":
            m["console_back_s"] = round(time.monotonic() - t1, 1)
            break
        time.sleep(2)
    after = observe_off(stand, "после перезапуска пульта", require_idle=False) if m["console_back_s"] else \
        {"when": "после перезапуска пульта", "error": "пульт не вернулся"}
    return judge_console_stop(m), m, after


def step_disable(a, stand):
    """ST-07: при ACTIVE — disable, IDLE (disabled), без FAILSAFE."""
    m = {}
    ok, _rows = ensure_active(stand)
    if not ok:
        m["error"] = "передача не стала ACTIVE перед disable"
        return judge_disable(m), m
    c = stand.control_after(time.monotonic(), 4)            # снимок ACTIVE этого включения: failsafes «до»
    m["failsafes0"], m["state0"] = c.get("failsafes"), c.get("state")
    epoch = stand.board_epoch(stand.rs)
    rows = stand.sticks([{"op": "disable"},
                         {"op": "poll", "s": LIMITS["idle_s"] + 4, "period": 0.05, "until": {P["rstate"]: "IDLE"}},
                         {"op": "poll", "s": 1.5, "period": 0.1}])
    d = op_row(rows, "disable")
    m["disable_result"] = d["r"].get("result") if d else None
    m["disable_detail"] = d["r"].get("detail") if d else "опросчик не ответил"
    m["idle_s"] = time_to(rows, d["t0"], remote_is("IDLE", "disabled")) if d else None
    seen = [f"эхо {g(s, 'rreason')}" for _t, s in statuses(rows, d["t0"] if d else None)
            if g(s, "rstate") == "FAILSAFE"]
    t_mac = time.monotonic()
    c = stand.control_after(t_mac, 4)
    m.update(failsafes1=c.get("failsafes"), block_state=c.get("state"), block_reason=c.get("reason"))
    seen += ["link-status" for doc in distinct([x for _m, _b, x in stand.rs.since(t_mac - 15)])
             if ctl_of(doc).get("state") == "FAILSAFE" and (doc.get("time") or 0) > epoch]
    m["journal_ok"], tr = journal_transitions(a, epoch)
    m["journal"] = [f"{x['from']} -> {x['to']} ({x['reason']})" for x in tr]
    seen += [f"журнал {x['from']} -> FAILSAFE ({x['reason']})" for x in tr if x["to"] == "FAILSAFE"]
    m["failsafe_seen"] = seen
    return judge_disable(m), m


def step_unplug(a, stand, prompt):
    """ST-08 (ручной): выдернуть и вернуть USB-кабель аппаратуры на pi1."""
    m = {}
    ok, _rows = ensure_active(stand)
    m["active_before"] = ok
    if not ok:
        return judge_unplug(m), m
    prompt("ST-08: ВЫДЕРНИТЕ USB-кабель аппаратуры из pi1 и нажмите Enter в тот же момент… ")
    t0 = time.monotonic()
    rows = stand.sticks([{"op": "poll", "s": LIMITS["device_lost_s"], "period": 0.2,
                          "until": {P["present"]: False, P["enabled"]: False, P["rstate"]: "FAILSAFE"}}])
    sts = statuses(rows)
    last = sts[-1][1] if sts else {}
    m.update(lost_present=g(last, "present"), lost_enabled=g(last, "enabled"),
             lost_echo=f"{g(last, 'rstate')} ({g(last, 'rreason')})")
    dt, doc = stand.wait_doc(stand.rs, lambda d: ctl_of(d).get("state") == "FAILSAFE", t0, 6)
    c = ctl_of(doc) if doc else stand.control()
    m.update(lost_state=c.get("state"), lost_reason=c.get("reason"), lost_doc_s=dt)
    prompt("ST-08: ВЕРНИТЕ USB-кабель аппаратуры в pi1 и нажмите Enter в тот же момент… ")
    rows = stand.sticks([{"op": "poll", "s": LIMITS["replug_s"], "period": 0.2, "until": {P["present"]: True}},
                         {"op": "poll", "s": LIMITS["replug_hold_s"], "period": 0.2}], LIMITS["replug_s"] + 40)
    sts = statuses(rows)
    t_back = next((t for t, s in sts if g(s, "present") is True), None)
    m["back_s"] = round(t_back - rows[0]["t0"], 1) if t_back is not None else None
    after = [s for t, s in sts if t_back is not None and t >= t_back]
    m["enabled_after"] = sum(1 for s in after if g(s, "enabled") is not False)
    m["active_after"] = sum(1 for s in after if g(s, "rstate") == "ACTIVE")
    c = stand.control_after(time.monotonic(), 4)
    m["state_after"] = f"{c.get('state')} ({c.get('reason')})"
    if c.get("state") == "ACTIVE":
        m["active_after"] += 1
    return judge_unplug(m), m


def restore(a, stand, initial_state):
    """finally: службы запущены (start только остановленной — без траты лимита запусков); блок возвращён в IDLE
    (enable → ACTIVE → disable), если был IDLE до прогона и аппаратура на месте; передача выключена."""
    out = {}
    for unit, host in ((REMOTE_UNIT, a.remote), (CONSOLE_UNIT, a.console)):
        st = E.out(host, f"systemctl is-active {unit}").strip()
        out[unit] = st
        if st != "active":
            out[f"start {unit}"] = S6.systemctl(host, "start", unit)[0]
    end = time.monotonic() + LIMITS["console_start_s"]
    rep = stand.status()
    while rep.get("result") != "ok" and time.monotonic() < end:
        time.sleep(2)
        rep = stand.status()
    s = sticks_of(rep)
    if initial_state == "IDLE" and g(s, "present") is True and g(s, "support") == "ok" and \
            stand.control_after(time.monotonic(), 3).get("state") != "IDLE":
        out["to_idle"] = ensure_active(stand)[0]
    rows = stand.sticks([{"op": "disable"},
                         {"op": "poll", "s": 3, "period": 0.1, "until": {P["enabled"]: False}}])
    d = op_row(rows, "disable")
    out["disable"] = d["r"].get("result") if d else None
    sts = statuses(rows)
    last = sts[-1][1] if sts else {}
    out["final_enabled"], out["final_remote"] = g(last, "enabled"), g(last, "rstate")
    out["final_block"] = stand.control_after(time.monotonic(), 3).get("state")
    return out


# ---------------------------------------------------------------- план и запуск


def plan(a):
    man = "по подсказкам" if a.manual else "нет — SKIP (--manual)"
    win = f"{(a.window + 8) / 60:.1f}".replace(".", ",")
    rows = [
        ("предпроверка", "серийные номера; OTA обеих плат в покое, не tryboot (s6_jr_accept.preflight); пульт отвечает "
                         "sticks status", ""),
        ("ST-10 до", "sheregesh-ota-client status, DEV-выпуск, образ, boot_id, failed — pi1 и pi2", "0,2"),
        ("ST-01", "sticks status: present, 8 осей, 24 кнопки, имя; ui-state sticks.present", "0,1"),
        ("ST-02", "старт: enabled=false (пульт и экран), блок IDLE", "0,1"),
        ("ST-03/04/05", f"enable → ACTIVE ≤ 1 с; {a.window:.0f} с опроса 20 Гц: rate_hz ≥ 45, gaps не растут; ch/btn "
                        f"блока = оси/кнопки ±1 (стики не трогать!); latency: медиана ≤ 30 мс, p99 ≤ 100 мс", win),
        ("ST-04 ручн.", f"движение стиков и переключатель: {man}", ""),
        ("ST-06", "ACTIVE → systemctl stop sheregesh-console → FAILSAFE (timeout | link-lost) ≤ 600 мс от последнего "
                  "кадра (часы pi2) → start пульта; ST-02 ещё раз: enabled=false, блок не ACTIVE", "1"),
        ("ST-07", "enable → ACTIVE → disable → IDLE (disabled) ≤ 1 с, без FAILSAFE, failsafes не вырос", "0,3"),
        ("ST-08", f"выдернуть/вернуть USB аппаратуры: {man}", ""),
        ("ST-09", "output = none во всех снимках control блока и rc-state", ""),
        ("ST-10 после", "то же, что «до», должно совпасть", "0,2"),
    ]
    out = [f"S7 ST — приёмка передачи стиков (ADR-0013 §5), pi1 {a.console} / pi2 {a.remote}, ≈ 3–5 мин:"]
    out += [f"  {k:<13} {t}" + (f"  ~{m} мин" if m else "") for k, t, m in rows]
    out += ["  finally: службы запущены (start только остановленной), блок снова IDLE, передача выключена.",
            "  Не трогает: OTA, слоты, видео, модуль, образ, юниты, polkit; без sudo; systemctl — только "
            "stop/start sheregesh-console (ST-06, лимит запусков — start_guard)."]
    return "\n".join(out)


def main(argv=None, prompt=None):
    global LOG
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console", default="auto")
    ap.add_argument("--remote", default="auto")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--only", help="через запятую: ST-01…ST-09 (ST-10 «до/после» выполняется всегда)")
    ap.add_argument("--window", type=float, default=LIMITS["window_s"], help="ST-03/05: окно замера, с")
    ap.add_argument("--manual", action="store_true", help="ST-04 движение стиков и ST-08 USB — по подсказкам")
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
    prompt = prompt or E.fresh_input
    a.out.mkdir(parents=True, exist_ok=True)
    LOG = S6.LOG = a.out / "run.log"                  # паузы start_guard — в тот же журнал
    ids, before, problems = S6.preflight(a)
    meta = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "boards": ids, "limits": LIMITS,
            "args": {k: str(v) for k, v in vars(a).items()}, "before": before}
    stand = Stand(a)
    rep = {}
    if not problems:
        rep = stand.status()
        if rep.get("result") != "ok":
            tail = rep.get("detail") or (stand.runs[-1].get("tail", "") if stand.runs else "")
            problems.append(f"pi1: пульт не ответил sticks status (не S7 или не запущен): {rep.get('result')!r} "
                            f"{str(tail)[-200:]!r}")
        meta["sticks_before"] = sticks_of(rep)
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
    blocked, initial_state, off_obs = None, None, []
    try:
        stand.wait(stand.rs, lambda d: bool(d), time.monotonic(), 10)
        stand.wait(stand.ui, lambda d: bool(d), time.monotonic(), 10)
        c0 = stand.control_after(time.monotonic() - 3, 4)
        initial_state = c0.get("state")
        meta["control_before"] = c0
        s0 = sticks_of(rep)
        if "ST-01" in only:
            t0 = time.monotonic()
            r = stand.ui.last()
            ui = r[2] if r else {}
            put("ST-01", *judge_device(rep, ui), {"sticks": s0, "ui": ui_sticks(ui)}, t0)
        if not c0:
            blocked = "в link-status.json блока нет control (блок не S7)"
        elif g(s0, "support") != "ok":
            blocked = f"sticks support {g(s0, 'support')!r} — пульт и блок не на S7 (1.4) или нет аппаратуры"
        elif g(s0, "present") is not True:
            blocked = "аппаратура не видна пульту (present ≠ true)"
        if "ST-02" in only:
            off_obs.append(observe_off(stand, "старт прогона", require_idle=True))

        def run(keys, fn):
            """fn() → {шаг: ((вердикт, подробности), замеры)}; шаги вне --only не выполняются и не пишутся."""
            keys = [k for k in keys if k in only]
            if not keys:
                return
            if blocked:
                for k in keys:
                    put(k, BLOCKED, blocked)
                return
            t0 = time.monotonic()
            for k, ((v, dd), m) in fn().items():
                if k in keys:
                    put(k, v, dd, m, t0)

        def window():
            (r3, m3), rest, (r5, m5) = step_window(a, stand)
            poses = step_poses(a, stand, prompt) if a.manual and "ST-04" in only else None
            return {"ST-03": (r3, m3), "ST-04": (judge_echo(rest, poses, a.manual), {"rest": rest, "poses": poses}),
                    "ST-05": (r5, m5)}

        def console_stop():
            r6, m6, after = step_console_stop(a, stand)
            if after and "ST-02" in only:
                off_obs.append(after)
            return {"ST-06": (r6, m6)}

        run(("ST-03", "ST-04", "ST-05"), window)
        run(("ST-06",), console_stop)
        run(("ST-07",), lambda: {"ST-07": step_disable(a, stand)})
        if "ST-08" in only and not a.manual:
            put("ST-08", "SKIP", "ручной шаг: --manual (USB-кабель аппаратуры выдёргивает человек)")
        else:
            run(("ST-08",), lambda: {"ST-08": step_unplug(a, stand, prompt)})
        if "ST-02" in only:
            put("ST-02", *judge_default_off(off_obs), {"observations": off_obs})
    finally:
        try:
            meta["cleanup"] = restore(a, stand, initial_state)
        except Exception as e:                                       # noqa: BLE001 — итог пишется в любом случае
            meta["cleanup"] = {"error": repr(e)}
        log(f"восстановление: {meta['cleanup']}")
        stand.ui.stop()
        stand.rs.stop()
    if "ST-09" in only:
        put("ST-09", *judge_output(stand.outputs_all()))
    t0 = time.monotonic()
    after = S6.boards(a)
    meta["after"] = after
    v, p = S6.judge_ota(before, after)
    put("ST-10", v, "; ".join(p) or "OTA, слот, tryboot, образ, DEV-выпуск и boot_id прежние на обеих платах; "
                                    "failed пуст", {"before": before, "after": after}, t0)
    for k in STEPS:
        results.setdefault(k, {"verdict": "SKIP", "detail": "не запрошен (--only)"})
    meta["sticks_runs"] = stand.runs
    c = meta.get("cleanup") or {}
    if c.get("error") or c.get("final_enabled") is not False:
        meta["cleanup_warning"] = "выключение передачи не подтверждено — см. cleanup"
        log("ВНИМАНИЕ: " + meta["cleanup_warning"])
    summary = {**meta, "duration_min": round((time.monotonic() - t_run) / 60, 1), "durations_s": durations,
               "results": {k: results[k] for k in STEPS}, "verdict": verdict_of(results)}
    (a.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    log(f"ИТОГ S7 ST {summary['verdict']} за {summary['duration_min']} мин: " +
        ", ".join(f"{k} {results[k]['verdict']}" for k in STEPS))
    return {"PASS": 0, "FAIL": 1, "ERROR": 2, BLOCKED: 3, "SKIP": 2}[summary["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
