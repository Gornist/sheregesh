# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Стенд без маршрута по умолчанию и без DNS (TEST-SYS-004, часть на работающих платах). Запуск с машины владельца.

  python3 tools/probe/offline_stand.py --console 192.168.1.94 --remote 192.168.1.173 --out DIR [--minutes 10]

На обеих платах (root по ключу, DEV-образ): запись исходящих пакетов eth0 (AF_PACKET, без tcpdump), снятие маршрута
по умолчанию и серверов DNS канала; затем на пульте — изменение и применение настройки модуля через локальный канал
(`module configure auto_recover`, новая ревизия хранилища на блоке), диагностика (`sheregesh-health --json` обеих
плат, раздел «Диагностика» GUI, связь пульт ↔ блок), ожидание; в конце маршрут и DNS возвращаются
(`resolvectl revert eth0`, `networkctl reconfigure eth0`). Итог: пакетов за пределы подсети стенда 0, запросов DNS 0, записей err служб
sheregesh за окно 0. Первая загрузка свежезаписанного образа здесь не проверяется — это L3 `boot_check.sh offline`
и запись карты владельцем.
"""
import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

SNIFF = r'''
import ipaddress, json, signal, socket, struct, sys, time
net = ipaddress.ip_network(sys.argv[1]); out = sys.argv[2]
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(3)); s.bind(("eth0", 0)); s.settimeout(1)
stat = {"total": 0, "outgoing": 0, "external": 0, "dns": 0, "samples": [], "started": time.time()}
stop = []
signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
def local(a):
    return a in net or a.is_multicast or a.is_link_local or a == ipaddress.ip_address("255.255.255.255") or a.is_loopback
while not stop:
    try:
        pkt, addr = s.recvfrom(65535)
    except socket.timeout:
        continue
    stat["total"] += 1
    if addr[2] != socket.PACKET_OUTGOING:
        continue
    stat["outgoing"] += 1
    et = struct.unpack("!H", pkt[12:14])[0]
    if et == 0x0800:
        ihl = (pkt[14] & 15) * 4; proto = pkt[23]; dst = ipaddress.ip_address(pkt[30:34]); l4 = 14 + ihl
    elif et == 0x86DD:
        proto = pkt[20]; dst = ipaddress.ip_address(pkt[38:54]); l4 = 54
    else:
        continue
    dport = struct.unpack("!H", pkt[l4 + 2:l4 + 4])[0] if proto in (6, 17) and len(pkt) >= l4 + 4 else None
    ext, dns = not local(dst), dport in (53, 853)
    stat["external"] += ext; stat["dns"] += dns
    if (ext or dns) and len(stat["samples"]) < 20:
        stat["samples"].append({"t": round(time.time(), 2), "dst": str(dst), "proto": proto, "dport": dport})
stat["stopped"] = time.time()
open(out, "w").write(json.dumps(stat))
'''


def ssh(host, cmd, timeout=120, user="root"):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"{user}@{host}", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


CTL = r'''
import socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(float(sys.argv[2]))
s.connect("/run/user/1000/sheregesh-console.sock"); s.sendall(sys.argv[1].encode() + b"\n")
b = b""
while not b.endswith(b"\n"):
    c = s.recv(65536)
    if not c:
        break
    b += c
print(b.decode())
'''


def ctl(console, req, timeout=60):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", f"sheregesh-ui@{console}",
                        f"python3 - {shlex.quote(json.dumps(req))} {timeout}"],
                       input=CTL, capture_output=True, text=True, timeout=timeout + 15)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"result": "error", "detail": r.stdout or r.stderr}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--console", required=True)
    p.add_argument("--remote", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--minutes", type=float, default=10)
    p.add_argument("--subnet", default="192.168.1.0/24")
    a = p.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    boards = {"console": a.console, "remote": a.remote}
    res = {"steps": [], "boards": {}}

    def step(name, ok, detail):
        res["steps"].append({"step": name, "ok": bool(ok), "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {name}: {str(detail)[:300]}", flush=True)

    t_start = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    for role, h in boards.items():
        ssh(h, "mkdir -p /run/shoffline")
        subprocess.run(["ssh", f"root@{h}", "cat > /run/shoffline/sniff.py"], input=SNIFF, text=True, check=True)
        rc, o, e = ssh(h, f"systemd-run --quiet --unit=shoffline-sniff python3 -I /run/shoffline/sniff.py "
                          f"{a.subnet} /run/shoffline/stat.json && sleep 1 && systemctl is-active shoffline-sniff")
        step(f"{role}: запись трафика", o == "active", o or e)
    try:
        for role, h in boards.items():
            rc, o, e = ssh(h, "ip route del default; resolvectl dns eth0 ''; resolvectl domain eth0 ''; "
                              "ip -4 route show default; echo ---; resolvectl dns eth0")
            gw, dns = o.split("---")
            step(f"{role}: без маршрута и DNS", not gw.strip() and not dns.split(":", 1)[-1].strip(), o)
        rc, o, e = ssh(a.console, "getent hosts deb.debian.org; echo rc=$?", user="sheregesh-ui")
        step("пульт: имя внешнего сервера не разрешается", "rc=0" not in o, o)
        before = ctl(a.console, {"cmd": "module", "op": "status"})
        rev0 = ((before.get("module") or {}).get("telemetry") or {}).get("settings", {})
        r1 = ctl(a.console, {"cmd": "module", "op": "configure", "args": {"auto_recover": False}, "wait": 30})
        r2 = ctl(a.console, {"cmd": "module", "op": "configure", "args": {"auto_recover": True}, "wait": 30})
        after = ctl(a.console, {"cmd": "module", "op": "status"})
        rev1 = ((after.get("module") or {}).get("telemetry") or {}).get("settings", {})
        step("изменение и применение настройки (модуль, auto_recover)",
             r1.get("result") == "ok" and r2.get("result") == "ok" and rev0 != rev1,
             {"before": rev0, "after": rev1, "r1": r1.get("result"), "r2": r2.get("result")})
        for role, h in boards.items():
            rc, o, e = ssh(h, "sheregesh-health --json", user="sheregesh-ui")
            try:
                hj = json.loads(o)
            except ValueError:
                hj = {}
            res["boards"].setdefault(role, {})["health"] = hj
            step(f"{role}: диагностика sheregesh-health", rc == 0 and bool(hj), f"rc={rc} " + (o[:200] or e))
        u = ctl(a.console, {"cmd": "ui", "op": "open", "section": "diagnostics"})
        step("пульт: раздел «Диагностика» GUI", u.get("result") == "ok", (u.get("ui") or {}).get("section", u))
        deadline = time.time() + a.minutes * 60
        while time.time() < deadline:
            time.sleep(min(30, max(0, deadline - time.time())))
        rc, o, e = ssh(a.console, "cat /run/sheregesh-ui/link-status.json", user="sheregesh-ui")
        try:
            phase = json.loads(o).get("phase")
        except ValueError:
            phase = o or e
        step(f"связь пульт ↔ блок после {a.minutes:g} мин", phase == "connected", phase)
    finally:
        for role, h in boards.items():
            ssh(h, "systemctl stop shoffline-sniff; resolvectl revert eth0; networkctl reconfigure eth0")
        time.sleep(8)
        for role, h in boards.items():
            rc, o, e = ssh(h, "ip -4 route show default; resolvectl dns eth0; cat /run/shoffline/stat.json; "
                              "rm -rf /run/shoffline")
            lines = o.splitlines()
            try:
                stat = json.loads(lines[-1])
            except (ValueError, IndexError):
                stat = {"error": o or e}
            res["boards"].setdefault(role, {})["traffic"] = stat
            dns_back = len(lines) > 1 and lines[1].split(":", 1)[-1].strip()
            step(f"{role}: маршрут и DNS возвращены", "default via" in o and bool(dns_back), " ".join(lines[:2]))
            step(f"{role}: пакеты за пределы подсети стенда", stat.get("external") == 0,
                 {k: stat.get(k) for k in ("outgoing", "external", "samples")})
            step(f"{role}: запросы DNS", stat.get("dns") == 0, stat.get("dns"))
            rc, o, e = ssh(h, f"journalctl --since '{t_start}' -p err -o cat --no-pager -u 'sheregesh-*' | head -20")
            res["boards"][role]["errors"] = o.splitlines()
            step(f"{role}: записи err служб sheregesh за окно", not o.strip(), o[:300] or "0")
    res["ok"] = all(s["ok"] for s in res["steps"])
    (out / "offline-stand.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print("итог", "PASS" if res["ok"] else "FAIL")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
