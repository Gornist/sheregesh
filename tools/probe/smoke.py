#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Smoke Test выпуска на стенде (REQ-REL-003/004/005): протоколы TEST-SMOKE-P (пульт), TEST-SMOKE-R (удалённый блок)
и TEST-SMOKE-S (совместная работа) по пунктам реестра. С машины владельца по SSH.

  python3 tools/probe/smoke.py --console 192.168.1.94 --remote 192.168.1.173 --out DIR [--suite P,R,S]
                               [--interactive | --manual FILE.json] [--exchange-s 300]

Пункты, которые проверяются с машины владельца, — автоматически; пункты рук и глаз оператора (касание, физические
органы, USB, кабель, кнопка питания, индикатор) — вопросом оператору (--interactive) или ответами из файла (--manual:
{"P2": "PASS", "P3": "FAIL: Esc не закрывает диалог", ...}); без ответа — MANUAL, и протокол не PASS. Холодной
перезагрузки и выключения проба не делает: эти пункты — оператору. Итог набора — PASS, только если каждый пункт PASS.
В DIR: smoke.json, PROTOCOL.md.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/probe"))
import check_video_link as cvl  # noqa: E402

UI, C_STATUS = cvl.UI, cvl.C_STATUS
MANDATORY = {"console": ("console", "video-rx"), "remote": ("remote-net", "video-tx")}
TEMP_TOLERANCE_C = 5.0
MIN_FPS = 25.0

# (пункт, набор, текст реестра, кто проверяет)
ITEMS = [
    ("P1", "P", "Холодная загрузка ОС; GUI появляется", "auto+manual"),
    ("P2", "P", "Сенсорный ввод: открыть 3 экрана касанием", "manual"),
    ("P3", "P", "Физический ввод: те же 3 экрана только физическими органами", "manual"),
    ("P4", "P", "Диагностика: все обязательные службы active", "auto"),
    ("P5", "P", "Настройки: изменить, применить, подтвердить, откатить тестовый параметр", "manual"),
    ("P6", "P", "Журналы: экспорт на USB", "manual"),
    ("P7", "P", "Штатное выключение кнопкой питания с подтверждением", "manual"),
    ("R1", "R", "Подача питания; загрузка ОС; индикатор «загрузка» → «норма»", "auto+manual"),
    ("R2", "R", "Службы: состояние всех обязательных служб видно на пульте и равно active", "auto"),
    ("R3", "R", "Настройки: изменить, применить и подтвердить тестовый параметр через пульт", "manual"),
    ("R4", "R", "Диагностика: паспорт и температура видны на пульте", "auto"),
    ("R5", "R", "Журналы: экспорт журналов удалённого блока на USB пульта", "manual"),
    ("R6", "R", "Штатное выключение командой с пульта; индикатор гаснет; журнал завершён", "manual"),
    ("S1", "S", "Рукопожатие, проверка совместимости версий", "auto"),
    ("S2", "S", "Обмен тестовыми данными без потерь", "auto"),
    ("S3", "S", f"Видео с удалённого блока на пульте ≥ {MIN_FPS:.0f} к/с", "auto"),
    ("S4", "S", "Отключение и подключение кабеля — восстановление ≤ 10 с", "manual"),
    ("S5", "S", "Инжекция одной неисправности удалённого блока — сообщение на пульте", "auto"),
]
TESTS = {"P": "TEST-SMOKE-P", "R": "TEST-SMOKE-R", "S": "TEST-SMOKE-S"}


def item(verdict, detail, **data):
    return {"verdict": verdict, "detail": detail, **data}


def boot_state(host):
    """Загрузка ОС: systemd running (degraded — FAIL), время с загрузки, неуспешные юниты."""
    out = cvl.ssh(host, "systemctl is-system-running; cut -d. -f1 /proc/uptime; systemctl --failed --no-legend | wc -l")
    lines = (out.splitlines() + ["", "", ""])[:3]
    state, uptime, failed = lines[0].strip(), lines[1].strip(), lines[2].strip()
    ok = state == "running" and failed == "0"
    return ok, {"system": state, "uptime_s": int(uptime) if uptime.isdigit() else None,
                "failed_units": int(failed) if failed.isdigit() else None}


def ui_services(ch):
    return {(s.get("side"), s.get("name")): s for s in cvl.jread(ch, UI).get("services") or []}


def judge_services(rows, side):
    want = MANDATORY[side]
    seen = {name: rows.get((side, name)) for name in want}
    bad = [f"{n}: {(s or {}).get('state', 'нет в диагностике')}" for n, s in seen.items()
           if not s or s.get("state") != "active"]
    return item("PASS" if not bad else "FAIL", "; ".join(bad) or ", ".join(f"{n} active" for n in want),
                services={n: s for n, s in seen.items()})


