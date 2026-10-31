#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""TEST-SW-005 на физических платах: закрытых ключей нет в журналах и файлах состояния. С машины владельца.

  python3 tools/probe/check_secrets_in_logs.py --console 192.168.1.94 --remote 192.168.1.173 --out DIR

На каждой плате (root по ключу, DEV-образ) выполняется сценарий, читающий настоящие секреты из мест хранения
(docs/security/secrets.md): ключ устройства канала управления (device.key) и закрытые ключи хоста SSH. Из каждого
ключа берутся три фрагмента тела по 24 символа; ищутся во всех журналах journald (все загрузки), /var/log,
/persistent/log, файлах состояния /run/sheregesh*, /tmp и каталогах экспорта. Ключи SRTP живут только в памяти служб:
в журнале допустимо только «key_b64: ***». Фрагменты ключей плату не покидают — наружу идут только счётчики.
Контроль: канарейка SHRG-CANARY-SW005-<случайная> пишется в journald (logger) и /tmp и должна найтись — иначе ERROR.
Итог: DIR/<плата>.json, DIR/summary.json; PASS — 0 совпадений везде.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ON_BOARD = r'''
import glob, json, os, re, subprocess
CANARY = "SHRG-CANARY-SW005-" + os.urandom(6).hex()     # контроль: поиск находит то, что есть
subprocess.run(["logger", "-t", "sheregesh-sw005", f"control canary {CANARY}"])
open("/tmp/sw005-control.txt", "w").write(CANARY)
SECRETS = sorted(set(os.path.realpath(x) for x in glob.glob("/home/sheregesh-ui/.local/share/sheregesh/link/device.key")
                     + glob.glob("/persistent/home/sheregesh-ui/.local/share/sheregesh/link/device.key")
                     + glob.glob("/etc/ssh/ssh_host_*_key")))
needles, sources = [], []
for p in SECRETS:
    try:
        body = "".join(l.strip() for l in open(p) if l.strip() and "-----" not in l)
    except OSError:
        continue
    if len(body) < 120:
        continue
    sources.append(p)
    for off in (40, len(body) // 2, len(body) - 64):
        needles.append(body[off:off + 24])
def grep_text(text):
    return sum(text.count(n) for n in needles)
def canary(text):
    return text.count(CANARY)
res = {"secret_files": sources, "needles": len(needles), "places": {}}
j = subprocess.run(["journalctl", "--no-pager", "-o", "cat", "-q"], capture_output=True, text=True, errors="replace").stdout
res["control"] = {"journald": canary(j)}
res["places"]["journald"] = {"bytes": len(j), "matches": grep_text(j),
                             "srtp_key_unmasked": len(re.findall(r"key_b64['\"]?\s*[:=]\s*['\"](?!\*\*\*)[A-Za-z0-9+/=]{16,}", j))}
for name, pattern in (("var-log", "/var/log/**/*"), ("persistent-log", "/persistent/log/**/*"),
                      ("run-state", "/run/sheregesh*/**/*"), ("tmp", "/tmp/**/*"),
                      ("exports", "/home/sheregesh-ui/**/*export*")):
    total = files = 0
    for f in glob.glob(pattern, recursive=True):
        if not os.path.isfile(f) or f in sources or "/journal/" in f or os.path.getsize(f) > 64 << 20:
            continue
        try:
            text = open(f, errors="replace").read()
            total += grep_text(text); files += 1
            if f == "/tmp/sw005-control.txt":
                res["control"]["tmp"] = canary(text)
        except OSError:
            pass
    res["places"][name] = {"files": files, "matches": total}
os.remove("/tmp/sw005-control.txt")
print(json.dumps(res))
'''


def board(host):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"root@{host}", "python3 -I -"],
                       input=ON_BOARD, capture_output=True, text=True, timeout=600)
    if r.returncode:
        return {"verdict": "ERROR", "reason": r.stderr.strip()[-300:]}
    res = json.loads(r.stdout)
    bad = {k: v for k, v in res["places"].items() if v.get("matches") or v.get("srtp_key_unmasked")}
    control = res.get("control", {})
    ok = res["needles"] >= 6 and not bad and control.get("journald", 0) >= 1 and control.get("tmp", 0) >= 1
    return {"verdict": "PASS" if ok else ("FAIL" if bad else "ERROR"), **res,
            **({"reason": "мало секретов для поиска или контрольная канарейка не найдена"}
               if not ok and not bad else {})}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console", required=True)
    ap.add_argument("--remote", required=True)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    summary = {}
    for name, host in (("console", a.console), ("remote", a.remote)):
        res = board(host)
        (a.out / f"{name}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
        summary[name] = res["verdict"]
        print(name, res["verdict"], json.dumps(res.get("places", res.get("reason")), ensure_ascii=False))
    (a.out / "summary.json").write_text(json.dumps(summary))
    return 0 if all(v == "PASS" for v in summary.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
