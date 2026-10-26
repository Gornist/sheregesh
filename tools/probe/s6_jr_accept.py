#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Сокращённая приёмка S6 «внешний модуль JR» (ADR-0012 §11, JR-01…JR-10) на двух платах за 5–10 мин
(docs/acceptance/s6-jr/PLAN.md). С машины владельца по SSH от sheregesh-ui, без sudo.

  python3 tools/probe/s6_jr_accept.py --plan                                     # только план, без SSH
  python3 tools/probe/s6_jr_accept.py --console 192.168.1.92 --remote 192.168.1.171 --out DIR \\
      [--only JR-05,F-03,…] [--hold 30] [--reboot] [--manual-ethernet]

Модуль физически не подключён: на pi2 работает только лабораторный эмулятор (дочерний процесс sheregesh-remote-net).
Команды — на pi1 через работающий пульт: `python3 -m sheregesh.link.modulectl … --via console` (тот же путь, что
кнопки экрана «Модуль»: ModuleView в сеансе пульта, второго сеанса с блоком нет). Состояние GUI — выборка
ui-state.json pi1 (ключ module: support, state, stateText, errorText, faultEvent…, ADR §10.4), состояние блока —
выборка link-status.json pi2 (module = ModuleService.health()), процессы — ps на pi2, история — status.module.history.

  JR-01  эмулятор на pi2: backend running, ровно один процесс jrmodule, потомок MainPID sheregesh-remote-net
  JR-02  профиль Nomad: telemetry.profile radiomaster-nomad / «RadioMaster Nomad», profileText, плашка «лабораторный»
  JR-03  reinit --expect READY: INITIALIZING → READY (init-ok), init_ms ≤ 5000, READY ≤ 15 с
  JR-04  GUI «Готов» не позже 2 с после READY на блоке
  JR-05  lab detach → «Модуль отсутствует» ≤ 5 с; lab attach → READY ≤ 15 с
  JR-06  lab iface-error → «Ошибка…», «Ошибка интерфейса», MODULE-FAULT ≤ 6 с; lab hang → первый FAULT MOD-NO-RESPONSE
  JR-07  lab heal → READY ≤ 20 с через RECOVERING (recover-attempt, без reset/reinit), последняя ошибка видна
  JR-08  (a) lab crash, (b) SIGKILL процесса эмулятора → EMU-EXITED → READY ≤ 15 с, новый PID, restarts +1;
         (c) lab emu-unavailable → EMU-UNAVAILABLE ≤ 30 с, удержание --hold без процессов и перезапусков, reset → READY;
         (d) systemctl restart sheregesh-remote-net → no-link → READY ≤ 30 с, процесс один, родитель — новый MainPID
  JR-09  configure enabled=false → restart службы → DISABLED, процессов нет, ревизия та же, source store →
         enabled=true → READY; при --reboot — то же через перезагрузку pi2 (только COMMITTED и tryboot_now=false)
  JR-10  OTA и выпуск не изменились: sheregesh-ota-client status, DEV-выпуск, образ, boot_id на обеих платах,
         systemctl --failed пуст
  F-01   (§12.4) pi1 раньше pi2: блок остановлен → «нет связи», «Готов» по старым данным не показывается → READY
  F-02   (§12.4) pi2 раньше pi1 и перезапуск GUI: пульт остановлен, блок перезапущен (READY автономно ≤ 15 с),
         пульт запущен → READY и история из телеметрии
  F-03   (§12.4, §8) настройки: каталог отсутствует → factory; state.json повреждён → recovered (CFG-…);
         все ревизии повреждены → reset (remote-config.corrupt-*); исходный каталог возвращается
  F-04   (§12.4) Ethernet pi2 — ТОЛЬКО вручную (--manual-ethernet): человек вынимает и возвращает кабель pi2;
         интерфейсы на платах скрипт не трогает (SSH оборвался бы)
  OUT    поле output = "none" во всех ответах modulectl (ADR §12.3)

Не трогает OTA, слоты, видео, образ, юниты, polkit. systemctl — только start/stop/restart sheregesh-remote-net и
sheregesh-console (правило 50-sheregesh-dev.rules), reboot pi2 — только с --reboot и только при COMMITTED и
tryboot_now=false. Каталог настроек модуля (~/.config/sheregesh/remote-config) в F-03 переносится, а не удаляется;
исходный возвращается в finally, остатки — в ~/.config/sheregesh/s6-jr-<время>/. Вердикты PASS | FAIL | ERROR |
BLOCKED | SKIP; итог FAIL > ERROR > BLOCKED > PASS > SKIP (s5_accept.verdict_of). Коды выхода: 0 PASS, 1 FAIL,
2 ERROR, 3 BLOCKED.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/probe"))
sys.path.insert(0, str(ROOT / "tools/deploy/ota-check"))
import s4_accept as S4  # noqa: E402
import s5_accept as S5  # noqa: E402
import s5_pi2_accept as P2  # noqa: E402

E = S4.E
STEPS = ("JR-01", "JR-02", "JR-03", "JR-04", "JR-05", "JR-06", "JR-07", "JR-08", "JR-09", "JR-10",
         "F-01", "F-02", "F-03", "F-04", "OUT")
BLOCKED = S5.BLOCKED
REMOTE_UNIT, CONSOLE_UNIT = E.REMOTE_UNIT, E.CONSOLE_UNIT
REL = P2.REL
EMU_MARK = "sheregesh_emulators.jrmodule"
PROFILE_ID, PROFILE_NAME = "radiomaster-nomad", "RadioMaster Nomad"
RCDIR = "$HOME/.config/sheregesh/remote-config"           # ADR §8: dirname(link.json)/remote-config
CFG_PARENT = "$HOME/.config/sheregesh"
# ADR §11 (LIMITS) + свои: gui_lag_s (JR-04), console_start_s (F-02: QML на Pi 4), eth_* (REQ-SYS-002: 5 с / 10 с +
# первый status блока после переподключения).
LIMITS = {"ready_s": 15.0, "detect_s": 5.0, "fault_s": 6.0, "recover_s": 20.0, "crash_recover_s": 15.0,
          "unavailable_s": 30.0, "hold_s": 30.0, "service_restart_s": 30.0, "reboot_s": 300.0,
          "gui_lag_s": 2.0, "console_start_s": 60.0, "eth_detect_s": 5.0, "eth_back_s": 15.0}
INIT_MS_MAX = 5000                                         # profile.init_timeout_s Nomad
# Тексты GUI (ADR §4.1, §4.4, §10.3). FAULT с MOD-IFACE-ERROR: §4.1 даёт «Ошибка интерфейса», JR-06 — «Ошибка»;
# принимаются оба (начало «Ошибка»), «Ошибка интерфейса» обязательно в errorText.
STATE_TEXT = {"DISCONNECTED": "Модуль отсутствует", "DETECTED": "Модуль обнаружен", "INITIALIZING": "Инициализация",
              "READY": "Готов", "FAULT": "Ошибка", "RECOVERING": "Восстановление", "DISABLED": "Отключён"}
CODE_TEXT = {"MOD-IFACE-ERROR": "Ошибка интерфейса", "MOD-NO-RESPONSE": "Модуль не отвечает",
             "MOD-INIT-TIMEOUT": "Инициализация не завершилась", "MOD-INIT-FAILED": "Ошибка инициализации",
             "MOD-PROFILE-MISMATCH": "Модуль не соответствует профилю",
             "MOD-RECOVER-TIMEOUT": "Восстановление не завершилось", "EMU-START-FAILED": "Эмулятор не запустился",
             "EMU-EXITED": "Эмулятор аварийно завершился", "EMU-NO-KEEPALIVE": "Эмулятор не отвечает",
             "EMU-UNAVAILABLE": "Эмулятор недоступен"}
LAB_BANNER = "Лабораторный эмулятор"
LOG = None


def log(msg):
    line = time.strftime("%H:%M:%S") + " " + msg
    print(line, flush=True)
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------------------------------------------------------------- чистые функции (тесты: test_s6_jr_accept.py)


def verdict_of(results):
    return S5.verdict_of(results)


agg = P2.agg


def parse_cli(text):
    """Вывод modulectl (JSON, возможно после строк журнала и/или многострочный) → dict; {} — JSON нет."""
    dec = json.JSONDecoder()
    text = text or ""
    i = text.find("{")
    while i >= 0:
        try:
            v, _end = dec.raw_decode(text[i:])
            if isinstance(v, dict) and "result" in v:
                return v
        except ValueError:
            pass
        i = text.find("{", i + 1)
    return {}


def telemetry(cli):
    """status.module после sanitize из ответа modulectl (M.telemetry); {} — нет."""
    m = (cli or {}).get("module") or {}
    t = m.get("telemetry") if isinstance(m, dict) else None
    return t if isinstance(t, dict) else {}


def ui_mod(doc):
    m = (doc or {}).get("module")
    return m if isinstance(m, dict) else {}


def rs_mod(doc):
    m = (doc or {}).get("module")
    return m if isinstance(m, dict) else {}


def ui_state_is(state, text=None):
    """Предикат выборки ui-state: support ok и состояние state (и текст плашки text)."""
    def pred(d):
        m = ui_mod(d)
        return m.get("support") == "ok" and not m.get("stale") and m.get("state") == state and \
            (text is None or m.get("stateText") == text)
    return pred


def rs_state_is(state, seq_gt=None, code=None, after_epoch=None):
    """Предикат выборки link-status блока: module.state (и seq > seq_gt, error_code, запись после after_epoch)."""
    def pred(d):
        m = rs_mod(d)
        return m.get("state") == state and (seq_gt is None or (m.get("seq") or 0) > seq_gt) and \
            (code is None or m.get("error_code") == code) and \
            (after_epoch is None or (d.get("time") or 0) > after_epoch)
    return pred


def first_row(rows, t0, pred):
    """(секунды от t0, документ) первой выборки с pred или (None, None)."""
    for mono, _b, doc in rows:
        if mono >= t0 and doc and pred(doc):
            return round(mono - t0, 2), doc
    return None, None


