#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Независимый от агента OTA снимок платы и сверка «до/после» (S3.2: шаг 1 — запись слота, шаг 4 — пробная загрузка).
Только чтение.

  python3 tools/deploy/ota-check/slot_guard.py snap pi2 HOST OUT.json
  python3 tools/deploy/ota-check/slot_guard.py compare BEFORE.json AFTER.json --mode install|trial|unchanged --version V
  python3 tools/deploy/ota-check/slot_guard.py compare BEFORE.json AFTER.json --mode rollback --version V [--trial T.json]

Вердикты: PASS (0), FAIL (1) — доказано несоответствие, ERROR (2) — доказательств недостаточно (снимок неполон,
команда не выполнилась, SSH не удался, серийный номер чужой, активный слот неоднозначен). ERROR никогда не PASS.

Вход — sheregesh-ui (без root), сырые разделы не читаются. Неизменность основного слота доказывается не состоянием
агента, а ядром: счётчики записанных и отброшенных (discard) секторов /sys/block/mmcblk0[/mmcblk0pN]/stat при том же
boot_id, плюс сверка «диск = сумма разделов» (запись через /dev/mmcblk0 в обход разделов видна как расхождение);
активный слот — по пяти независимым признакам; autoboot.txt — хеш и разбор файла. Хеши файлов смонтированных boot и
системы — дополнительные (частичные) доказательства. Отпечатки — SHA-256 файлов; содержимое ключей в вывод не попадает.
Ключ хоста SSH проверяется самим соединением (StrictHostKeyChecking=yes: сервер доказывает владение известным ключом).
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shdeploy import BOARDS  # noqa: E402 — серийные номера плат: единый источник

BOARD_ROLE = {"pi1": "console", "pi2": "remote"}
ROLE_UNIT = {"console": "sheregesh-console", "remote": "sheregesh-remote-net"}
LAYOUT = {"p1": "bootconfig", "p2": "boot_a", "p3": "boot_b", "p4": "system_a", "p5": "system_b", "p6": "persistent"}
SLOT_PARTS = {"A": ("p2", "p4"), "B": ("p3", "p5")}
EMPTY_SHA = hashlib.sha256(b"").hexdigest()
HEX64 = re.compile(r"[0-9a-f]{64}")
SECTOR = 512
BOOT_SIZE, SYSTEM_SIZE = 268435456, 2684354560

HOME_FILES = (".ssh/authorized_keys", ".config/sheregesh/link.json", ".config/sheregesh/console.role",
              ".config/sheregesh/remote.role", ".local/share/sheregesh/link/device.key",
              ".local/share/sheregesh/link/device.crt", ".local/share/sheregesh/link/ca.crt")
SYS_FILES = ("/etc/ssh/ssh_host_ed25519_key.pub", "/etc/ssh/ssh_host_ecdsa_key.pub", "/etc/ssh/ssh_host_rsa_key.pub",
             "/etc/machine-id")
UNITS = ("ssh", "sheregesh-console", "sheregesh-remote-net", "sheregesh-ota.socket", "sheregesh-health.timer")

# Общие функции снимка; каждая — одна цепочка через && или конвейер под pipefail: любой сбой → код ≠ 0.
PRELUDE = r"""
set -euo pipefail
export LC_ALL=C
kv() { printf '%s\t%s\n' "$1" "$2"; }
err() { printf '!ERR\t%s\t%s\n' "$1" "$2"; }
field() {   # field ИМЯ КОМАНДА…: команда обязана завершиться 0 и вывести непустое значение
  local name=$1 out rc=0
  shift
  out=$("$@" 2>/dev/null) || rc=$?
  if [ "$rc" -ne 0 ]; then err "$name" "код $rc"; elif [ -z "$out" ]; then err "$name" "пустое значение"; else kv "$name" "$out"; fi
  return 0
}
fsha() { local h; h=$(sha256sum -- "$1" | cut -c1-64) && [ "${#h}" -eq 64 ] && printf '%s' "$h"; }
fpm() { local h m; h=$(fsha "$1") && m=$(stat -c %a -- "$1") && printf '%s %s' "$h" "$m"; }
fpopt() { if [ -e "$1" ] || [ -L "$1" ]; then fpm "$1"; else printf 'absent'; fi; }
treehash() {   # «SHA-256 списка хешей» и число файлов; ошибка find/чтения или ни одного файла — отказ
  local list n
  list=$(find "$@" -type f -print0 | sort -z | xargs -0 -r sha256sum --) && n=$(printf '%s\n' "$list" | grep -c .) \
    && [ "$n" -gt 0 ] && printf '%s %s' "$(printf '%s\n' "$list" | sha256sum | cut -c1-64)" "$n"
}
"""

