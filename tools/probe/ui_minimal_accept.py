#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Интерфейс Minimal на двух платах: что показывает рабочий пульт pi1 при видео от pi2, потеря связи с блоком
(агент pi2 заморожен SIGSTOP на --freeze с, как CR-08) и восстановление (SIGCONT). Источник — ui-state.json пульта
(perf.status, perf.videoMessage), ничего не нажимается; разделы открываются через сокет пульта ({"cmd": "ui"}).

  python3 tools/probe/ui_minimal_accept.py --console 192.168.1.92 --remote 192.168.1.171 --out DIR [--freeze 8]

Агент pi2 не перезапускается и не останавливается: только пауза процесса; OTA, слоты и выпуски не трогаются.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

STATE = "python3 -c 'import json;print(json.dumps(json.load(open(\"/run/sheregesh-ui/ui-state.json\"))))'"
SOCK = "/run/user/1000/sheregesh-console.sock"


def ssh(host, cmd, timeout=30):
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", f"sheregesh-ui@{host}", cmd], capture_output=True, text=True,
                       timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def ui_state(host):
    rc, out, _ = ssh(host, STATE)
    d = json.loads(out) if rc == 0 and out else {}
    p = d.get("perf") or {}
    return {"t": time.time(), "status": p.get("status"), "videoMessage": p.get("videoMessage"),
            "linkView": d.get("linkView"), "decoded_fps": (d.get("video") or {}).get("decoded_fps_5s"),
            "display": (d.get("videoSource") or {}).get("display"), "layout": p.get("layout")}


def ui_cmd(host, req):
    code = ("import json,socket;s=socket.socket(socket.AF_UNIX);s.settimeout(20);s.connect(%r);"
            "s.sendall((%r+chr(10)).encode());print(s.makefile().readline().strip())" % (SOCK, json.dumps(req)))
    rc, out, _ = ssh(host, "python3 -c " + json.dumps(code))
    return json.loads(out) if rc == 0 and out else {}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console", default="192.168.1.92")
    ap.add_argument("--remote", default="192.168.1.171")
    ap.add_argument("--freeze", type=float, default=8.0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows, res = [], {}

    def note(tag, s):
        s["tag"] = tag
        rows.append(s)
        print(f"{tag:10} {json.dumps({k: s[k] for k in ('status', 'videoMessage', 'decoded_fps', 'display')}, ensure_ascii=False)}",
              flush=True)

    before = ui_state(a.console)
    note("before", before)
    res["layout"] = before["layout"]
    res["before_ok"] = before["status"] is not None and before["status"]["link"] == "на связи" and not before["videoMessage"]
    # разделы рабочего пульта: открываются и закрываются по сокету (без дисплея время открытия не измеряется)
    opened = []
    for sec in ("video", "control", "module", "diagnostics", "system", ""):
        r = ui_cmd(a.console, {"cmd": "ui", "op": "open", "section": sec})
        opened.append(((r.get("ui") or {}).get("section"), r.get("result")))
    res["sections"] = opened
    res["sections_ok"] = [s for s, _ in opened] == ["video", "control", "module", "diagnostics", "system", ""]
    # потеря связи: пауза агента pi2
    rc, pid, _ = ssh(a.remote, "pgrep -u sheregesh-ui -f 'python3 -s -m sheregesh_remote' | head -1")
    if rc or not pid.isdigit():
        print("агент pi2 не найден", file=sys.stderr)
        return 2
    t0 = time.time()
    ssh(a.remote, f"kill -STOP {pid}")
    lost_at = None
    try:
        while time.time() - t0 < a.freeze:
            s = ui_state(a.console)
            note(f"frozen+{s['t'] - t0:.1f}", s)
            if lost_at is None and s["videoMessage"] and (s["status"] or {}).get("link", "").startswith("нет связи"):
                lost_at = s["t"] - t0
            time.sleep(0.5)
    finally:
        ssh(a.remote, f"kill -CONT {pid}")
    t1 = time.time()
    back_at = None
    while time.time() - t1 < 30:
        s = ui_state(a.console)
        note(f"cont+{s['t'] - t1:.1f}", s)
        if not s["videoMessage"] and (s["status"] or {}).get("link") == "на связи" and (s["decoded_fps"] or 0) >= 25:
            back_at = s["t"] - t1
            break
        time.sleep(0.5)
    res.update(lost_shown_s=lost_at, recovered_s=back_at, freeze_s=a.freeze,
               lost_text=next((r["videoMessage"] for r in rows if r["videoMessage"]), ""))
    res["verdict"] = "PASS" if (res["before_ok"] and res["sections_ok"] and lost_at is not None and back_at is not None
                                and res["layout"] == "minimal") else "FAIL"
    (out / "ui-minimal-boards.json").write_text(json.dumps({"result": res, "rows": rows}, ensure_ascii=False, indent=1))
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