def history_after(hist, seq0):
    """Переходы status.module.history с seq > seq0 по возрастанию seq (в телеметрии новые первыми)."""
    rows = [h for h in hist or [] if isinstance(h, dict) and isinstance(h.get("seq"), int) and h["seq"] > seq0]
    return sorted(rows, key=lambda h: h["seq"])


def has_transition(entries, frm, to, reason=None):
    return any(h.get("from") == frm and h.get("state") == to and (reason is None or h.get("reason") == reason)
               for h in entries)


def first_fault_code(entries):
    """Код ошибки первого перехода в FAULT (entries по возрастанию seq) или None."""
    return next((h.get("code") for h in entries if h.get("state") == "FAULT"), None)


def parse_procs(text):
    """Вывод PROC_CMD: строка systemctl show (KEY=VALUE через пробел), затем ps -e -o pid=,ppid=,args=."""
    lines = (text or "").splitlines()
    show = dict(kv.split("=", 1) for kv in (lines[0].split() if lines else []) if "=" in kv)
    procs = []
    for ln in lines[1:]:
        parts = ln.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            procs.append({"pid": int(parts[0]), "ppid": int(parts[1]), "args": parts[2] if len(parts) > 2 else ""})
    main = show.get("MainPID", "")
    return {"show": show, "mainpid": int(main) if main.isdigit() else None, "procs": procs,
            "nrestarts": int(show["NRestarts"]) if show.get("NRestarts", "").isdigit() else None,
            "active": show.get("ActiveState")}


def emulator_pids(snap):
    """Процессы эмулятора модуля: python3 -s -m sheregesh_emulators.jrmodule (не сторож pdeath, не grep)."""
    return sorted(p["pid"] for p in (snap or {}).get("procs", [])
                  if EMU_MARK in p["args"] and "pdeath" not in p["args"] and "grep" not in p["args"].split()[:1])


def ancestors(pid, snap):
    """Цепочка родителей pid по снимку ps (без самого pid)."""
    parent = {p["pid"]: p["ppid"] for p in (snap or {}).get("procs", [])}
    out, cur = [], parent.get(pid)
    while cur and cur not in out and cur != 1:
        out.append(cur)
        cur = parent.get(cur)
    return out


def judge_emulator(snap, health):
    """JR-01: служба active, backend_state running, ровно один процесс эмулятора, его предок — MainPID службы.
    На pi2 (setpriv → sh exec → python3) родитель прямой; на машине без setpriv между ними сторож pdeath.py."""
    p = []
    if not health:
        return "FAIL", "в link-status.json блока нет module (подсистема модуля не запущена или блок не S6)"
    if snap.get("active") != "active":
        p.append(f"{REMOTE_UNIT} {snap.get('active')!r}")
    if health.get("backend_state") != "running":
        p.append(f"backend_state {health.get('backend_state')!r}, ожидался running")
    emus = emulator_pids(snap)
    if len(emus) != 1:
        p.append(f"процессов эмулятора {len(emus)}: {emus}")
    main = snap.get("mainpid")
    for e in emus:
        if main not in ancestors(e, snap):
            p.append(f"эмулятор {e} не потомок MainPID {main} (родители {ancestors(e, snap)[:3]})")
    if emus and health.get("pid") and health["pid"] not in emus and \
            not any(health["pid"] in ancestors(e, snap) for e in emus):     # без setpriv pid — у сторожа pdeath
        p.append(f"link-status pid {health['pid']} ≠ процесс {emus}")
    direct = bool(emus) and ancestors(emus[0], snap)[:1] == [main]
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"эмулятор PID {emus[0]}, {'родитель' if direct else 'предок'} — MainPID {main}, backend running")


def judge_profile(cli, ui):
    """JR-02: профиль Nomad в телеметрии (через пульт) и на экране; плашка «лабораторный эмулятор»."""
    t, m, p = telemetry(cli), ui_mod(ui), []
    if not cli:
        return "ERROR", "modulectl status не ответил JSON"
    mod = cli.get("module") or {}
    if mod.get("support") != "ok":
        return "FAIL", f"support {mod.get('support')!r} (сессия {mod.get('session')!r}), ожидался ok и 1.3"
    prof = t.get("profile") or {}
    if prof.get("id") != PROFILE_ID or prof.get("name") != PROFILE_NAME:
        p.append(f"telemetry.profile {prof!r}")
    if not m:
        p.append("в ui-state.json пульта нет module (GUI не S6?)")
    else:
        if PROFILE_NAME not in str(m.get("profileText") or ""):
            p.append(f"profileText {m.get('profileText')!r}")
        if m.get("labBanner") is not True:
            p.append(f"labBanner {m.get('labBanner')!r} (плашка «лабораторный эмулятор» обязана быть видна)")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"{prof.get('name')} ({prof.get('id')}), «{m.get('profileText')}», плашка видна, сессия {mod.get('session')}")


def judge_init(m):
    """JR-03: reinit --expect READY ok; в истории INITIALIZING → READY (init-ok); init_ms ≤ 5000; READY ≤ ready_s."""
    p = []
    if m.get("result") != "ok" or m.get("rc") != 0:
        p.append(f"reinit: rc {m.get('rc')} result {m.get('result')!r} {m.get('detail', '')}")
    if not has_transition(m.get("history") or [], "INITIALIZING", "READY", "init-ok"):
        p.append(f"в истории после reinit нет INITIALIZING → READY (init-ok): "
                 f"{[(h.get('from'), h.get('state'), h.get('reason')) for h in m.get('history') or []]}")
    im = m.get("init_ms")
    if not isinstance(im, int) or im > INIT_MS_MAX or im <= 0:
        p.append(f"diag.init_ms {im!r} (нужно 0 < init_ms ≤ {INIT_MS_MAX})")
    if m.get("ready_s") is None or m["ready_s"] > LIMITS["ready_s"]:
        p.append(f"READY на блоке через {m.get('ready_s')} с (предел {LIMITS['ready_s']:.0f})")
    return ("FAIL" if p else "PASS"), "; ".join(p) or f"READY за {m['ready_s']} с, init_ms {im}"


def judge_gui_ready(m):
    """JR-04: ui-state READY и «Готов» не позже gui_lag_s после READY в link-status блока."""
    if m.get("rs_ready_s") is None:
        return "ERROR", "READY на блоке не наблюдался — сравнивать не с чем"
    if m.get("ui_ready_s") is None:
        return "FAIL", f"пульт не показал «{STATE_TEXT['READY']}» (support ok) за {m.get('timeout_s')} с"
    lag = round(m["ui_ready_s"] - m["rs_ready_s"], 2)
    if lag > LIMITS["gui_lag_s"]:
        return "FAIL", f"«Готов» на пульте через {lag} с после READY блока (предел {LIMITS['gui_lag_s']:.0f})"
    return "PASS", f"«Готов» через {lag} с после READY блока (≤ {LIMITS['gui_lag_s']:.0f})"


def judge_detach(m):
    """JR-05: detach → «Модуль отсутствует» ≤ detect_s; attach → «Готов» ≤ ready_s."""
    p = []
    for k in ("detach", "attach"):
        if m.get(f"{k}_result") != "ok":
            p.append(f"lab {k}: {m.get(f'{k}_result')!r}")
    if m.get("absent_s") is None or m["absent_s"] > LIMITS["detect_s"]:
        p.append(f"«{STATE_TEXT['DISCONNECTED']}» через {m.get('absent_s')} с (предел {LIMITS['detect_s']:.0f})")
    if m.get("ready_s") is None or m["ready_s"] > LIMITS["ready_s"]:
        p.append(f"после attach «Готов» через {m.get('ready_s')} с (предел {LIMITS['ready_s']:.0f})")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"«{STATE_TEXT['DISCONNECTED']}» {m['absent_s']} с, «Готов» после attach {m['ready_s']} с")


def judge_fault_shown(m, kind):
    """JR-06. iface: FAULT, «Ошибка…», errorText «Ошибка интерфейса», faultEvent ≤ fault_s.
    hang: первый FAULT после команды — MOD-NO-RESPONSE ≤ fault_s (дальше MOD-INIT-TIMEOUT допустим)."""
    p = []
    if m.get("result") != "ok":
        p.append(f"lab {m.get('scenario')}: {m.get('result')!r}")
    if kind == "iface":
        if m.get("fault_s") is None or m["fault_s"] > LIMITS["fault_s"]:
            p.append(f"«{CODE_TEXT['MOD-IFACE-ERROR']}» на пульте через {m.get('fault_s')} с "
                     f"(предел {LIMITS['fault_s']:.0f})")
        elif not str(m.get("state_text") or "").startswith(STATE_TEXT["FAULT"]):
            p.append(f"stateText {m.get('state_text')!r}, ожидалось «{STATE_TEXT['FAULT']}…»")
        if m.get("fault_event_s") is None or m["fault_event_s"] > LIMITS["fault_s"]:
            p.append(f"событие MODULE-FAULT (faultEvent) через {m.get('fault_event_s')} с")
        ok = f"«{m.get('state_text')}», «{CODE_TEXT['MOD-IFACE-ERROR']}» {m.get('fault_s')} с, MODULE-FAULT"
    else:
        if m.get("first_code") != "MOD-NO-RESPONSE":
            p.append(f"первый FAULT {m.get('first_code')!r}, ожидался MOD-NO-RESPONSE")
        if m.get("fault_s") is None or m["fault_s"] > LIMITS["fault_s"]:
            p.append(f"FAULT на блоке через {m.get('fault_s')} с (предел {LIMITS['fault_s']:.0f})")
        if m.get("ui_fault_s") is None or m["ui_fault_s"] > LIMITS["fault_s"] + LIMITS["gui_lag_s"]:
            p.append(f"«{STATE_TEXT['FAULT']}» на пульте через {m.get('ui_fault_s')} с")
        ok = f"первый FAULT MOD-NO-RESPONSE {m.get('fault_s')} с, на пульте {m.get('ui_fault_s')} с"
    return ("FAIL" if p else "PASS"), "; ".join(p) or ok


