#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""TEST-SW-004 на физических платах: понятные локальные состояния, когда второго устройства нет. С машины владельца.

  python3 tools/probe/check_absent_peer.py --console 192.168.1.94 --remote 192.168.1.173 --out DIR [--only A,B]

Сценарии (DIR/<сценарий>.json, итог DIR/summary.json; PASS | FAIL | ERROR):
  console-alone  служба sheregesh-remote-net блока остановлена, GUI пульта перезапущен: «нет связи с удалённым блоком»
                 видно не позднее 10 с после старта (видимое сообщение Minimal, ui-state.json perf.videoMessage), затем
                 30 с: GUI жив (ui-state обновляется, перезапусков службы нет), связь в состоянии переподключения,
                 проверка здоровья пульта — link.state FAIL с понятной причиной, остальные проверки без новых FAIL.
  remote-alone   служба sheregesh-console пульта остановлена: 60 с службы блока работают без перезапусков, статус
                 канала блока (link-status.json) — «нет сессии», проверка здоровья — link.state «нет сессии», в журнале
                 блока — закрытие сессии.
После каждого сценария стенд возвращается: служба запущена, связь CONNECTED.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

UI_STATE = "/run/sheregesh-ui/ui-state.json"
REMOTE_STATUS = "/run/sheregesh-remote/link-status.json"
HEALTH = "/run/sheregesh-health/report.json"
NO_LINK = ("НЕТ СВЯЗИ С БЛОКОМ", "NO LINK TO THE REMOTE UNIT")   # перевод — software/console/…/i18n/sheregesh_ru.ts
SHOWN_MAX_S = 10.0


def ssh(host, cmd, timeout=120):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"sheregesh-ui@{host}", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


def jread(host, path):
    try:
        return json.loads(ssh(host, f"cat {path} 2>/dev/null") or "{}")
    except ValueError:
        return {}


def wait(pred, timeout, step=0.5):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return round(time.monotonic() - t0, 2)
        time.sleep(step)
    return None


def nrestarts(host, unit):
    v = ssh(host, f"systemctl show -p NRestarts --value {unit}")
    return int(v) if v.isdigit() else None


def health(host):
    """Свежий отчёт sheregesh-health (служба по таймеру — запускается явно)."""
    ssh(host, "systemctl start sheregesh-health.service", timeout=90)
    rep = jread(host, HEALTH)
    return {c["id"]: c for c in rep.get("checks", [])}


def connected(ch):
    return jread(ch, UI_STATE).get("linkView") == "CONNECTED"


def restore(ch, rh):
    ssh(rh, "systemctl start sheregesh-remote-net sheregesh-video-tx")
    ssh(ch, "systemctl start sheregesh-console")
    return wait(lambda: connected(ch), 60, 1) is not None