SNAP = PRELUDE + r"""
partname() { grep -h '^PARTNAME=' "/sys/class/block/$1/uevent" | cut -d= -f2; }
mm_part() { local mm; mm=$(findmnt -no MAJ:MIN "$1" | tr -d ' ') && [ -n "$mm" ] && grep -h '^PARTNAME=' "/sys/dev/block/$mm/uevent" | cut -d= -f2; }
link_part() { local d; d=$(readlink -e "/dev/disk/by-slot/$1") && partname "${d##*/}"; }
dt_u32() { od -An -tu1 "/proc/device-tree/chosen/bootloader/$1" | awk 'NF==4{print $1*16777216+$2*65536+$3*256+$4; ok=1} END{exit !ok}'; }
dt_part() { local n; n=$(dt_u32 partition) && partname "mmcblk0p$n"; }
iosnap() {     # согласованный срез счётчиков: диск до и после чтения разделов одинаков
  local i d1 d2 parts
  for i in 1 2 3 4 5 6 7 8 9 10; do
    d1=$(awk 'NF>=14{print $7, $14; ok=1} END{exit !ok}' /sys/block/mmcblk0/stat) || return 1
    parts=$(for n in 1 2 3 4 5 6; do awk -v n="$n" 'NF>=14{print "io.p" n "\t" $7 " " $14; ok=1} END{exit !ok}' \
      "/sys/block/mmcblk0/mmcblk0p$n/stat" || exit 1; done) || return 1
    d2=$(awk 'NF>=14{print $7, $14; ok=1} END{exit !ok}' /sys/block/mmcblk0/stat) || return 1
    if [ "$d1" = "$d2" ]; then printf '%s\nio.disk\t%s\n' "$parts" "$d1"; return 0; fi
    sleep 0.2
  done
  return 1
}
unit_state() { systemctl show "$1" -p LoadState -p ActiveState -p SubState -p NRestarts -p InvocationID | sort | tr '\n' ';'; }
health() { local j; j=$(sheregesh-health --json) || true; printf '%s' "$j" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["verdict"])'; }
jsonv() { local j; j=$("$@") && printf '%s' "$j" | python3 -I -c 'import json,sys; print(json.dumps(json.load(sys.stdin), sort_keys=True))'; }
mounts() { findmnt -rno SOURCE,TARGET -t ext4,vfat | sort | tr '\n' ';'; }

field serial sh -c "tr -d '\0' </proc/device-tree/serial-number"
field boot_id cat /proc/sys/kernel/random/boot_id
field package dpkg-query -W -f='${Version}' sheregesh
field build jsonv cat /usr/share/sheregesh/build.json
for n in 1 2 3 4 5 6; do field "partname.p$n" partname "mmcblk0p$n"; done
field slot.root mm_part /
field slot.boot mm_part /boot/firmware
field slot.link.active.system link_part active/system
field slot.link.active.boot link_part active/boot
field slot.link.other.system link_part other/system
field slot.link.other.boot link_part other/boot
field slot.dt dt_part
field dt.tryboot dt_u32 tryboot
field mnt.root findmnt -no OPTIONS /
field mnt.boot findmnt -no OPTIONS /boot/firmware
field mounts mounts
iosnap || err io "нет согласованного среза счётчиков"
field autoboot.sha256 fsha /bootfs/autoboot.txt
field autoboot sh -c "tr '\n' ';' </bootfs/autoboot.txt"
field tree.boot treehash /boot/firmware
field tree.system treehash /usr/lib/sheregesh /usr/share/sheregesh /usr/bin /etc/systemd
field ota.enable_present sh -c 'if test -e /persistent/sheregesh-ota/enable.json; then echo YES; else echo no; fi'
field ota.status jsonv sheregesh-ota-client status
field deploy.status jsonv sheregesh-deployd status
cd ~
for f in @HOME_FILES@; do field "fp.~/$f" fpopt "$f"; done
for f in @SYS_FILES@; do field "fp.$f" fpm "$f"; done
failed_units() { systemctl --failed --no-legend --plain | awk '{printf "%s ", $1; n++} END{if (!n) printf "none"}'; }
field failed failed_units
for u in @UNITS@; do field "unit.$u" unit_state "$u"; done
field health health
printf '!END\tok\n'
""".replace("@HOME_FILES@", " ".join(HOME_FILES)).replace("@SYS_FILES@", " ".join(SYS_FILES)) \
    .replace("@UNITS@", " ".join(UNITS))