def check_p1(ch, rh):
    ok, boot = boot_state(ch)
    ui = cvl.jread(ch, UI)
    age = time.time() - ui["time"] if isinstance(ui.get("time"), (int, float)) else None
    gui = cvl.ssh(ch, "systemctl is-active sheregesh-console; pgrep -x cage >/dev/null && echo cage").split()
    ok = ok and gui[:1] == ["active"] and "cage" in gui and age is not None and age < 5
    return item("PASS" if ok else "FAIL", f"ОС {boot['system']}, отказавших юнитов {boot['failed_units']}, "
                f"GUI {'работает' if ok else 'не подтверждён'} (ui-state {age and round(age, 1)} с); без холодной "
                "перезагрузки — оператор подтверждает загрузку и изображение на дисплее", boot=boot, ui_age_s=age)


def check_p4(ch, rh):
    return judge_services(ui_services(ch), "console")


def check_r1(ch, rh):
    ok, boot = boot_state(rh)
    return item("PASS" if ok else "FAIL", f"ОС {boot['system']}, отказавших юнитов {boot['failed_units']}; индикатор — "
                "оператор", boot=boot)


def check_r2(ch, rh):
    return judge_services(ui_services(ch), "remote")


def check_r4(ch, rh):
    """Паспорт блока в канале пульта совпадает с sheregesh-info блока; температура SoC — с датчиком платы ±5 °C."""
    st = cvl.jread(ch, C_STATUS)
    peer, tel = st.get("peer") or {}, st.get("telemetry") or {}
    try:
        info = json.loads(cvl.ssh(rh, "sheregesh-info --json") or "{}")
    except ValueError:
        info = {}
    raw = cvl.ssh(rh, "cat /sys/class/thermal/thermal_zone0/temp")
    real = int(raw) / 1000 if raw.isdigit() else None
    shown = (tel.get("temperatures") or {}).get("soc")
    bad = []
    for k, want in (("software", info.get("package")), ("hardware", info.get("hardware")),
                    ("device_id", info.get("device_id"))):
        if not peer.get(k) or peer.get(k) != want:
            bad.append(f"{k}: пульт {peer.get(k)!r}, блок {want!r}")
    if shown is None:
        bad.append("температура блока на пульте не показана")
    elif real is None or abs(shown - real) > TEMP_TOLERANCE_C:
        bad.append(f"температура SoC: пульт {shown} °C, датчик блока {real} °C (допуск ±{TEMP_TOLERANCE_C:.0f})")
    return item("PASS" if not bad else "FAIL", "; ".join(bad) or f"паспорт совпадает, SoC {shown} °C (датчик {real})",
                peer=peer, info=info, temp_shown_c=shown, temp_sensor_c=real)


def check_s1(ch, rh):
    st = cvl.jread(ch, C_STATUS)
    peer = st.get("peer") or {}
    ok = st.get("phase") == "connected" and bool(peer.get("proto")) and st.get("session") == peer.get("proto")
    return item("PASS" if ok else "FAIL", f"фаза {st.get('phase')}, протокол блока {peer.get('proto')}, сессия "
                f"{st.get('session')}", phase=st.get("phase"), session=st.get("session"), peer_proto=peer.get("proto"))


def check_s2(ch, rh, window):
    r = cvl.sc_telemetry(ch, rh, window=window)
    m = r.get("metrics") or {}
    return item(r["verdict"], f"{m.get('seconds')} с: сообщений {m.get('messages_per_s')}/с, разрывов seq "
                f"{m.get('seq_gaps')}, событий {m.get('events_sent')} (потеряно {m.get('event_loss')})", metrics=m)


def check_s3(ch, rh):
    if cvl.wait(cvl.playing(ch), 30) is None:                # 0.0 — уже играет, не «нет»
        return item("FAIL", "видео на пульте не воспроизводится")
    time.sleep(6)
    v = cvl.cv(ch)
    fps = v.get("decoded_fps_5s")
    ok = isinstance(fps, (int, float)) and fps >= MIN_FPS
    return item("PASS" if ok else "FAIL", f"{fps} к/с декодировано за 5 с, {v.get('mode')}", fps=fps,
                source=(cvl.jread(ch, C_STATUS).get("telemetry") or {}).get("video", {}).get("source", {}).get("input"))


def check_s5(ch, rh, pause=6):
    """Неисправность блока — служба управления блока остановлена SIGSTOP на pause с: на пульте — событие PEER-LOST
    (нового id) в списке событий; затем блок возобновляется и событие снимается."""
    pid = cvl._pid(rh, "sheregesh-remote-net")
    if pid is None:
        return item("ERROR", "нет PID sheregesh-remote-net")
    last = max([e.get("id", 0) for e in cvl.jread(ch, UI).get("events") or []] or [0])
    t_stop = cvl._signal(rh, pid, "STOP")
    if t_stop is None:
        return item("ERROR", "SIGSTOP не доставлен")
    seen, resumed = None, None
    try:
        t0 = time.monotonic()
        while time.monotonic() - t0 < pause + 4 and seen is None:
            ev = [e for e in cvl.jread(ch, UI).get("events") or [] if e.get("id", 0) > last
                  and e.get("code") == "PEER-LOST" and e.get("active")]
            if ev:
                seen = round(time.monotonic() - t0, 1)
            time.sleep(0.5)
    finally:
        resumed = cvl._signal(rh, pid, "CONT")
    if resumed is None:
        return item("ERROR", "возобновление блока не подтверждено", abort=True)
    back = cvl.wait(lambda: cvl.jread(ch, C_STATUS).get("phase") == "connected", 60)
    ok = seen is not None and back is not None
    return item("PASS" if ok else "FAIL", f"PEER-LOST на пульте через {seen} с, связь снова через {back} с",
                event_after_s=seen, reconnect_s=back)