def judge_recovered(m, codes):
    """JR-07: heal → READY ≤ recover_s без reset/reinit; в истории FAULT → RECOVERING (recover-attempt);
    последняя ошибка (код из codes, active=false) видна в errorText; faultEvent снято."""
    p = []
    if m.get("result") != "ok":
        p.append(f"lab heal: {m.get('result')!r}")
    if m.get("ready_s") is None or m["ready_s"] > LIMITS["recover_s"]:
        p.append(f"READY через {m.get('ready_s')} с после heal (предел {LIMITS['recover_s']:.0f})")
    hist = m.get("history") or []
    if not has_transition(hist, "FAULT", "RECOVERING", "recover-attempt"):
        p.append("в истории нет FAULT → RECOVERING (recover-attempt)")
    cmds = sorted({h.get("reason") for h in hist if str(h.get("reason", "")).startswith("cmd-")})
    if cmds:
        p.append(f"переходы по командам {cmds} — восстановление не автоматическое")
    err = m.get("error") or {}
    if err.get("code") not in codes or err.get("active") is not False:
        p.append(f"последняя ошибка {err.get('code')!r} active={err.get('active')!r}, ожидалась одна из "
                 f"{sorted(codes)} (active=false)")
    elif CODE_TEXT[err["code"]] not in str(m.get("error_text") or ""):
        p.append(f"errorText {m.get('error_text')!r} без «{CODE_TEXT[err['code']]}»")
    if m.get("ui_ready_s") is None:
        p.append("пульт не показал «Готов» со снятым MODULE-FAULT")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"READY {m['ready_s']} с через RECOVERING, последняя ошибка {err.get('code')} видна, MODULE-FAULT снято")


def judge_crash(m):
    """JR-08 (a)/(b): FAULT EMU-EXITED, READY ≤ crash_recover_s, новый PID, restarts +1, процесс один."""
    p = []
    if m.get("error"):
        return "ERROR", m["error"]
    if m.get("fault_code") != "EMU-EXITED":
        p.append(f"FAULT {m.get('fault_code')!r}, ожидался EMU-EXITED")
    if m.get("ready_s") is None or m["ready_s"] > LIMITS["crash_recover_s"]:
        p.append(f"READY через {m.get('ready_s')} с (предел {LIMITS['crash_recover_s']:.0f})")
    if not m.get("pid1") or m.get("pid1") == m.get("pid0"):
        p.append(f"PID эмулятора {m.get('pid0')} → {m.get('pid1')} (нужен новый)")
    if m.get("restarts1") != (m.get("restarts0") or 0) + 1:
        p.append(f"restarts {m.get('restarts0')} → {m.get('restarts1')} (нужно +1)")
    if m.get("emulators") != 1:
        p.append(f"процессов эмулятора {m.get('emulators')}")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"EMU-EXITED → READY {m['ready_s']} с, PID {m['pid0']} → {m['pid1']}, restarts {m['restarts1']}")


def judge_unavailable(m):
    """JR-08 (c): EMU-UNAVAILABLE ≤ unavailable_s; за удержание нет процессов, restarts не растёт, нет новых
    «emulator started» (если журнал доступен); после emu-available + reset — READY ≤ ready_s."""
    p = []
    if m.get("unavail_s") is None or m["unavail_s"] > LIMITS["unavailable_s"]:
        p.append(f"FAULT EMU-UNAVAILABLE через {m.get('unavail_s')} с (предел {LIMITS['unavailable_s']:.0f})")
    else:
        if m.get("procs_during"):
            p.append(f"за удержание процессы эмулятора: {m['procs_during']}")
        if m.get("restarts_grew"):
            p.append(f"restarts за удержание: {m.get('restarts_hold')}")
        if m.get("running_seen"):
            p.append("за удержание backend_state running")
        if m.get("journal_started"):
            p.append(f"за удержание в журнале «emulator started»: {m['journal_started']}")
    for k in ("available_result", "reset_result"):
        if m.get(k) != "ok":
            p.append(f"{k.split('_')[0]}: {m.get(k)!r}")
    if m.get("ready_s") is None or m["ready_s"] > LIMITS["ready_s"]:
        p.append(f"после reset READY через {m.get('ready_s')} с (предел {LIMITS['ready_s']:.0f})")
    note = "" if m.get("journal_ok") else " (журнал службы недоступен — только процессы и счётчик)"
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"EMU-UNAVAILABLE {m['unavail_s']} с, {m.get('hold_s')} с без процессов и попыток, "
        f"reset → READY {m['ready_s']} с{note}")


def judge_service_restart(m):
    """JR-08 (d): no-link на пульте, затем READY ≤ service_restart_s; эмулятор один, его предок — новый MainPID,
    прежний процесс исчез; NRestarts службы блока и пульта не выросли (рестарт ручной, не аварийный)."""
    p = []
    if m.get("rc") != 0:
        return "ERROR", f"systemctl restart: rc {m.get('rc')} {m.get('out', '')}"
    if m.get("mainpid1") in (None, 0, m.get("mainpid0")):
        return "ERROR", f"MainPID {m.get('mainpid0')} → {m.get('mainpid1')} — перезапуска не было"
    if m.get("nolink_s") is None:
        p.append("пульт не показал no-link при перезапуске блока")
    if m.get("ready_s") is None or m["ready_s"] > LIMITS["service_restart_s"]:
        p.append(f"READY на пульте через {m.get('ready_s')} с (предел {LIMITS['service_restart_s']:.0f})")
    if m.get("emulators") != 1 or not m.get("parent_ok"):
        p.append(f"эмуляторов {m.get('emulators')}, предок — новый MainPID: {m.get('parent_ok')}")
    if m.get("old_alive"):
        p.append(f"прежний процесс эмулятора {m.get('emu_pid0')} жив (сирота)")
    for role in ("remote", "console"):
        b, a = m.get(f"nrestarts_{role}0"), m.get(f"nrestarts_{role}1")
        if b is None or a is None or a != b:
            p.append(f"NRestarts {role}: {b} → {a}")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"no-link {m['nolink_s']} с, READY {m['ready_s']} с, MainPID {m['mainpid0']} → {m['mainpid1']}, "
        f"эмулятор один, сирот нет, NRestarts прежние")


def judge_settings(m):
    """JR-09 → (вердикт, подробности). Подшаг перезагрузки: verdict reboot_verdict (None — не запрошен)."""
    p, vs = [], []
    if m.get("off_result") != "ok":
        return "ERROR", f"configure enabled=false: {m.get('off_result')!r} {m.get('off_detail', '')}"
    for ev in m.get("events") or []:
        q = []
        if ev.get("error"):
            vs.append(ev.get("verdict", "ERROR"))
            p.append(f"{ev['event']}: {ev['error']}")
            continue
        if ev.get("disabled_s") is None:
            q.append(f"DISABLED не наблюдался за {ev.get('timeout_s')} с")
        if "ui_disabled_s" in ev and ev["ui_disabled_s"] is None:
            q.append(f"пульт не показал «{STATE_TEXT['DISABLED']}» в новом сеансе (no-link {ev.get('nolink_s')} с)")
        if ev.get("state") != "DISABLED":
            q.append(f"состояние {ev.get('state')!r}")
        if ev.get("revision") != m.get("revision") or not m.get("revision"):
            q.append(f"ревизия {ev.get('revision')!r}, после configure {m.get('revision')!r}")
        if ev.get("source") != "store":
            q.append(f"source {ev.get('source')!r}, ожидался store")
        if ev.get("emulators"):
            q.append(f"процессы эмулятора при DISABLED: {ev['emulators']}")
        vs.append("FAIL" if q else "PASS")
        p += [f"{ev['event']}: {x}" for x in q]
    if m.get("on_result") != "ok":
        p.append(f"configure enabled=true: {m.get('on_result')!r}")
        vs.append("FAIL")
    if m.get("ready_s") is None or m["ready_s"] > LIMITS["ready_s"]:
        p.append(f"после enabled=true READY через {m.get('ready_s')} с")
        vs.append("FAIL")
    v = agg(vs or ["PASS"])
    evs = ", ".join(e["event"] for e in m.get("events") or [])
    return v, "; ".join(p) or f"DISABLED, ревизия {m.get('revision')} (store) пережила {evs}; enabled=true → READY " \
                              f"{m.get('ready_s')} с"


def reboot_allowed(o):
    """Перезагрузка pi2 в JR-09 допустима только в покое OTA: COMMITTED, tryboot_now=false, загружен
    подтверждённый слот, состояние согласовано (иначе прошивка вернёт другой слот или собьёт пробу)."""
    if o is None:
        return False, "агент OTA pi2 недоступен"
    p = []
    if o.get("state") != "COMMITTED":
        p.append(f"OTA {o.get('state')!r}, нужен COMMITTED")
    if o.get("tryboot_now") is not False:
        p.append(f"tryboot_now={o.get('tryboot_now')!r}")
    if o.get("slot") != o.get("committed"):
        p.append(f"загружен {o.get('slot')!r}, подтверждён {o.get('committed')!r}")
    if o.get("consistent") is not True:
        p.append(f"consistent={o.get('consistent')!r}")
    return (not p), "; ".join(p) or f"COMMITTED {o.get('committed')}, tryboot_now=false"


BOARD_CMD = ("echo '== ota'; sheregesh-ota-client status 2>/dev/null | tr -d '\\n'; echo; "
             "echo '== deploy'; readlink /var/lib/sheregesh-deploy/current 2>/dev/null || echo factory; "
             "echo '== boot'; cat /proc/sys/kernel/random/boot_id; "
             "echo '== build'; tr -d '\\n' < /usr/share/sheregesh/build.json 2>/dev/null; echo; "
             "echo '== failed'; systemctl --failed --no-legend --plain")