def required(role):
    keys = ["serial", "boot_id", "package", "build", "slot.root", "slot.boot", "slot.link.active.system",
            "slot.link.active.boot", "slot.link.other.system", "slot.link.other.boot", "slot.dt", "dt.tryboot",
            "mnt.root", "mnt.boot", "mounts", "io.disk", "autoboot.sha256", "autoboot", "tree.boot", "tree.system",
            "ota.enable_present", "ota.status", "deploy.status", "failed", "health"]
    keys += [f"partname.{p}" for p in LAYOUT] + [f"io.{p}" for p in LAYOUT]
    keys += [f"fp.~/{f}" for f in HOME_FILES] + [f"fp.{f}" for f in SYS_FILES] + [f"unit.{u}" for u in UNITS]
    return keys


def identity_keys(role):
    """Отпечатки, которые обязаны пережить запись слота и пробную загрузку (persistent и общие данные)."""
    other = "console" if role == "remote" else "remote"
    return [k for k in (f"fp.~/{f}" for f in HOME_FILES) if not k.endswith(f"{other}.role")] + \
        [f"fp.{f}" for f in SYS_FILES]


def parse(text):
    fields, errors, complete = {}, [], False
    for ln in text.splitlines():
        parts = ln.split("\t")
        if parts[0] == "!END":
            complete = True
        elif parts[0] == "!ERR":
            errors.append(f"{parts[1] if len(parts) > 1 else '?'}: {parts[2] if len(parts) > 2 else 'ошибка'}")
        elif len(parts) == 2:
            if parts[0] in fields:
                errors.append(f"{parts[0]}: поле повторяется")
            fields[parts[0]] = parts[1]
    return fields, errors, complete


def snap(board, host, runner=subprocess.run):
    meta = {"board": board, "host": host, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "host_key": "strict"}
    try:
        r = runner(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes",
                    f"sheregesh-ui@{host}", "bash -s"], input=SNAP, capture_output=True, text=True, timeout=300)
        fields, errors, complete = parse(r.stdout)
        meta["ssh_rc"] = r.returncode
        if r.returncode != 0:
            errors.append(f"ssh/bash: код {r.returncode}: {(r.stderr or '').strip()[:200]}")
    except subprocess.TimeoutExpired:
        fields, errors, complete = {}, ["ssh: тайм-аут сбора снимка"], False
    meta["complete"] = complete
    return {"meta": meta, "fields": fields, "errors": errors}


def slot_of(partname):
    return {"boot_a": "A", "system_a": "A", "boot_b": "B", "system_b": "B"}.get(partname)


def autoboot_parts(text):
    """[all]/[tryboot] boot_partition из самого файла — независимо от разбора агентом."""
    out, sect = {}, None
    for ln in text.split(";"):
        ln = ln.strip()
        if ln.startswith("["):
            sect = ln.strip("[]")
        elif ln.startswith("boot_partition=") and sect:
            out[sect] = ln.split("=", 1)[1]
    return out