AUTO = {"P1": check_p1, "P4": check_p4, "R1": check_r1, "R2": check_r2, "R4": check_r4, "S1": check_s1,
        "S3": check_s3, "S5": check_s5}


def ask(iid, text, auto=None):
    hint = f" (автоматически: {auto['verdict']} — {auto['detail']})" if auto else ""
    while True:
        ans = input(f"\n{iid} {text}{hint}\n  PASS / FAIL[: причина] / пусто — не проверялось > ").strip()
        if not ans:
            return None
        if ans.upper().startswith(("PASS", "FAIL")):
            return ans


def manual(answer):
    if not answer:
        return item("MANUAL", "оператор не проверял")
    verdict, _, why = answer.partition(":")
    verdict = verdict.strip().upper()
    return item(verdict if verdict in ("PASS", "FAIL") else "MANUAL", why.strip() or "оператор")


def combine(auto, op):
    """auto+manual: PASS — только оба PASS."""
    if auto["verdict"] != "PASS":
        return {**auto, "operator": op}
    return {**auto, "verdict": op["verdict"], "detail": f"{auto['detail']}; оператор: {op['detail']}", "operator": op}


def run(ch, rh, suites, answers, interactive=False, exchange_s=300.0):
    out = {}
    for iid, suite, text, how in ITEMS:
        if suite not in suites:
            continue
        auto = None
        if how.startswith("auto"):
            fn = AUTO.get(iid)
            try:
                auto = check_s2(ch, rh, exchange_s) if iid == "S2" else fn(ch, rh)
            except Exception as e:                       # проба не роняет протокол: пункт — ERROR
                auto = item("ERROR", f"{type(e).__name__}: {e}")
        if how == "auto":
            res = auto
        else:
            op = manual(ask(iid, text, auto) if interactive else answers.get(iid))
            res = op if auto is None else combine(auto, op)
        out[iid] = {"suite": suite, "text": text, "how": how, **res}
        print(f"{iid} {res['verdict']:6} {res['detail']}"[:200], flush=True)
        if res.get("abort"):
            break
    return out


def summary(results, suites):
    by = {}
    for s in suites:
        rows = [r for r in results.values() if r["suite"] == s]
        expected = sum(1 for _, su, _, _ in ITEMS if su == s)
        by[TESTS[s]] = {"verdict": "PASS" if rows and len(rows) == expected and all(r["verdict"] == "PASS"
                                                                                     for r in rows) else "FAIL",
                        "pass": sum(r["verdict"] == "PASS" for r in rows), "total": expected,
                        "manual": sum(r["verdict"] == "MANUAL" for r in rows)}
    return by


def protocol_md(meta, results, by):
    lines = [f"# Smoke Test {meta['time']}", "",
             f"Пульт {meta['console']} · блок {meta['remote']} · выпуск пульта `{meta.get('release_console')}`, блока "
             f"`{meta.get('release_remote')}`", ""]
    for test, s in by.items():
        lines += [f"## {test}: {s['verdict']} ({s['pass']}/{s['total']}, без проверки оператора {s['manual']})", "",
                  "| Пункт | Что | Как | Итог | Подробно |", "|---|---|---|---|---|"]
        lines += [f"| {iid} | {r['text']} | {r['how']} | {r['verdict']} | {r['detail']} |".replace("\n", " ")
                  for iid, r in results.items() if TESTS[r["suite"]] == test]
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console", required=True)
    ap.add_argument("--remote", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--suite", default="P,R,S")
    ap.add_argument("--interactive", action="store_true")
    ap.add_argument("--manual", type=Path, help="ответы оператора JSON {пункт: «PASS» | «FAIL: причина»}")
    ap.add_argument("--exchange-s", type=float, default=300.0, help="S2: окно обмена, с (реестр — 5 мин)")
    a = ap.parse_args(argv)
    suites = [s.strip().upper() for s in a.suite.split(",") if s.strip()]
    answers = json.loads(a.manual.read_text()) if a.manual else {}
    a.out.mkdir(parents=True, exist_ok=True)
    rel = "readlink /var/lib/sheregesh-deploy/current 2>/dev/null || echo factory"
    meta = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "console": a.console, "remote": a.remote,
            "release_console": cvl.ssh(a.console, rel), "release_remote": cvl.ssh(a.remote, rel),
            "exchange_s": a.exchange_s}
    results = run(a.console, a.remote, suites, answers, a.interactive, a.exchange_s)
    by = summary(results, suites)
    (a.out / "smoke.json").write_text(json.dumps({"meta": meta, "tests": by, "items": results}, ensure_ascii=False,
                                                 indent=1))
    (a.out / "PROTOCOL.md").write_text(protocol_md(meta, results, by))
    print(json.dumps(by, ensure_ascii=False))
    return 0 if all(s["verdict"] == "PASS" for s in by.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