def board_state(text):
    """Вывод BOARD_CMD → {ota (s5_pi2_accept.ota_summary), deploy, boot_id, build_commit, failed}."""
    sec, cur = {}, None
    for ln in (text or "").splitlines():
        if ln.startswith("== "):
            cur = ln[3:].strip()
            sec[cur] = []
        elif cur is not None:
            sec[cur].append(ln)
    get = lambda k: "\n".join(sec.get(k, [])).strip()  # noqa: E731
    return {"ota": P2.ota_summary(E.jload(get("ota"))), "deploy": get("deploy") or None,
            "boot_id": get("boot") or None, "build_commit": E.jload(get("build")).get("commit"),
            "failed": [x for x in get("failed").splitlines() if x.strip()]}


OTA_KEYS = ("state", "committed", "slot", "tryboot_now", "target", "version_active", "consistent", "default_part",
            "commit", "ota_version")


def judge_ota(before, after, rebooted=False):
    """JR-10: на обеих платах OTA, слот, tryboot, образ, DEV-выпуск и boot_id прежние (boot_id pi2 — кроме --reboot);
    systemctl --failed пуст после прогона."""
    p, err = [], []
    for board in ("pi1", "pi2"):
        b, a = (before or {}).get(board), (after or {}).get(board)
        if not b or not a or b.get("ota") is None or a.get("ota") is None:
            err.append(f"{board}: нет состояния OTA «до» или «после»")
            continue
        for k in OTA_KEYS:
            if b["ota"].get(k) != a["ota"].get(k):
                p.append(f"{board} ota.{k}: {b['ota'].get(k)!r} → {a['ota'].get(k)!r}")
        for k in ("deploy", "build_commit"):
            if b.get(k) != a.get(k):
                p.append(f"{board} {k}: {b.get(k)!r} → {a.get(k)!r}")
        if b.get("boot_id") != a.get("boot_id") and not (board == "pi2" and rebooted):
            p.append(f"{board} перезагружался (boot_id сменился)")
        if a.get("failed"):
            p.append(f"{board} failed-юниты: {a['failed']}")
    if p:
        return "FAIL", p
    return ("ERROR", err) if err else ("PASS", [])


def judge_no_link_gap(m):
    """F-01: блок остановлен — «нет связи» ≤ detect_s, «Готов» по старым данным не показан; после start — READY."""
    p = []
    if m.get("stop_rc") != 0:
        return "ERROR", f"systemctl stop: rc {m.get('stop_rc')}"
    if m.get("nolink_s") is None or m["nolink_s"] > LIMITS["detect_s"]:
        p.append(f"no-link на пульте через {m.get('nolink_s')} с (предел {LIMITS['detect_s']:.0f})")
    if m.get("ready_during_gap"):
        p.append(f"во время остановки блока пульт показывал «Готов» ({m['ready_during_gap']} выборок)")
    if m.get("start_rc") != 0:
        p.append(f"systemctl start: rc {m.get('start_rc')}")
    if m.get("ready_s") is None or m["ready_s"] > LIMITS["service_restart_s"]:
        p.append(f"после start READY через {m.get('ready_s')} с (предел {LIMITS['service_restart_s']:.0f})")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"no-link {m['nolink_s']} с, «Готов» по старым данным не показан ({m.get('gap_docs')} выборок), "
        f"после start READY {m['ready_s']} с")


def judge_unit_first(m):
    """F-02: пульт остановлен, блок перезапущен → READY автономно ≤ ready_s; пульт запущен → READY ≤ console_start_s
    из телеметрии (история не пуста), до первого status — не «Готов»."""
    p = []
    for k in ("console_stop_rc", "remote_restart_rc", "console_start_rc"):
        if m.get(k) != 0:
            return "ERROR", f"{k}: rc {m.get(k)}"
    if m.get("unit_ready_s") is None or m["unit_ready_s"] > LIMITS["ready_s"]:
        p.append(f"блок без пульта дошёл до READY через {m.get('unit_ready_s')} с (предел {LIMITS['ready_s']:.0f})")
    if m.get("ui_ready_s") is None or m["ui_ready_s"] > LIMITS["console_start_s"]:
        p.append(f"новый пульт показал «Готов» через {m.get('ui_ready_s')} с (предел "
                 f"{LIMITS['console_start_s']:.0f})")
    if m.get("ready_without_link"):
        p.append(f"«Готов» при support ≠ ok: {m['ready_without_link']} выборок")
    if not m.get("history_ok"):
        p.append(f"история с пульта после перезапуска: {m.get('history_states')}")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"блок READY без пульта {m['unit_ready_s']} с, новый пульт «Готов» {m['ui_ready_s']} с, история "
        f"{len(m.get('history_states') or [])} переходов")


def judge_settings_case(c):
    """F-03, один случай: служба поднялась, модуль READY, источник настроек ожидаемый, notice с кодами CFG-*."""
    p = []
    if c.get("error"):
        return "ERROR", c["error"]
    if c.get("ready_s") is None:
        p.append(f"READY не наблюдался за {LIMITS['service_restart_s']:.0f} с")
    if c.get("source") != c.get("want"):
        p.append(f"source {c.get('source')!r}, ожидался {c.get('want')!r}")
    if c.get("want") in ("recovered", "reset") and "CFG-" not in str(c.get("notice") or ""):
        p.append(f"notice {c.get('notice')!r} без кода CFG-*")
    if c.get("want") == "reset" and not c.get("corrupt_dirs"):
        p.append("каталога remote-config.corrupt-* нет")
    if c.get("want") == "store" and c.get("revision") != c.get("revision_want"):
        p.append(f"ревизия {c.get('revision')!r}, исходная {c.get('revision_want')!r}")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"{c['want']}{' (' + str(c.get('notice')) + ')' if c.get('notice') else ''}, READY {c['ready_s']} с")


def judge_ethernet(m):
    """F-04 (ручной): пульт no-link ≤ eth_detect_s; после возврата «Готов» ≤ eth_back_s; блок работал без переходов
    (seq, PID и restarts модуля прежние)."""
    p = []
    if m.get("nolink_s") is None or m["nolink_s"] > LIMITS["eth_detect_s"]:
        p.append(f"no-link через {m.get('nolink_s')} с (предел {LIMITS['eth_detect_s']:.0f}, REQ-SYS-002)")
    if m.get("ready_s") is None or m["ready_s"] > LIMITS["eth_back_s"]:
        p.append(f"«Готов» после возврата кабеля через {m.get('ready_s')} с (предел {LIMITS['eth_back_s']:.0f})")
    b, a = m.get("unit_before") or {}, m.get("unit_after") or {}
    if not a:
        p.append("состояние модуля на блоке после возврата не прочитано")
    else:
        for k in ("seq", "pid", "state"):
            if b.get(k) != a.get(k):
                p.append(f"модуль блока {k}: {b.get(k)!r} → {a.get(k)!r}")
        if (a.get("restarts") or 0) > (b.get("restarts") or 0):        # restarts — за скользящие 60 с: может убыть
            p.append(f"модуль блока restarts: {b.get('restarts')!r} → {a.get('restarts')!r}")
    return ("FAIL" if p else "PASS"), "; ".join(p) or (
        f"no-link {m['nolink_s']} с, «Готов» {m['ready_s']} с после возврата, блок без переходов (seq {a.get('seq')})")


def judge_output(values):
    """OUT: output = "none" во всех снимках status.module, полученных через пульт."""
    bad = [v for v in values if v != "none"]
    if not values:
        return "ERROR", "нет ни одного снимка status.module"
    return ("FAIL", f"output ≠ none: {sorted(set(map(str, bad)))} в {len(bad)} из {len(values)}") if bad else \
        ("PASS", f"output = none во всех {len(values)} снимках")


def journal_usable(text):
    """journalctl от sheregesh-ui вернул записи службы (а не «No journal files»/«not seeing messages»)."""
    low = (text or "").lower()
    return bool(low.strip()) and "no journal files" not in low and "not seeing messages" not in low and \
        "no entries" not in low


# ---------------------------------------------------------------- стенд


def ssh(host, cmd, timeout=60):
    return E.ssh(host, cmd, timeout)


PROC_CMD = (f"systemctl show -p MainPID -p NRestarts -p ActiveState {REMOTE_UNIT} | tr '\\n' ' '; echo; "
            "ps -e -o pid=,ppid=,args=")


def procs(a):
    return parse_procs(E.out(a.remote, PROC_CMD))


def unit_show(host, unit):
    return parse_procs(E.out(host, f"systemctl show -p MainPID -p NRestarts -p ActiveState {unit} | tr '\\n' ' '"))


def safety_start(host, unit, after_s):
    """Страховка на самой плате (как e2e_run stop-start): start службы через after_s, даже если Mac или SSH пропадут.
    start активной службы ничего не делает. Пауза по лимиту запусков — до страховки: за долгую паузу она сработала
    бы на ещё работающей службе и после stop не осталась бы."""
    start_guard(host, unit, record=False)
    ssh(host, f"nohup sh -c 'sleep {after_s:.0f}; systemctl --no-ask-password start {unit}' >/dev/null 2>&1 &")


# Юниты блока и пульта: StartLimitBurst=5 за StartLimitIntervalSec=300 (tools/image/units). Шестой запуск в окне
# systemd отвергает (start-limit-hit, служба остаётся failed до конца окна) — так прогон 2026-10-26 потерял блок на F-03.
# Скрипт сам выдерживает паузу: не больше START_BUDGET запусков в окне, один запас — на запуск вне скрипта
# (страховочный start). Начальная история — запуски из журнала юнита на плате (предыдущий прогон, DEV deploy).
# Пауза берётся и перед stop: иначе служба простаивает остановленной всю паузу и её поднимает страховочный start.
START_WINDOW_S, START_BUDGET = 300.0, 4
_starts = {}