def sc_console_alone(ch, rh):
    if not connected(ch):
        return {"verdict": "ERROR", "reason": "нет связи до сценария"}
    before = {k for k, c in health(ch).items() if c["status"] == "FAIL"}
    ssh(rh, "systemctl stop sheregesh-remote-net")
    try:
        ssh(ch, "systemctl restart sheregesh-console")
        t0 = time.time()
        r0 = nrestarts(ch, "sheregesh-console")
        seen = {}

        def shown():
            st = jread(ch, UI_STATE)
            if st.get("time", 0) <= t0:
                return False
            msg = (st.get("perf") or {}).get("videoMessage") or ""
            if msg in NO_LINK and st.get("linkView") != "CONNECTED":
                seen.update(time=st["time"], msg=msg, linkView=st.get("linkView"), peerState=st.get("peerState"),
                            peerDetail=st.get("peerDetail"), status=(st.get("perf") or {}).get("status"))
                return True
            return False
        found = wait(shown, 30, 0.25)
        shown_s = round(seen["time"] - t0, 2) if seen else None
        times, msgs = [], set()
        for _ in range(15):                                   # 30 с: GUI жив и обновляет состояние
            time.sleep(2)
            st = jread(ch, UI_STATE)
            times.append(st.get("time"))
            msgs.add((st.get("perf") or {}).get("videoMessage") or "")
        alive = len({t for t in times if t}) >= 12
        r1 = nrestarts(ch, "sheregesh-console")
        h = health(ch)
        new_fail = sorted(k for k, c in h.items() if c["status"] == "FAIL" and k not in before | {"link.state"})
        link = h.get("link.state", {})
    finally:
        ok_back = restore(ch, rh)
    crit = {"shown_s": f"<= {SHOWN_MAX_S}", "gui_alive": "ui-state обновляется 30 с", "restarts": "0",
            "health": "link.state FAIL с причиной, новых FAIL нет"}
    passed = (found is not None and shown_s is not None and shown_s <= SHOWN_MAX_S and alive and r0 == r1
              and msgs <= set(NO_LINK) and link.get("status") == "FAIL" and not new_fail and ok_back)
    return {"verdict": "PASS" if passed else "FAIL", "shown_s": shown_s, "seen": seen, "gui_alive": alive,
            "messages_30s": sorted(msgs), "console_restarts": [r0, r1], "health_link": link,
            "health_new_fail": new_fail, "restored": ok_back, "criteria": crit}


def sc_remote_alone(ch, rh):
    if not connected(ch):
        return {"verdict": "ERROR", "reason": "нет связи до сценария"}
    units = ("sheregesh-remote-net", "sheregesh-video-tx")
    r0 = {u: nrestarts(rh, u) for u in units}
    t_since = ssh(rh, "date +%s")
    ssh(ch, "systemctl stop sheregesh-console")
    try:
        gone = wait(lambda: jread(rh, REMOTE_STATUS).get("connected") is False, 30, 0.5)
        time.sleep(60)
        active = {u: ssh(rh, f"systemctl is-active {u}") for u in units}
        r1 = {u: nrestarts(rh, u) for u in units}
        st = jread(rh, REMOTE_STATUS)
        h = health(rh)
        link = h.get("link.state", {})
        log = ssh(rh, f"journalctl -u sheregesh-remote-net --since @{t_since} --no-pager -o cat | tail -40")
        closed = [ln for ln in log.splitlines() if "closed" in ln or "session end" in ln or "lost" in ln][:5]
    finally:
        ok_back = restore(ch, rh)
    passed = (gone is not None and all(v == "active" for v in active.values()) and r0 == r1
              and st.get("connected") is False and "нет сессии" in str(link.get("detail", "")) and closed and ok_back)
    return {"verdict": "PASS" if passed else "FAIL", "status_gone_after_s": gone, "active": active,
            "restarts": [r0, r1], "status": {k: st.get(k) for k in ("connected", "session", "listen", "time")},
            "health_link": link, "journal": closed, "restored": ok_back,
            "criteria": {"services": "active, без перезапусков 60 с", "status": "connected=false",
                         "health": "link.state «нет сессии»", "journal": "закрытие сессии"}}


SCENARIOS = {"console-alone": sc_console_alone, "remote-alone": sc_remote_alone}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console", required=True)
    ap.add_argument("--remote", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    names = [n for n in (a.only.split(",") if a.only else SCENARIOS) if n]
    summary = {}
    for name in names:
        print(f"== {name}", flush=True)
        try:
            res = SCENARIOS[name](a.console, a.remote)
        except Exception as e:                                # noqa: BLE001 — итог сценария, а не падение пробы
            res = {"verdict": "ERROR", "reason": f"{type(e).__name__}: {e}"}
        res = {"scenario": name, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **res}
        (out / f"{name}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
        summary[name] = res["verdict"]
        print(f"   {res['verdict']}" + (f" — {res.get('reason')}" if res.get("reason") else ""), flush=True)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if all(v == "PASS" for v in summary.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