def validate(s):
    """→ (ошибки, выведенное). Любая ошибка — снимок не доказательство (ERROR)."""
    errs = list(s.get("errors") or [])
    meta, f = s.get("meta") or {}, s.get("fields") or {}
    board = meta.get("board")
    if board not in BOARDS:
        return errs + [f"неизвестная плата {board!r}"], {}
    if not meta.get("complete"):
        errs.append("сбор снимка прерван (нет маркера конца)")
    role = BOARD_ROLE[board]
    for k in required(role):
        v = f.get(k)
        if v is None:
            errs.append(f"{k}: нет поля")
        elif not v.strip():
            errs.append(f"{k}: пустое значение")
    for k, v in f.items():
        if EMPTY_SHA in v:
            errs.append(f"{k}: SHA-256 пустого потока")
        if k.startswith(("fp.", "autoboot.sha256", "tree.")) and v != "absent" and not HEX64.match(v):
            errs.append(f"{k}: не SHA-256: {v[:20]!r}")
    if errs:
        return errs, {}
    if f["serial"] != BOARDS[board]["serial"]:
        errs.append(f"серийный номер {f['serial']} ≠ {board} {BOARDS[board]['serial']}")
    for p, name in LAYOUT.items():
        if f[f"partname.{p}"] != name:
            errs.append(f"partname.{p} = {f[f'partname.{p}']!r}, ожидалось {name!r}")
    srcs = {k: slot_of(f[k]) for k in ("slot.root", "slot.boot", "slot.link.active.system", "slot.link.active.boot",
                                        "slot.dt")}
    other = {slot_of(f["slot.link.other.system"]), slot_of(f["slot.link.other.boot"])}
    act = set(srcs.values())
    if len(act) != 1 or None in act or len(other) != 1 or other == act:
        errs.append(f"активный слот неоднозначен: {srcs}, другой {sorted(map(str, other))}")
    io = {}
    for p in [*LAYOUT, "disk"]:
        try:
            w, d = (int(x) for x in f[f"io.{p}"].split())
            io[p] = (w, d)
        except ValueError:
            errs.append(f"io.{p}: не два числа: {f[f'io.{p}']!r}")
    if not errs and any(sum(io[p][i] for p in LAYOUT) != io["disk"][i] for i in (0, 1)):
        errs.append(f"счётчики: диск {io['disk']} ≠ сумме разделов — запись в обход разделов или иной учёт ядра")
    for k in ("ota.status", "deploy.status", "build"):
        try:
            json.loads(f[k])
        except ValueError:
            errs.append(f"{k}: не JSON")
    rf = f"fp.~/.config/sheregesh/{role}.role"
    if f.get(rf) == "absent":
        errs.append(f"{rf}: нет файла роли {role}")
    for k in identity_keys(role):
        if f.get(k) == "absent":
            errs.append(f"{k}: файла нет")
    if errs:
        return errs, {}
    return [], {"role": role, "active": act.pop(), "io": io}