def board_starts(host, unit):
    """Возраст (с) запусков юнита за окно по журналу платы; [] — журнал недоступен."""
    rc, out = ssh(host, f"date +%s.%N; journalctl -u {unit} --since=-{START_WINDOW_S:.0f}s -o short-unix "
                        f"--no-pager 2>/dev/null | grep -F 'systemd[1]: Starting {unit}'", 20)
    lines = out.split()
    try:
        board_now = float(lines[0])
        return [board_now - float(w) for w in lines[1:] if w.replace(".", "", 1).isdigit()]
    except (IndexError, ValueError):
        return []


def start_guard(host, unit, record=True, now=time.monotonic, sleep=time.sleep, seed=board_starts):
    """Перед start/restart (record) и stop: если в окне уже START_BUDGET запусков — ждать, пока старейший выйдет."""
    key = (host, unit)
    if key not in _starts:
        t = now()
        _starts[key] = sorted(t - age for age in seed(host, unit))
    hist = _starts[key]
    t = now()
    hist[:] = [x for x in hist if t - x < START_WINDOW_S]
    if len(hist) >= START_BUDGET:
        wait = START_WINDOW_S - (t - hist[0]) + 2.0
        log(f"  пауза {wait:.0f} с: лимит запусков {unit} ({START_BUDGET} за {START_WINDOW_S:.0f} с)")
        sleep(wait)
        t = now()
        hist[:] = [x for x in hist if t - x < START_WINDOW_S]
    if record:
        hist.append(t)


def systemctl(host, verb, unit, timeout=90):
    if verb in ("start", "restart", "stop"):
        start_guard(host, unit, record=verb != "stop")
    return ssh(host, f"systemctl --no-ask-password {verb} {unit} 2>&1", timeout)


class Stand(S4.Stand):
    """Выборки ui-state pi1 и link-status pi2 (s4_accept.Stand), команды модуля через работающий пульт."""

    def __init__(self, a):
        super().__init__(a)
        self.calls, self.outputs, self.rebooted = [], [], False

    def ctl(self, args, timeout=60):
        """modulectl на pi1 через локальный канал пульта → (rc, ответ JSON, текст)."""
        t0 = time.monotonic()
        code, text = ssh(self.a.console, f"{REL}; PYTHONPATH=$C/software python3 -m sheregesh.link.modulectl {args} "
                                         f"--via console 2>&1", timeout)
        d = parse_cli(text)
        tel = telemetry(d)
        if tel:
            self.outputs.append(tel.get("output"))
        self.calls.append({"args": args, "rc": code, "result": d.get("result"), "detail": d.get("detail"),
                           "dur_s": round(time.monotonic() - t0, 2)})
        log(f"  modulectl {args}: rc {code} {d.get('result')!r} {str(d.get('detail') or '')[:80]}")
        return code, d, text

    def lab(self, scenario):
        code, d, _ = self.ctl(f"lab {scenario} --wait 15")
        return d.get("result") if d else f"rc {code}"

    def status(self):
        return self.ctl("status")[1]

    def wait_doc(self, sampler, pred, t0, timeout):
        end = time.monotonic() + timeout
        while True:
            dt, doc = first_row(sampler.since(t0), t0, pred)
            if dt is not None or time.monotonic() >= end:
                return dt, doc
            time.sleep(0.2)

    def unit(self):
        """Последняя выборка module из link-status pi2."""
        r = self.rs.last()
        return rs_mod(r[2]) if r else {}

    def board_epoch(self, sampler):
        r = sampler.last()
        return r[1] if r else 0.0


# ---------------------------------------------------------------- шаги


def boards(a):
    return {"pi1": board_state(E.out(a.console, BOARD_CMD)), "pi2": board_state(E.out(a.remote, BOARD_CMD))}


def preflight(a):
    """Предпроверка без действий (её же берёт s7_sticks_accept): серийные номера плат, OTA обеих в покое и не пробная
    загрузка; заодно снимок JR-10 «до» → (ids, before, problems)."""
    ids = {"console": E.board_identity(a.console), "remote": E.board_identity(a.remote)}
    problems = [f"{r}: серийный номер {ids[r]['serial']!r} ≠ {E.SERIALS[r]!r}" for r in ids
                if ids[r]["serial"] != E.SERIALS[r]]
    before = boards(a)
    for b, st in before.items():
        o = st.get("ota")
        if o is None or o.get("state") not in E.OTA_OK or o.get("tryboot_now") is True:
            problems.append(f"{b}: OTA {o!r} не в покое (или пробная загрузка) — без действий")
    return ids, before, problems


def step_emulator(a, stand):
    snap, health = procs(a), stand.unit()
    v, d = judge_emulator(snap, health)
    return v, d, {"health": health, "mainpid": snap["mainpid"], "emulators": emulator_pids(snap)}


def step_profile(a, stand):
    cli = stand.status()
    r = stand.ui.last()
    v, d = judge_profile(cli, r[2] if r else {})
    return v, d, {"module": (cli.get("module") or {}) if cli else None, "ui": ui_mod(r[2] if r else {})}


def step_init(a, stand):
    """JR-03 и JR-04: одна команда reinit."""
    seq0 = telemetry(stand.status()).get("seq") or 0
    t0 = time.monotonic()
    code, d, _ = stand.ctl(f"reinit --wait 15 --expect READY --expect-timeout {LIMITS['ready_s']:.0f}", 90)
    m = {"rc": code, "result": d.get("result"), "detail": d.get("detail"), "seq0": seq0}
    m["ready_s"] = stand.wait(stand.rs, rs_state_is("READY", seq_gt=seq0), t0, LIMITS["ready_s"])
    t = telemetry(stand.status())
    m["history"] = history_after(t.get("history"), seq0)
    m["init_ms"] = (t.get("diag") or {}).get("init_ms")
    g = {"rs_ready_s": m["ready_s"], "timeout_s": LIMITS["ready_s"] + LIMITS["gui_lag_s"]}
    g["ui_ready_s"] = stand.wait(stand.ui, lambda doc: ui_state_is("READY", STATE_TEXT["READY"])(doc) and
                                 (ui_mod(doc).get("seq") or 0) > seq0, t0, g["timeout_s"])
    return judge_init(m), m, judge_gui_ready(g), g


def step_detach(a, stand):
    m = {}
    t0 = time.monotonic()
    m["detach_result"] = stand.lab("detach")
    m["absent_s"] = stand.wait(stand.ui, ui_state_is("DISCONNECTED", STATE_TEXT["DISCONNECTED"]), t0,
                               LIMITS["detect_s"] + 2)
    t1 = time.monotonic()
    m["attach_result"] = stand.lab("attach")
    m["ready_s"] = stand.wait(stand.ui, ui_state_is("READY", STATE_TEXT["READY"]), t1, LIMITS["ready_s"] + 2)
    return judge_detach(m), m


def step_fault(a, stand, scenario):
    """JR-06 (iface-error | hang) и JR-07 (heal) → ((v6, d6), m6, (v7, d7), m7)."""
    seq0 = telemetry(stand.status()).get("seq") or 0
    m6 = {"scenario": scenario, "seq0": seq0}
    t0 = time.monotonic()
    m6["result"] = stand.lab(scenario)
    if scenario == "iface-error":
        dt, doc = stand.wait_doc(stand.ui, lambda d: ui_mod(d).get("state") == "FAULT" and
                                 CODE_TEXT["MOD-IFACE-ERROR"] in str(ui_mod(d).get("errorText") or ""),
                                 t0, LIMITS["fault_s"] + 2)
        m6["fault_s"], m6["state_text"] = dt, ui_mod(doc).get("stateText") if doc else None
        m6["error_text"] = ui_mod(doc).get("errorText") if doc else None
        m6["fault_event_s"] = stand.wait(stand.ui, lambda d: ui_mod(d).get("faultEvent") is True, t0,
                                         LIMITS["fault_s"] + 2)
        v6 = judge_fault_shown(m6, "iface")
        codes = {"MOD-IFACE-ERROR"}
    else:
        dt, doc = stand.wait_doc(stand.rs, lambda d: rs_mod(d).get("state") == "FAULT" and
                                 (rs_mod(d).get("seq") or 0) > seq0, t0, LIMITS["fault_s"] + 2)
        m6["fault_s"], m6["rs_code"] = dt, rs_mod(doc).get("error_code") if doc else None
        m6["ui_fault_s"] = stand.wait(stand.ui, lambda d: ui_mod(d).get("state") == "FAULT" and
                                      str(ui_mod(d).get("stateText") or "").startswith(STATE_TEXT["FAULT"]),
                                      t0, LIMITS["fault_s"] + LIMITS["gui_lag_s"] + 1)
        hist = history_after(telemetry(stand.status()).get("history"), seq0)
        m6["first_code"] = first_fault_code(hist) or m6["rs_code"]
        m6["history"] = hist
        v6 = judge_fault_shown(m6, "hang")
        codes = {"MOD-NO-RESPONSE", "MOD-INIT-TIMEOUT"}
    seq1 = telemetry(stand.status()).get("seq") or 0
    m7 = {"seq_heal": seq1}
    t1 = time.monotonic()
    m7["result"] = stand.lab("heal")
    m7["ready_s"] = stand.wait(stand.rs, rs_state_is("READY", seq_gt=seq0), t1, LIMITS["recover_s"])
    m7["ui_ready_s"] = stand.wait(stand.ui, lambda d: ui_state_is("READY", STATE_TEXT["READY"])(d) and
                                  ui_mod(d).get("faultEvent") is False, t1, LIMITS["recover_s"] + LIMITS["gui_lag_s"])
    r = stand.ui.last()
    m7["error_text"] = ui_mod(r[2] if r else {}).get("errorText")
    t = telemetry(stand.status())
    m7["history"], m7["error"] = history_after(t.get("history"), seq0), t.get("error")
    return v6, m6, judge_recovered(m7, codes), m7


