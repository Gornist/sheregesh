# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S2-BOOT: сбор фактических данных загрузки образа с платы по SSH (только чтение) и поиск платы по MAC.

  python3 tools/probe/collect_boot.py find --mac d8:3a:dd:69:0e:90 [--net 192.168.1.0/24]
  python3 tools/probe/collect_boot.py collect --host 192.168.1.87 --board pi1 --tag first-boot --out DIR

collect выполняет на плате только читающие команды (ОС, ядро, слот, разделы, службы, журналы, порты,
температура, питание) и пишет DIR/<плата>-<метка>.json с ревизией образа и идентификатором платы.
Вердикт — по фактам: службы активны, нет неуспешных юнитов, корень только для чтения, эмулятор слушает только
loopback, нет признаков недостаточного питания. Ничего на плате не меняет; перезагрузку выполняет оператор.
"""
import argparse
import ipaddress
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UNITS = ["sheregesh-remote-emu", "sheregesh-console", "seatd", "ssh", "systemd-networkd", "systemd-timesyncd"]
COMMANDS = {
    "os_release": "cat /etc/os-release",
    "uname": "uname -a",
    "cmdline": "cat /proc/cmdline",
    "model": "tr -d '\\0' < /proc/device-tree/model",
    "serial": "tr -d '\\0' < /proc/device-tree/serial-number",
    "mac": "cat /sys/class/net/eth0/address",
    "ip": "ip -brief address show eth0",
    "routes": "ip route",
    "hostname": "hostname",
    "uptime": "cat /proc/uptime",
    "boots": "journalctl --list-boots --no-pager 2>/dev/null | tail -5",
    "slots": "ls -l /dev/disk/by-slot/ /dev/disk/by-slot/active/ /dev/disk/by-slot/other/ 2>&1",
    "active_boot": "readlink -f /dev/disk/by-slot/active/boot",
    "active_system": "readlink -f /dev/disk/by-slot/active/system",
    "autoboot": "cat /bootfs/autoboot.txt 2>&1",
    "lsblk": "lsblk -J -o NAME,SIZE,FSTYPE,LABEL,PARTLABEL,MOUNTPOINTS,RO",
    "findmnt_root": "findmnt -J -o TARGET,SOURCE,FSTYPE,OPTIONS /",
    "mounts": "findmnt -rn -o TARGET,SOURCE,FSTYPE,OPTIONS | grep -E ' (/|/boot/firmware|/bootfs|/persistent|/home|/var|/var/log/journal) ' || true",
    "df": "df -h / /persistent /boot/firmware /bootfs 2>&1",
    "free": "free -m",
    "throttled": "vcgencmd get_throttled",
    "temp": "vcgencmd measure_temp",
    "volts": "vcgencmd measure_volts core",
    "system_state": "systemctl is-system-running 2>&1 || true",
    "failed": "systemctl --failed --no-legend --plain 2>&1",
    "units": "systemctl show -p Id,ActiveState,SubState,NRestarts,ExecMainStatus " + " ".join(f"{u}.service" for u in UNITS),
    # В образе нет ss (iproute2): слушающие сокеты — из /proc/net, вывод «tcp|udp адрес:порт».
    "listen": "python3 -c \"import socket,struct\nfor f in ('tcp','tcp6','udp','udp6'):\n for l in open('/proc/net/'+f).read().splitlines()[1:]:\n  p=l.split(); a,o=p[1].split(':')\n  if f.startswith('udp') or p[3]=='0A': print(f, (socket.inet_ntoa(struct.pack('<I',int(a,16))) if len(a)==8 else ('[::1]' if a.endswith('01000000') and a.startswith('0'*24) else '[v6]'))+':'+str(int(o,16)))\"",
    "analyze": "systemd-analyze 2>&1 || true",
    "blame": "systemd-analyze blame 2>/dev/null | head -15",
    "journal_sheregesh": "journalctl -b --no-pager -o short-monotonic -u sheregesh-remote-emu -u sheregesh-console -n 120",
    "journal_err": "journalctl -b --no-pager -o short-monotonic -p err -n 60",
    "first_frame": "cat /run/sheregesh-ui/first-frame.json 2>&1",
    "devmode": "cat /etc/sheregesh/DEV-MODE 2>&1",
    "package": "dpkg-query -W sheregesh",
    "image_id": "grep -hE 'IMAGE_ID|BUILD_ID|IMAGE_VERSION' /etc/os-release /etc/rpi-image-gen* /usr/lib/os-release 2>/dev/null | sort -u",
    "whoami": "id",
}


def ssh(host, user, cmd, timeout=40):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
                        f"{user}@{host}", cmd], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def find(mac, net):
    """Пинг подсети (только ICMP, по одному пакету) и поиск MAC в таблице ARP Mac."""
    mac = mac.lower()
    hosts = [str(h) for h in ipaddress.ip_network(net).hosts()]
    with ThreadPoolExecutor(64) as ex:
        alive = {h for h, r in zip(hosts, ex.map(lambda h: subprocess.run(
            ["ping", "-c", "1", "-W", "1000" if sys.platform == "darwin" else "1", h], capture_output=True), hosts))
            if r.returncode == 0}
    arp = subprocess.run(["arp", "-an"], capture_output=True, text=True).stdout
    norm = lambda m: ":".join(f"{int(x, 16):02x}" for x in m.split(":"))           # noqa: E731 (macOS пишет «d8:3a:dd:69:e:90»)
    for line in arp.splitlines():
        m = re.search(r"\(([\d.]+)\) at ([0-9a-f:]+)", line)
        if m and norm(m[2]) == mac and m[1] in alive:   # в ARP могут остаться прежние адреса той же платы
            return m[1]
    return None


def collect(a):
    t0 = time.time()
    rc, _, err = ssh(a.host, a.user, "true")
    data = {"board": a.board, "tag": a.tag, "host": a.host, "user": a.user, "ssh_ok": rc == 0, "ssh_error": err,
            "collected": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "raw": {}}
    if rc == 0:
        for key, cmd in COMMANDS.items():
            _, out, e = ssh(a.host, a.user, cmd)
            data["raw"][key] = out if out else e
    raw = data["raw"]
    units = {}
    for block in raw.get("units", "").split("\n\n"):
        kv = dict(x.split("=", 1) for x in block.splitlines() if "=" in x)
        if kv.get("Id"):
            units[kv["Id"].removesuffix(".service")] = kv
    listen = [x.split()[1] for x in raw.get("listen", "").splitlines() if len(x.split()) == 2]
    emu_ports = [x for x in listen if x.endswith((":7700", ":5600"))]
    root = json.loads(raw.get("findmnt_root") or '{"filesystems":[{}]}')["filesystems"][0]
    thr = raw.get("throttled", "")
    pkg = raw.get("package", "")
    data["summary"] = {
        "serial": raw.get("serial"), "mac": raw.get("mac"), "model": raw.get("model"),
        "os": next((x.split("=", 1)[1].strip('"') for x in raw.get("os_release", "").splitlines()
                    if x.startswith("PRETTY_NAME=")), None),
        "kernel": raw.get("uname", "").split()[2] if raw.get("uname") else None,
        "image_package": pkg, "active_boot": raw.get("active_boot"), "active_system": raw.get("active_system"),
        "root": root, "throttled": thr, "temp": raw.get("temp"), "system_state": raw.get("system_state"),
        "failed_units": [x.split()[0] for x in raw.get("failed", "").splitlines() if x.strip()],
        "units": {u: f"{v.get('ActiveState')}/{v.get('SubState')} restarts={v.get('NRestarts')}" for u, v in units.items()},
        "emulator_listen": emu_ports, "listen": listen, "uptime_s": float(raw.get("uptime", "0 0").split()[0] or 0),
        "boot_time": raw.get("analyze", "").splitlines()[0] if raw.get("analyze") else None,
    }
    s = data["summary"]
    checks = {
        "ssh": data["ssh_ok"],
        "image": pkg.startswith("sheregesh\t") and bool(raw.get("devmode", "").startswith("DEV image")),
        "root_ro": "ro" in (root.get("options") or "").split(","),
        "slot": (s["active_system"] or "").startswith("/dev/mmcblk0p") and (s["active_boot"] or "").startswith("/dev/mmcblk0p"),
        "emulator_active": units.get("sheregesh-remote-emu", {}).get("ActiveState") == "active",
        "console_active": units.get("sheregesh-console", {}).get("ActiveState") == "active",
        "loopback_only": bool(emu_ports) and all(p.startswith(("127.", "[::1]")) for p in emu_ports),
        "lan_only_ssh_dhcp": all(x.startswith(("127.", "[::1]")) or x.endswith((":22", ":68")) for x in listen),
        "no_failed_units": not s["failed_units"],
        "power_ok": thr.strip() == "throttled=0x0",
    }
    data["checks"] = checks
    data["verdict"] = "PASS" if all(checks.values()) else "FAIL"
    data["elapsed_s"] = round(time.time() - t0, 1)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{a.board}-{a.tag}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    print(json.dumps({"file": str(path), "verdict": data["verdict"], "checks": checks, **s}, ensure_ascii=False, indent=1))
    return 0 if data["verdict"] == "PASS" else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find")
    f.add_argument("--mac", required=True)
    f.add_argument("--net", default="192.168.1.0/24")
    c = sub.add_parser("collect")
    c.add_argument("--host", required=True)
    c.add_argument("--board", required=True)
    c.add_argument("--tag", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--user", default="sheregesh-ui")
    a = ap.parse_args(argv)
    if a.cmd == "find":
        ip = find(a.mac, a.net)
        print(ip or "не найдено")
        return 0 if ip else 1
    return collect(a)


if __name__ == "__main__":
    sys.exit(main())
