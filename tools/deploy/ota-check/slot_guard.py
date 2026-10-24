#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Независимый от агента OTA снимок платы и сверка «до/после» записи неактивного слота (S3.2, шаг 1). Только чтение.

  python3 tools/deploy/ota-check/slot_guard.py snap HOST OUT.json
  python3 tools/deploy/ota-check/slot_guard.py compare BEFORE.json AFTER.json --version V

Вход — sheregesh-ui (без root): сырые разделы не читаются, поэтому неизменность основного слота доказывается не
словами агента, а ядром — счётчиком записанных секторов /sys/block/mmcblk0/mmcblk0pN/stat (с момента загрузки,
boot_id тот же), монтированием A только для чтения, хешами файлов смонтированных boot_a и системы A, autoboot.txt.
Отпечатки ключей — SHA-256 файлов; содержимое закрытых ключей не читается в вывод и не покидает плату.
"""
import argparse
import json
import subprocess
import sys

SNAP = r"""
set -u
kv() { printf '%s\t%s\n' "$1" "$2"; }
kv serial "$(tr -d '\0' </proc/device-tree/serial-number)"
kv boot_id "$(cat /proc/sys/kernel/random/boot_id)"
kv package "$(dpkg-query -W -f='${Version}' sheregesh)"
kv build "$(tr -d '\n ' </usr/share/sheregesh/build.json)"
kv root "$(findmnt -no SOURCE,OPTIONS /)"
kv mount.boot "$(findmnt -no SOURCE,OPTIONS /boot/firmware)"
for l in active/boot active/system other/boot other/system bootconfig persistent; do kv "by-slot.$l" "$(readlink -f /dev/disk/by-slot/$l)"; done
for n in 1 2 3 4 5 6; do kv "partname.p$n" "$(grep -h PARTNAME /sys/class/block/mmcblk0p$n/uevent | cut -d= -f2)"; done
for n in 1 2 3 4 5 6; do kv "io.p$n" "$(awk '{print $7}' /sys/block/mmcblk0/mmcblk0p$n/stat)"; done
kv dt.partition "$(od -An -tu1 /proc/device-tree/chosen/bootloader/partition | tr -s ' ')"
kv dt.tryboot "$(od -An -tu1 /proc/device-tree/chosen/bootloader/tryboot 2>/dev/null | tr -s ' ')"
kv mount.other "$(findmnt -rno TARGET,SOURCE | grep -E 'mmcblk0p3|mmcblk0p5' || echo none)"
kv autoboot.sha256 "$(sha256sum /bootfs/autoboot.txt | cut -c1-64)"
kv tree.boot_a "$(cd /boot/firmware && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-64)"
kv tree.system_a "$(find /usr/lib/sheregesh /usr/share/sheregesh /usr/bin /etc/systemd -type f -print0 2>/dev/null | sort -z | xargs -0 sha256sum 2>/dev/null | sha256sum | cut -c1-64)"
kv ota.enable_present "$(test -e /persistent/sheregesh-ota/enable.json && echo YES || echo no)"
kv deploy.status "$(sheregesh-deployd status | tr -d '\n ')"
cd ~ && for f in .ssh/authorized_keys .config/sheregesh/link.json .config/sheregesh/console.role .config/sheregesh/remote.role .local/share/sheregesh/link/device.key .local/share/sheregesh/link/device.crt .local/share/sheregesh/link/ca.crt; do [ -e "$f" ] && kv "fp.$f" "$(sha256sum "$f" | cut -c1-64) $(stat -c %a "$f")"; done
for f in /etc/ssh/ssh_host_ed25519_key.pub /etc/ssh/ssh_host_ecdsa_key.pub /etc/ssh/ssh_host_rsa_key.pub /etc/machine-id; do kv "fp.$f" "$(sha256sum $f | cut -c1-64)"; done
kv failed "$(systemctl --failed --no-legend --plain | awk '{print $1}' | tr '\n' ' ')"
for u in ssh sheregesh-console sheregesh-remote-net sheregesh-ota.socket sheregesh-health.timer; do kv "unit.$u" "$(systemctl show $u -p ActiveState,SubState,NRestarts,InvocationID --value | tr '\n' ' ')"; done
kv health "$(sheregesh-health --json | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["verdict"])')"
kv ota.status "$(sheregesh-ota-client status | tr -d '\n')"
"""

SAME = ("serial", "boot_id", "package", "build", "root", "mount.boot", "dt.partition", "dt.tryboot", "mount.other",
        "autoboot.sha256", "tree.boot_a", "tree.system_a", "ota.enable_present", "deploy.status", "failed")
SAME_PREFIX = ("by-slot.", "partname.", "fp.", "unit.")
UNTOUCHED = ("io.p1", "io.p2", "io.p4")                      # bootconfig, boot_a, system_a — ни одного сектора
SECTOR = 512


def snap(host):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"sheregesh-ui@{host}", "bash -s"],
                       input=SNAP, capture_output=True, text=True, timeout=300)
    d = dict(ln.split("\t", 1) for ln in r.stdout.splitlines() if "\t" in ln)
    if "serial" not in d:
        raise SystemExit(f"{host}: снимок не получен: {r.stderr.strip()}")
    return d


def compare(b, a, version, boot_size=268435456, system_size=2684354560):
    out = []

    def add(cid, ok, detail=""):
        out.append({"id": cid, "status": "PASS" if ok else "FAIL", "detail": detail})

    for k in sorted(set(b) | set(a)):
        if k in SAME or k.startswith(SAME_PREFIX):
            add(f"same.{k}", b.get(k) == a.get(k), "" if b.get(k) == a.get(k) else f"{b.get(k)!r} → {a.get(k)!r}")
    for k in UNTOUCHED:
        d = int(a[k]) - int(b[k])
        add(f"untouched.{k}", d == 0, f"записано секторов: {d}")
    for k, size in (("io.p3", boot_size), ("io.p5", system_size)):
        d = int(a[k]) - int(b[k])
        add(f"written.{k}", d >= size // SECTOR, f"записано {d * SECTOR} байт, образ {size}")
    st = json.loads(a["ota.status"])
    s, sl = st.get("state", {}), st.get("slots", {})
    add("ota.state", s.get("state") == "INSTALLED" and s.get("target") == "B" and s.get("version_target") == version,
        f"{s.get('state')} → {s.get('target')} {s.get('version_target')}")
    add("ota.active", sl.get("active") == "A" and not sl.get("tryboot_now") and st.get("consistent") is True,
        f"активный {sl.get('active')}, tryboot {sl.get('tryboot_now')}, согласовано {st.get('consistent')}")
    add("ota.autoboot", st.get("autoboot") == {"default_part": 2, "tryboot_part": 3}, json.dumps(st.get("autoboot")))
    add("health", a.get("health") == "PASS", a.get("health", ""))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snap")
    s.add_argument("host")
    s.add_argument("out")
    c = sub.add_parser("compare")
    c.add_argument("before")
    c.add_argument("after")
    c.add_argument("--version", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "snap":
        d = snap(a.host)
        with open(a.out, "w") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        print(f"{a.host}: {len(d)} полей → {a.out}")
        return 0
    res = compare(json.load(open(a.before)), json.load(open(a.after)), a.version)
    for r in res:
        print(f"{r['status']} {r['id']}" + (f" — {r['detail']}" if r["detail"] else ""))
    bad = [r for r in res if r["status"] != "PASS"]
    print(f"== итог: PASS {len(res) - len(bad)}, FAIL {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