def step_crash(a, stand, how):
    """JR-08 (a) lab crash | (b) SIGKILL процесса эмулятора на pi2."""
    u0 = stand.unit()
    m = {"how": how, "pid0": u0.get("pid"), "restarts0": u0.get("restarts"), "seq0": u0.get("seq")}
    t0 = time.monotonic()
    if how == "crash":
        m["result"] = stand.lab("crash")
    else:
        code, text = ssh(a.remote, f"pkill -KILL -u sheregesh-ui -f '[s]heregesh_emulators[.]jrmodule'; echo rc=$?")
        m["result"] = text.strip()
        if "rc=0" not in text:
            return judge_crash({**m, "error": f"pkill не нашёл процесс эмулятора: {text!r}"}), m
    dt, doc = stand.wait_doc(stand.rs, lambda d: rs_mod(d).get("state") == "FAULT" and
                             (rs_mod(d).get("seq") or 0) > (m["seq0"] or 0), t0, LIMITS["fault_s"])
    m["fault_s"], m["fault_code"] = dt, rs_mod(doc).get("error_code") if doc else None
    dt, doc = stand.wait_doc(stand.rs, rs_state_is("READY", seq_gt=m["seq0"] or 0), t0, LIMITS["crash_recover_s"])
    m["ready_s"] = dt
    u1 = rs_mod(doc) if doc else stand.unit()
    m["pid1"], m["restarts1"] = u1.get("pid"), u1.get("restarts")
    m["emulators"] = len(emulator_pids(procs(a)))
    if m["fault_code"] is None:                    # FAULT короче периода выборки — код из истории
        m["fault_code"] = first_fault_code(history_after(telemetry(stand.status()).get("history"), m["seq0"] or 0))
    return judge_crash(m), m


def journal_count(a, since, pattern):
    """(журнал доступен, число строк pattern с since) — строки агента «[remote] module: …»."""
    code, probe = ssh(a.remote, f"journalctl -u {REMOTE_UNIT} -n 3 -o cat --no-pager 2>&1")
    if code != 0 or not journal_usable(probe):
        return False, None
    n = E.out(a.remote, f"journalctl -u {REMOTE_UNIT} --since @{int(since)} -o cat --no-pager 2>/dev/null | "
                        f"grep -c '{pattern}' || true")
    return True, int(n) if n.strip().isdigit() else None


def step_unavailable(a, stand, hold):
    u0 = stand.unit()
    m = {"hold_s": hold, "seq0": u0.get("seq")}
    t0 = time.monotonic()
    m["result"] = stand.lab("emu-unavailable")
    dt, doc = stand.wait_doc(stand.rs, rs_state_is("FAULT", code="EMU-UNAVAILABLE"), t0, LIMITS["unavailable_s"])
    m["unavail_s"] = dt
    if dt is not None:
        r0 = rs_mod(doc).get("restarts")
        epoch = stand.board_epoch(stand.rs)
        th = time.monotonic()
        during = []
        while time.monotonic() - th < hold:
            during += emulator_pids(procs(a))
            time.sleep(min(5.0, max(0.0, hold - (time.monotonic() - th))))
        rows = [rs_mod(d) for _m, _b, d in stand.rs.since(th)]
        m["procs_during"] = sorted(set(during))
        m["restarts_hold"] = sorted({r.get("restarts") for r in rows if r})
        m["restarts_grew"] = any((x or 0) > (r0 or 0) for x in m["restarts_hold"])
        m["running_seen"] = any(r.get("backend_state") == "running" for r in rows)
        m["journal_ok"], m["journal_started"] = journal_count(a, epoch, "module: emulator started")
    t1 = time.monotonic()
    m["available_result"] = stand.lab("emu-available")
    m["reset_result"] = stand.ctl("reset --wait 15")[1].get("result")
    m["ready_s"] = stand.wait(stand.rs, rs_state_is("READY"), t1, LIMITS["ready_s"] + 5)
    return judge_unavailable(m), m


def step_service_restart(a, stand):
    s0, c0 = procs(a), unit_show(a.console, CONSOLE_UNIT)
    emu0 = emulator_pids(s0)
    m = {"mainpid0": s0["mainpid"], "emu_pid0": emu0[0] if emu0 else None, "nrestarts_remote0": s0["nrestarts"],
         "nrestarts_console0": c0["nrestarts"]}
    t0 = time.monotonic()
    m["rc"], out = systemctl(a.remote, "restart", REMOTE_UNIT)
    m["out"] = out[-200:]
    m["nolink_s"], m["ready_s"] = wait_relink(stand, t0, ui_state_is("READY"), LIMITS["service_restart_s"])
    time.sleep(1)
    s1, c1 = procs(a), unit_show(a.console, CONSOLE_UNIT)
    emus = emulator_pids(s1)
    m.update(mainpid1=s1["mainpid"], emulators=len(emus), emu_pids=emus,
             parent_ok=bool(emus) and all(s1["mainpid"] in ancestors(e, s1) for e in emus),
             old_alive=m["emu_pid0"] in {p["pid"] for p in s1["procs"]},
             nrestarts_remote1=s1["nrestarts"], nrestarts_console1=c1["nrestarts"])
    return judge_service_restart(m), m


def wait_relink(stand, t0, pred, timeout):
    """После перезапуска блока: сначала «нет связи» на пульте (выборки прежнего сеанса не в счёт — команда в них
    получила бы no-link), затем pred в новом сеансе. → (no-link, pred) в секундах от t0; None — не наблюдалось."""
    nl = stand.wait(stand.ui, lambda d: ui_mod(d).get("support") == "no-link", t0, timeout)
    if nl is None:
        return None, None
    dt = stand.wait(stand.ui, pred, t0 + nl, timeout)
    return nl, (None if dt is None else round(nl + dt, 2))


def disabled_check(a, stand, event, t0, timeout, after_epoch=None):
    ev = {"event": event, "timeout_s": timeout}
    ev["disabled_s"] = stand.wait(stand.rs, rs_state_is("DISABLED", after_epoch=after_epoch), t0, timeout)
    ev["nolink_s"], ev["ui_disabled_s"] = wait_relink(stand, t0, ui_state_is("DISABLED"), timeout)
    time.sleep(1)
    t = telemetry(stand.status())
    st = t.get("settings") or {}
    ev.update(state=t.get("state"), revision=st.get("revision"), source=st.get("source"),
              emulators=emulator_pids(procs(a)))
    return ev


def step_settings(a, stand):
    m = {"events": []}
    code, d, _ = stand.ctl("configure enabled=false --wait 30 --expect DISABLED --expect-timeout 15", 90)
    m["off_result"], m["off_detail"] = d.get("result"), d.get("detail")
    if m["off_result"] != "ok":
        return judge_settings(m), m
    m["revision"] = (telemetry(stand.status()).get("settings") or {}).get("revision")
    epoch = stand.board_epoch(stand.rs)
    t0 = time.monotonic()
    rc, out = systemctl(a.remote, "restart", REMOTE_UNIT)
    if rc != 0:
        m["events"].append({"event": "restart", "error": f"rc {rc} {out[-200:]}"})
    else:
        m["events"].append(disabled_check(a, stand, f"systemctl restart {REMOTE_UNIT}", t0,
                                          LIMITS["service_restart_s"], epoch))
    if a.reboot:
        m["events"].append(reboot_event(a, stand))
    t1 = time.monotonic()
    code, d, _ = stand.ctl("configure enabled=true --wait 30", 90)
    m["on_result"] = d.get("result")
    m["ready_s"] = stand.wait(stand.rs, rs_state_is("READY"), t1, LIMITS["ready_s"] + 5)
    return judge_settings(m), m


def reboot_event(a, stand):
    """JR-09 через перезагрузку pi2 — только COMMITTED и tryboot_now=false (иначе BLOCKED, ничего не делается)."""
    o = P2.ota_summary(E.jload(E.out(a.remote, "sheregesh-ota-client status")))
    ok, why = reboot_allowed(o)
    ev = {"event": "systemctl reboot pi2", "ota": o}
    if not ok:
        return {**ev, "verdict": BLOCKED, "error": f"перезагрузка запрещена: {why}"}
    boot0 = E.out(a.remote, "cat /proc/sys/kernel/random/boot_id")
    stand.rs.stop()
    t0 = time.monotonic()
    code, text = ssh(a.remote, "systemctl --no-ask-password reboot 2>&1", 30)
    if code not in (0, 255, 124) or "denied" in text.lower() or "authentication" in text.lower():
        stand.rs = S4.RemoteSampler(a.remote).start()
        return {**ev, "verdict": "ERROR", "error": f"reboot не принят: rc {code} {text[-200:]}"}
    w = P2.wait_reboot(a.remote, boot0, LIMITS["reboot_s"])
    ev["reboot"] = w
    if w["up_s"] is None:
        return {**ev, "verdict": "ERROR", "error": f"SSH pi2 не вернулся за {LIMITS['reboot_s']:.0f} с (адрес "
                "DHCP сменился? collect_boot.py find --mac d8:3a:dd:69:0f:6e)"}
    stand.rs = S4.RemoteSampler(a.remote).start()
    stand.rebooted = True
    return disabled_check(a, stand, "systemctl reboot pi2", t0, w["up_s"] + LIMITS["service_restart_s"] + 30) | \
        {"reboot": w, "ota": o}


def step_gap(a, stand, gap):
    """F-01: «пульт раньше блока» — блок остановлен gap с, затем start."""
    m = {"gap_s": gap}
    safety_start(a.remote, REMOTE_UNIT, gap + 60)
    t0 = time.monotonic()
    m["stop_rc"], _ = systemctl(a.remote, "stop", REMOTE_UNIT)
    m["nolink_s"] = stand.wait(stand.ui, lambda d: ui_mod(d).get("support") == "no-link", t0, LIMITS["detect_s"] + 2)
    time.sleep(max(0.0, gap - (time.monotonic() - t0)))
    t_nl = t0 + (m["nolink_s"] or 0)
    gap_docs = [d for mono, _b, d in stand.ui.since(t_nl)]
    m["gap_docs"] = len(gap_docs)
    m["ready_during_gap"] = sum(1 for d in gap_docs if ui_mod(d).get("state") == "READY" or
                                str(ui_mod(d).get("stateText")) == STATE_TEXT["READY"])
    epoch = stand.board_epoch(stand.ui)
    t1 = time.monotonic()
    m["start_rc"], _ = systemctl(a.remote, "start", REMOTE_UNIT)
    m["ready_s"] = stand.wait(stand.ui, lambda d: ui_state_is("READY")(d) and (d.get("time") or 0) > epoch, t1,
                              LIMITS["service_restart_s"])
    return judge_no_link_gap(m), m