def compare(before, after, mode, version, trial=None):
    """→ (вердикт, проверки). Отсутствующее поле или недостоверный снимок — ERROR, не PASS.
    trial — снимок пробной загрузки (только mode=rollback): доказательство, что возврат — новая загрузка после неё."""
    out = []

    def add(cid, ok, detail="", status=None):
        out.append({"id": cid, "status": status or ("PASS" if ok else "FAIL"), "detail": detail})

    eb, db = validate(before)
    ea, da = validate(after)
    for tag, es in (("до", eb), ("после", ea)):
        for e in es:
            add(f"evidence.{tag}", False, e, status="ERROR")
    if not eb and not ea and before["meta"]["board"] != after["meta"]["board"]:
        add("evidence.board", False, "снимки разных плат", status="ERROR")
    if any(r["status"] == "ERROR" for r in out):
        return "ERROR", out
    b, a = before["fields"], after["fields"]

    def same(k):
        add(f"same.{k}", b[k] == a[k], "" if b[k] == a[k] else f"{b[k][:60]!r} → {a[k][:60]!r}")

    role, unit = db["role"], ROLE_UNIT[db["role"]]
    for k in identity_keys(role):
        same(k)
    same("serial")
    same("autoboot.sha256")
    ab = autoboot_parts(a["autoboot"])
    add("bootconfig.autoboot", ab.get("all") == "2" and ab.get("tryboot") == "3", f"разбор файла: {ab}")
    sl = json.loads(a["ota.status"])
    st = sl.get("state", {})
    add("health", a["health"] == "PASS", a["health"])
    add("failed", a["failed"] == "none", a["failed"])
    if mode in ("install", "unchanged"):
        add("slot.active_unchanged", db["active"] == da["active"] == "A", f"{db['active']} → {da['active']}")
        for k in ("boot_id", "package", "build", "slot.root", "slot.boot", "slot.dt", "dt.tryboot", "mnt.root",
                  "mnt.boot", "mounts", "tree.boot", "tree.system", "ota.enable_present", "deploy.status", *(
                      f"unit.{u}" for u in UNITS)):
            same(k)
        add("mnt.root_ro", "ro" in a["mnt.root"].split(","), a["mnt.root"])
        act, oth = SLOT_PARTS[db["active"]], SLOT_PARTS["B" if db["active"] == "A" else "A"]
        if mode == "unchanged":                                 # плата, которую шаг не затрагивает (pi1)
            act, oth = (*act, *oth), ()
            same("ota.status")
        for p in ("p1", *act):
            dw, dd = (da["io"][p][i] - db["io"][p][i] for i in (0, 1))
            add(f"untouched.{p}.{LAYOUT[p]}", dw == 0 and dd == 0, f"записано секторов {dw}, отброшено {dd}")
        for p, size in zip(oth, (BOOT_SIZE, SYSTEM_SIZE)):
            dw = da["io"][p][0] - db["io"][p][0]
            add(f"written.{p}.{LAYOUT[p]}", dw * SECTOR >= size, f"записано {dw * SECTOR} байт, образ {size}")
        if mode == "install":
            add("ota.state", st.get("state") == "INSTALLED" and st.get("target") == "B"
                and st.get("version_target") == version,
                f"{st.get('state')} → {st.get('target')} {st.get('version_target')}")
    elif mode == "rollback":                                    # возврат на A после abort/сбоя пробной загрузки
        add("slot.back_on_A", db["active"] == da["active"] == "A", f"{db['active']} → {da['active']} "
            "(корень, boot, by-slot, device-tree — согласованно)")
        add("dt.tryboot", a["dt.tryboot"] == "0", f"флаг пробной загрузки {a['dt.tryboot']}")
        add("rebooted", b["boot_id"] != a["boot_id"], "boot_id сменился относительно снимка «до»")
        if trial is not None:                                   # снимок пробной загрузки: B, и после неё — новая загрузка
            et, dt = validate(trial)
            add("trial.evidence", not et and dt.get("active") == "B" and trial["meta"]["board"] == before["meta"][
                "board"], "; ".join(et) or f"пробная загрузка: активный {dt.get('active')}",
                status=None if not et else "ERROR")
            if not et:
                add("rebooted_after_trial", trial["fields"]["boot_id"] != a["boot_id"], "boot_id ≠ пробной загрузки")
        # Содержимое A и его /var (выпуск DEV deploy) — прежние: слот A не переписывался, конфигурация не терялась.
        for k in ("package", "build", "slot.root", "slot.boot", "slot.dt", "mnt.root", "mnt.boot", "mounts",
                  "tree.boot", "tree.system", "deploy.status"):
            same(k)
        add("mnt.root_ro", "ro" in a["mnt.root"].split(","), a["mnt.root"])
        for p in SLOT_PARTS["B"]:                              # отвергнутый слот с начала этой загрузки не писался
            w, d = da["io"][p]
            add(f"untouched_since_boot.{p}.{LAYOUT[p]}", w == 0 and d == 0, f"записано {w}, отброшено {d}")
        add("ota.state", st.get("state") in ("ROLLED_BACK", "IDLE") and not st.get("target"),
            f"{st.get('state')} → {st.get('target')}")
        for u in ("ssh", unit):
            s = dict(x.split("=", 1) for x in a[f"unit.{u}"].strip(";").split(";") if "=" in x)
            add(f"unit.{u}", s.get("ActiveState") == "active" and s.get("NRestarts") == "0", a[f"unit.{u}"])
    else:                                                       # trial: шаг 4, после пробной загрузки слота B
        add("slot.trial_B", db["active"] == "A" and da["active"] == "B", f"{db['active']} → {da['active']} "
            "(корень, boot, by-slot, device-tree — согласованно)")
        add("dt.tryboot", a["dt.tryboot"] == "1", f"флаг пробной загрузки {a['dt.tryboot']}")
        add("rebooted", b["boot_id"] != a["boot_id"], "boot_id сменился")
        add("build.version", json.loads(a["build"]).get("ota_version") == version, a["build"][:120])
        add("ota.state", st.get("state") in ("TRYBOOT", "HEALTH_PENDING") and st.get("target") == "B",
            f"{st.get('state')} → {st.get('target')}")
        for p in SLOT_PARTS["A"]:                              # прежний основной слот с начала этой загрузки не писался
            w, d = da["io"][p]
            add(f"untouched_since_boot.{p}.{LAYOUT[p]}", w == 0 and d == 0, f"записано {w}, отброшено {d}")
        for u in ("ssh", unit):
            s = dict(x.split("=", 1) for x in a[f"unit.{u}"].strip(";").split(";") if "=" in x)
            add(f"unit.{u}", s.get("ActiveState") == "active" and s.get("NRestarts") == "0", a[f"unit.{u}"])
    if mode == "rollback":                                      # abort разрешение не отзывает: отзыв — ota-disable
        add("ota.enable", True, f"{a['ota.enable_present']} (сведения; отзыв разрешения — отдельное решение)",
            status="INFO")
    else:
        want = "YES" if mode == "trial" else "no"
        add("ota.enable_" + ("present" if mode == "trial" else "absent"), a["ota.enable_present"] == want,
            a["ota.enable_present"])
    act_other = SLOT_PARTS["B" if da["active"] == "A" else "A"]
    mounted = [p for p in act_other if f"/dev/mmcblk0{p} " in a["mounts"].replace(";", " ;")]
    add("other_slot_not_mounted", not mounted, ",".join(mounted) or "нет")
    verdict = "ERROR" if any(r["status"] == "ERROR" for r in out) else \
        "FAIL" if any(r["status"] == "FAIL" for r in out) else "PASS"
    return verdict, out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snap")
    s.add_argument("board", choices=list(BOARDS))
    s.add_argument("host")
    s.add_argument("out")
    c = sub.add_parser("compare")
    c.add_argument("before")
    c.add_argument("after")
    c.add_argument("--mode", choices=("install", "trial", "unchanged", "rollback"), required=True)
    c.add_argument("--trial", help="rollback: снимок пробной загрузки (boot_id после возврата должен отличаться)")
    c.add_argument("--version", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "snap":
        d = snap(a.board, a.host)
        errs, info = validate(d)
        d["valid"], d["validation_errors"] = not errs, errs
        Path(a.out).write_text(json.dumps(d, ensure_ascii=False, indent=1))
        if errs:
            print(f"ERROR {a.host}: снимок не годится как доказательство:\n  " + "\n  ".join(errs))
            return 2
        print(f"OK {a.board} ({a.host}): {len(d['fields'])} полей, активный слот {info['active']} → {a.out}")
        return 0
    verdict, res = compare(json.loads(Path(a.before).read_text()), json.loads(Path(a.after).read_text()),
                           a.mode, a.version, json.loads(Path(a.trial).read_text()) if a.trial else None)
    for r in res:
        print(f"{r['status']} {r['id']}" + (f" — {r['detail']}" if r["detail"] else ""))
    n = {v: sum(r["status"] == v for r in res) for v in ("PASS", "FAIL", "ERROR")}
    print(f"== {verdict}: PASS {n['PASS']}, FAIL {n['FAIL']}, ERROR {n['ERROR']}")
    return {"PASS": 0, "FAIL": 1, "ERROR": 2}[verdict]


if __name__ == "__main__":
    sys.exit(main())
