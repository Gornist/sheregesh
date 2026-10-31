#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""TEST-SW-001 на физических платах: принудительное завершение обязательной службы → запись о сбое в журнале,
перезапуск systemd, событие SVC-RESTART в GUI пульта не позднее 10 с. С машины владельца.

  python3 tools/probe/check_services.py --console 192.168.1.94 --remote 192.168.1.173 --out DIR [--only a,b]

Для каждой службы (docs/software/services.md, постоянные): root по ключу (DEV-образ) — время платы и `kill -9`
основного процесса; затем NRestarts вырос и ActiveState active; в journalctl юнита — «Main process exited … KILL» или
«Failed with result 'signal'»; в /run/sheregesh-ui/ui-state.json пульта (поле events, обновляется раз в 1 с) — событие
SVC-RESTART со службой; время — от kill по часам платы, на которой служба, до записи ui-state по часам пульта (часы
плат синхронизированы chrony, ADR-0017; погрешность — доли миллисекунды, разрешение — шаг записи 1 с).
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

SERVICES = [("console", "sheregesh-video-rx.service"), ("remote", "sheregesh-video-tx.service"),
            ("remote", "sheregesh-remote-net.service"), ("console", "sheregesh-console.service")]
UI_STATE = "/run/sheregesh-ui/ui-state.json"
EVENT_MAX_S = 10.0


def ssh(host, cmd, user="sheregesh-ui", timeout=60):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"{user}@{host}", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


def show(host, unit):
    out = ssh(host, f"systemctl show -p ActiveState,NRestarts,MainPID {unit}")
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def ui(ch):
    try:
        return json.loads(ssh(ch, f"cat {UI_STATE}") or "{}")
    except ValueError:
        return {}


def short(unit):
    return unit.removeprefix("sheregesh-").removesuffix(".service")


def check(ch, rh, side, unit):
    host = ch if side == "console" else rh
    before = show(host, unit)
    if before.get("ActiveState") != "active" or before.get("MainPID") in (None, "0"):
        return {"verdict": "ERROR", "reason": f"служба не работает до испытания: {before}"}
    seen_ids = {e["id"] for e in ui(ch).get("events", [])} if unit != "sheregesh-console.service" else set()
    t_kill = float(ssh(host, f"date +%s.%N; kill -9 {before['MainPID']}", user="root").splitlines()[0])
    restarted = None
    t0 = time.monotonic()
    while time.monotonic() - t0 < 60:
        st = show(host, unit)
        if st.get("ActiveState") == "active" and int(st.get("NRestarts", 0)) > int(before["NRestarts"]):
            restarted = st
            break
        time.sleep(0.5)
    event, t_event = None, None
    t0 = time.monotonic()
    while time.monotonic() - t0 < 60:
        st = ui(ch)
        for e in st.get("events", []):
            if e["code"] == "SVC-RESTART" and e["params"].get("service") == short(unit) and e["id"] not in seen_ids \
                    and st.get("time", 0) >= t_kill:
                event, t_event = e, st["time"]
                break
        if event:
            break
        time.sleep(0.25)
    journal = ssh(host, f"journalctl -u {unit} --since @{int(t_kill) - 1} --no-pager -o cat | grep -E "
                        f"\"KILL|signal|Main process exited|Scheduled restart\" | head -4", user="root")
    after_ui = ui(ch)
    event_s = round(t_event - t_kill, 2) if t_event else None
    ok = bool(restarted and journal and event_s is not None and event_s <= EVENT_MAX_S)
    return {"verdict": "PASS" if ok else "FAIL", "unit": unit, "host": host, "restarts": [before["NRestarts"],
            (restarted or {}).get("NRestarts")], "journal": journal.splitlines(), "event": event, "event_after_s": event_s,
            "criteria": {"event_after_s": f"<= {EVENT_MAX_S}", "restart": "NRestarts вырос, active",
                         "journal": "запись о завершении сигналом"},
            "ui_link": after_ui.get("linkView"), "ui_video": (after_ui.get("video") or {}).get("state")}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console", required=True)
    ap.add_argument("--remote", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--only", default="")
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    only = {s for s in a.only.split(",") if s}
    summary = {}
    for side, unit in SERVICES:
        if only and short(unit) not in only:
            continue
        print(f"== {unit}", flush=True)
        try:
            res = check(a.console, a.remote, side, unit)
        except Exception as e:                                # noqa: BLE001
            res = {"verdict": "ERROR", "reason": f"{type(e).__name__}: {e}"}
        (a.out / f"{short(unit)}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
        summary[short(unit)] = res["verdict"]
        print(f"   {res['verdict']} событие через {res.get('event_after_s')} с; {res.get('reason', '')}", flush=True)
        time.sleep(15)                                         # связь и видео восстанавливаются
    (a.out / "summary.json").write_text(json.dumps(summary))
    print(json.dumps(summary))
    return 0 if all(v == "PASS" for v in summary.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