def step_unit_first(a, stand):
    """F-02: пульт остановлен; блок перезапущен и сам доходит до READY; пульт запущен — состояние из телеметрии."""
    m = {}
    safety_start(a.console, CONSOLE_UNIT, 120)
    m["console_stop_rc"], _ = systemctl(a.console, "stop", CONSOLE_UNIT)
    repoch = stand.board_epoch(stand.rs)
    t0 = time.monotonic()
    m["remote_restart_rc"], _ = systemctl(a.remote, "restart", REMOTE_UNIT)
    m["unit_ready_s"] = stand.wait(stand.rs, rs_state_is("READY", after_epoch=repoch), t0, LIMITS["ready_s"] + 5)
    time.sleep(2)
    epoch = stand.board_epoch(stand.ui)
    t1 = time.monotonic()
    m["console_start_rc"], _ = systemctl(a.console, "start", CONSOLE_UNIT)
    m["ui_ready_s"] = stand.wait(stand.ui, lambda d: ui_state_is("READY")(d) and (d.get("time") or 0) > epoch, t1,
                                 LIMITS["console_start_s"])
    new = [d for _mono, _b, d in stand.ui.since(t1) if (d.get("time") or 0) > epoch]
    m["first_support"] = ui_mod(new[0]).get("support") if new else None
    m["ready_without_link"] = sum(1 for d in new if ui_mod(d).get("state") == "READY" and
                                  ui_mod(d).get("support") != "ok")
    hist = telemetry(stand.status()).get("history") or []
    m["history_states"] = [h.get("state") for h in hist if isinstance(h, dict)]
    m["history_ok"] = "READY" in m["history_states"]
    return judge_unit_first(m), m


def restart_and_read(a, stand, want, prep, revision_want=None):
    """Остановить блок, prep (команда на pi2), запустить, дождаться READY и прочитать settings."""
    c = {"want": want, "revision_want": revision_want}
    safety_start(a.remote, REMOTE_UNIT, 90)
    rc, out = systemctl(a.remote, "stop", REMOTE_UNIT)
    if rc != 0:
        return {**c, "error": f"stop: rc {rc} {out[-200:]}"}
    stand.wait(stand.ui, lambda d: ui_mod(d).get("support") == "no-link", time.monotonic(), 10)
    t_stop = time.monotonic()                    # дальше — только выборки после «нет связи»: новый сеанс
    rc, out = ssh(a.remote, prep + " 2>&1; echo rc=$?")
    c["prep"] = out[-300:]
    if "rc=0" not in out:
        systemctl(a.remote, "start", REMOTE_UNIT)
        return {**c, "error": f"подготовка каталога не удалась: {out[-200:]}"}
    epoch = stand.board_epoch(stand.rs)
    t0 = time.monotonic()
    rc, out = systemctl(a.remote, "start", REMOTE_UNIT)
    if rc != 0:
        return {**c, "error": f"start: rc {rc} {out[-200:]}"}
    c["ready_s"] = stand.wait(stand.rs, rs_state_is("READY", after_epoch=epoch), t0, LIMITS["service_restart_s"])
    c["ui_ready_s"] = stand.wait(stand.ui, ui_state_is("READY"), t_stop, LIMITS["service_restart_s"])
    st = telemetry(stand.status()).get("settings") or {}
    c.update(source=st.get("source"), notice=st.get("notice"), revision=st.get("revision"))
    c["corrupt_dirs"] = E.out(a.remote, f"ls -d {CFG_PARENT}/remote-config.corrupt-* 2>/dev/null").split()
    return c


def step_settings_faults(a, stand, stash):
    """F-03: отсутствует → factory; state.json повреждён → recovered; все ревизии повреждены → reset. Каталоги
    переносятся в stash (никаких удалений), исходный каталог возвращается."""
    orig = telemetry(stand.status()).get("settings") or {}
    m = {"stash": stash, "orig": orig, "cases": []}
    had = E.out(a.remote, f"test -d {RCDIR} && echo yes || echo no").strip() == "yes"
    m["had_dir"] = had
    mk = f"mkdir -p {stash} && chmod 700 {stash}"
    steps = [("missing", "factory", f"{mk} && " + (f"mv {RCDIR} {stash}/orig" if had else "true")),
             ("state-corrupt", "recovered", f"printf '{{broken' > {RCDIR}/state.json"),
             ("all-revisions-corrupt", "reset",
              f"for f in {RCDIR}/revisions/*/*.yaml; do printf '\\n#s6-corrupt\\n' >> \"$f\"; done; "
              f"test -n \"$(ls {RCDIR}/revisions)\"")]
    try:
        for name, want, prep in steps:
            c = restart_and_read(a, stand, want, prep)
            c["case"] = name
            m["cases"].append(c)
            log(f"  F-03 {name}: {judge_settings_case(c)}")
            if c.get("error"):
                break
    finally:
        m["restore"] = restore_settings_dir(a, stand, stash, had, orig.get("revision"))
    vs, ds = [], []
    for c in m["cases"] + [m["restore"]]:
        v, d = judge_settings_case(c)
        vs.append(v)
        ds.append(f"{c.get('case')}: {d}")
    return (agg(vs), "; ".join(ds)), m


def restore_settings_dir(a, stand, stash, had, revision):
    """Исходный каталог настроек на место: текущий и remote-config.corrupt-* — в stash."""
    prep = (f"mkdir -p {stash}/after && (test ! -e {RCDIR} || mv {RCDIR} {stash}/after/) && "
            f"for d in {CFG_PARENT}/remote-config.corrupt-*; do test -e \"$d\" && mv \"$d\" {stash}/after/; done; "
            + (f"mv {stash}/orig {RCDIR}" if had else "true"))
    c = restart_and_read(a, stand, "store" if had else "factory", prep, revision if had else None)
    c["case"] = "restore"
    return c


def step_ethernet(a, stand, prompt):
    """F-04: человек вынимает и возвращает кабель Ethernet pi2 (Mac и pi1 остаются на связи)."""
    m = {"unit_before": {k: stand.unit().get(k) for k in ("seq", "pid", "restarts", "state")}}
    prompt("F-04: ВЫНЬТЕ кабель Ethernet pi2 (со стороны pi2 или свитча; pi1 и Mac не трогать) и нажмите Enter "
           "в тот же момент… ")
    t0 = time.monotonic()
    m["nolink_s"] = stand.wait(stand.ui, lambda d: ui_mod(d).get("support") == "no-link", t0,
                               LIMITS["eth_detect_s"] + 5)
    stand.rs.stop()
    prompt("F-04: подождите ≥ 10 с, ВЕРНИТЕ кабель pi2 и нажмите Enter в тот же момент… ")
    t1 = time.monotonic()
    m["ready_s"] = stand.wait(stand.ui, lambda d: ui_state_is("READY")(d), t1, LIMITS["eth_back_s"] + 15)
    stand.rs = S4.RemoteSampler(a.remote).start()
    stand.wait(stand.rs, lambda d: bool(rs_mod(d)), time.monotonic(), 20)
    m["unit_after"] = {k: stand.unit().get(k) for k in ("seq", "pid", "restarts", "state")}
    return judge_ethernet(m), m


def cleanup(a, stand, orig):
    """finally: лабораторные флаги сняты, бюджет сброшен, исходные настройки, службы запущены, READY."""
    out = {}
    for unit, host in ((REMOTE_UNIT, a.remote), (CONSOLE_UNIT, a.console)):
        out[f"start {unit}"] = systemctl(host, "start", unit)[0]
    stand.wait(stand.ui, lambda d: ui_mod(d).get("support") == "ok", time.monotonic(), 40)
    for sc in ("emu-available", "heal", "attach"):
        out[sc] = stand.lab(sc)
    out["reset"] = stand.ctl("reset --wait 15")[1].get("result")
    cur = telemetry(stand.status()).get("settings") or {}
    diff = {k: orig[k] for k in ("enabled", "auto_init", "auto_recover", "profile")
            if k in orig and orig.get(k) != cur.get(k)}
    if diff:
        kv = " ".join(f"{k}={str(v).lower() if isinstance(v, bool) else v}" for k, v in diff.items())
        out["configure"] = stand.ctl(f"configure {kv} --wait 30", 90)[1].get("result")
    want = "DISABLED" if orig.get("enabled") is False else "READY"
    out["final_s"] = stand.wait(stand.ui, ui_state_is(want), time.monotonic(), 30)
    out["final"] = ui_mod(stand.ui.last()[2] if stand.ui.last() else {}).get("state")
    return out


# ---------------------------------------------------------------- план и запуск


def plan(a):
    rb = "да, только при COMMITTED и tryboot_now=false (иначе BLOCKED)" if a.reboot else "нет (--reboot)"
    eth = "да, по подсказкам" if a.manual_ethernet else "нет — ручной шаг протокола (--manual-ethernet)"
    hold = f"{(a.hold + 25) / 60:.1f}".replace(".", ",")
    rows = [
        ("предпроверка", "серийные номера; OTA обеих плат в покое, не tryboot; пульт отвечает modulectl status", ""),
        ("JR-10 до", "sheregesh-ota-client status, DEV-выпуск, образ, boot_id, failed — pi1 и pi2", "0,2"),
        ("JR-01", "backend running, один процесс jrmodule, предок — MainPID sheregesh-remote-net", "0,1"),
        ("JR-02", "modulectl status --via console: radiomaster-nomad «RadioMaster Nomad», плашка", "0,1"),
        ("JR-03/04", "reinit --expect READY: INITIALIZING → READY (init-ok), init_ms ≤ 5000; «Готов» ≤ 2 с", "0,3"),
        ("JR-05", "lab detach → «Модуль отсутствует» ≤ 5 с; lab attach → READY ≤ 15 с", "0,3"),
        ("JR-06/07", "lab iface-error → «Ошибка интерфейса», MODULE-FAULT ≤ 6 с; heal → READY ≤ 20 с", "0,5"),
        ("JR-06/07", "lab hang → первый FAULT MOD-NO-RESPONSE ≤ 6 с; heal → READY ≤ 20 с", "0,5"),
        ("JR-08a", "lab crash → EMU-EXITED → READY ≤ 15 с, новый PID, restarts +1", "0,3"),
        ("JR-08b", "pkill -KILL эмулятора на pi2 → то же", "0,3"),
        ("JR-08c", f"lab emu-unavailable → EMU-UNAVAILABLE ≤ 30 с, {a.hold:.0f} с без процессов и попыток, "
                   "emu-available + reset → READY", hold),
        ("JR-08d", "systemctl restart sheregesh-remote-net → no-link → READY ≤ 30 с, новый MainPID", "0,5"),
        ("JR-09", f"configure enabled=false → restart → DISABLED, ревизия та же (store) → enabled=true → READY; "
                  f"перезагрузка pi2: {rb}", "0,7"),
        ("F-01", f"stop блока на {a.gap:.0f} с: no-link ≤ 5 с, «Готов» по старым данным нет → start → READY", "0,5"),
        ("F-02", "stop пульта, restart блока (READY без пульта ≤ 15 с), start пульта → READY", "1"),
        ("F-03", "настройки: нет каталога → factory; state.json испорчен → recovered; ревизии → reset; "
                 "исходный каталог обратно (остатки — ~/.config/sheregesh/s6-jr-<время>/, без удалений)", "1,5"),
        ("F-04", f"Ethernet pi2 вручную: {eth}", ""),
        ("OUT", "output = none во всех ответах modulectl", ""),
        ("JR-10 после", "то же, что «до», должно совпасть (boot_id pi2 — кроме перезагрузки JR-09)", "0,2"),
    ]
    out = [f"S6 JR — сокращённая приёмка внешнего модуля (ADR-0012 §11), pi1 {a.console} / pi2 {a.remote}, "
           "≈ 7–9 мин:"]
    out += [f"  {k:<12} {t}" + (f"  ~{m} мин" if m else "") for k, t, m in rows]
    out += ["  finally: emu-available, heal, attach, reset, исходные настройки, службы запущены, READY.",
            "  Не трогает: OTA, слоты, видео, образ, юниты, polkit; без sudo; интерфейсы плат не отключаются."]
    return "\n".join(out)


def main(argv=None, prompt=None):
    global LOG
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console", default="192.168.1.92")
    ap.add_argument("--remote", default="192.168.1.171")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--only", help="через запятую: JR-01…JR-10, F-01…F-04, OUT (JR-10 «до/после» выполняется всегда)")
    ap.add_argument("--hold", type=float, default=LIMITS["hold_s"], help="JR-08c: удержание EMU-UNAVAILABLE, с")
    ap.add_argument("--gap", type=float, default=8.0, help="F-01: сколько блок остановлен, с")
    ap.add_argument("--reboot", action="store_true",
                    help="JR-09: ещё и перезагрузка pi2 (только COMMITTED, без tryboot)")
    ap.add_argument("--manual-ethernet", action="store_true", help="F-04: ручной обрыв кабеля pi2 по подсказкам")
    ap.add_argument("--plan", action="store_true")
    a = ap.parse_args(argv)
    if a.plan:
        print(plan(a))
        return 0
    if not a.out:
        ap.error("--out обязателен")
    only = set(STEPS) if not a.only else set(a.only.split(",")) & set(STEPS)
    if a.only and not only:
        ap.error(f"--only: нет известных шагов ({', '.join(STEPS)})")
    prompt = prompt or E.fresh_input
    a.out.mkdir(parents=True, exist_ok=True)
    LOG = a.out / "run.log"
    ids, before, problems = preflight(a)
    meta = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "boards": ids, "limits": LIMITS,
            "args": {k: str(v) for k, v in vars(a).items()}, "before": before}
    stand = Stand(a)
    if not problems:
        code, d, text = stand.ctl("status")
        if code == 2 or not d:
            problems.append(f"pi1: пульт не ответил modulectl status --via console (rc {code}): {text[-200:]!r}")
        meta["module_before"] = d.get("module") if d else None
    if problems:
        (a.out / "summary.json").write_text(json.dumps({**meta, "preflight": problems, "verdict": "ERROR"},
                                                       ensure_ascii=False, indent=1, default=str))
        log("ПРЕДПРОВЕРКА: " + "; ".join(problems) + " — без действий")
        return 2
    orig_settings = telemetry(d).get("settings") or {}
    meta["settings_orig"] = orig_settings
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
    blocked = None
    try:
        stand.wait(stand.rs, lambda doc: bool(rs_mod(doc)), time.monotonic(), 10)
        stand.wait(stand.ui, lambda doc: bool(ui_mod(doc)), time.monotonic(), 10)
        if "JR-01" in only:
            t0 = time.monotonic()
            v, dd, x = step_emulator(a, stand)
            put("JR-01", v, dd, x, t0)
            if not x["health"]:
                blocked = "JR-01: блок не сообщает module в link-status.json (не S6 или подсистема выключена)"
        if "JR-02" in only and not blocked:
            t0 = time.monotonic()
            v, dd, x = step_profile(a, stand)
            put("JR-02", v, dd, x, t0)
            if (x.get("module") or {}).get("support") != "ok":
                blocked = f"JR-02: support {(x.get('module') or {}).get('support')!r} — пульт и блок не на S6 (1.3)"

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

        def s34():
            r3, m3, r4, m4 = step_init(a, stand)
            return {"JR-03": (r3, m3), "JR-04": (r4, m4)}

        def s67():
            v6a, m6a, v7a, m7a = step_fault(a, stand, "iface-error")
            v6b, m6b, v7b, m7b = step_fault(a, stand, "hang")
            return {"JR-06": ((agg([v6a[0], v6b[0]]), f"iface-error: {v6a[1]}; hang: {v6b[1]}"),
                              {"iface-error": m6a, "hang": m6b}),
                    "JR-07": ((agg([v7a[0], v7b[0]]), f"после iface-error: {v7a[1]}; после hang: {v7b[1]}"),
                              {"iface-error": m7a, "hang": m7b})}

        def s08():
            parts, ms, vs = [], {}, []
            for key, fn in (("a crash", lambda: step_crash(a, stand, "crash")),
                            ("b SIGKILL", lambda: step_crash(a, stand, "kill")),
                            ("c emu-unavailable", lambda: step_unavailable(a, stand, a.hold)),
                            ("d restart службы", lambda: step_service_restart(a, stand))):
                (v, dd), m = fn()
                vs.append(v)
                parts.append(f"({key}) {v}: {dd}")
                ms[key] = m
                log(f"  JR-08 ({key}): {v} — {dd}")
            return {"JR-08": ((agg(vs), "; ".join(parts)), ms)}

        stash = f"{CFG_PARENT}/s6-jr-{time.strftime('%Y%m%d-%H%M%S')}"
        run(("JR-03", "JR-04"), s34)
        run(("JR-05",), lambda: {"JR-05": step_detach(a, stand)})
        run(("JR-06", "JR-07"), s67)
        run(("JR-08",), s08)
        run(("JR-09",), lambda: {"JR-09": step_settings(a, stand)})
        run(("F-01",), lambda: {"F-01": step_gap(a, stand, a.gap)})
        run(("F-02",), lambda: {"F-02": step_unit_first(a, stand)})
        run(("F-03",), lambda: {"F-03": step_settings_faults(a, stand, stash)})
        if "F-04" in only and not a.manual_ethernet:
            put("F-04", "SKIP", "ручной шаг: --manual-ethernet (интерфейсы плат скрипт не отключает)")
        elif a.manual_ethernet:
            run(("F-04",), lambda: {"F-04": step_ethernet(a, stand, prompt)})
    finally:
        try:
            meta["cleanup"] = cleanup(a, stand, orig_settings)
        except Exception as e:                                       # noqa: BLE001 — итог пишется в любом случае
            meta["cleanup"] = {"error": repr(e)}
        log(f"восстановление: {meta['cleanup']}")
        stand.ui.stop()
        stand.rs.stop()
    if "OUT" in only:
        put("OUT", *judge_output(stand.outputs))
    t0 = time.monotonic()
    after = boards(a)
    meta["after"] = after
    v, p = judge_ota(before, after, stand.rebooted)
    put("JR-10", v, "; ".join(p) or "OTA, слот, tryboot, образ, DEV-выпуск и boot_id прежние на обеих платах; "
                                    "failed пуст", {"before": before, "after": after}, t0)
    for k in STEPS:
        results.setdefault(k, {"verdict": "SKIP", "detail": "не запрошен (--only)"})
    meta["modulectl_calls"] = stand.calls
    c = meta.get("cleanup") or {}
    if c.get("error") or c.get("final_s") is None:
        meta["cleanup_warning"] = "исходное состояние модуля не подтверждено — см. cleanup"
        log("ВНИМАНИЕ: " + meta["cleanup_warning"])
    summary = {**meta, "duration_min": round((time.monotonic() - t_run) / 60, 1), "durations_s": durations,
               "results": {k: results[k] for k in STEPS}, "verdict": verdict_of(results)}
    (a.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    log(f"ИТОГ S6 JR {summary['verdict']} за {summary['duration_min']} мин: " +
        ", ".join(f"{k} {results[k]['verdict']}" for k in STEPS))
    return {"PASS": 0, "FAIL": 1, "ERROR": 2, BLOCKED: 3, "SKIP": 2}[summary["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
