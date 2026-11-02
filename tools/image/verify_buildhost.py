# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Автоматическая проверка образа сборочного хоста Pi 5 (ADR-0019) против os/config/sheregesh-buildhost-pi5.yaml.

  python3 tools/image/verify_buildhost.py IMAGE.img --variant sheregesh-buildhost-pi5 --out DIR

Структура (стандартная библиотека): MBR с подписью диска, два раздела — BOOT (FAT, тип 0x0C, загрузочный) и ROOT
(ext4, тип 0x83), выравнивание 8 МиБ, размеры, метки. Содержимое (debugfs из e2fsprogs и mtools; нет инструмента —
проверка SKIP с причиной, не PASS): fstab и cmdline ссылаются на PARTUUID этого диска, ядро Pi 5, PCIe для NVMe в
config.txt, вход по ключу (root тоже), пароли и хост-ключи SSH в образе отсутствуют, Docker и первичное
расширение корня включены, нужные инструменты на месте. Итог — DIR/verify.json и DIR/<имя>.packages.tsv; код 1 при FAIL.

Проверка образа не заменяет загрузку на Pi 5: прошивка, PCIe/NVMe, температура и скорость — NOT TESTED."""
import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

MiB = 1024 * 1024
SECTOR = 512
BOOT_MIN, ROOT_MIN = 512 * MiB, 6144 * MiB
HOSTNAME = "pi5"
USER = "shbuild"
TOOLS = ("usr/sbin/dockerd", "usr/bin/docker", "usr/bin/git", "usr/bin/zstd", "usr/bin/rsync", "usr/bin/python3",
         "usr/bin/growpart", "usr/sbin/resize2fs", "usr/sbin/nvme", "usr/bin/tmux")


class Report:
    def __init__(self):
        self.checks = []

    def add(self, cid, desc, ok, detail=""):
        status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        self.checks.append({"id": cid, "desc": desc, "status": status, "detail": detail})
        print(f"[{status}] {cid}: {desc}" + (f" — {detail}" if detail else ""))
        return ok


def read_mbr(f):
    f.seek(0)
    mbr = f.read(SECTOR)
    sig = struct.unpack_from("<I", mbr, 440)[0]
    parts = []
    for i in range(4):
        e = mbr[446 + 16 * i: 462 + 16 * i]
        ptype, lba, count = e[4], struct.unpack_from("<I", e, 8)[0], struct.unpack_from("<I", e, 12)[0]
        if ptype:
            parts.append({"index": i + 1, "boot": e[0] == 0x80, "type": ptype, "offset": lba * SECTOR,
                          "size": count * SECTOR})
    return {"valid": mbr[510:512] == b"\x55\xaa", "disksig": sig, "partitions": parts}


def fs_info(f, offset):
    """Тип ФС и метка по сигнатурам: ext2/3/4 — магия 0xEF53 суперблока; FAT — 0x55AA и строка типа."""
    f.seek(offset + 1024)
    sb = f.read(1024)
    if len(sb) == 1024 and struct.unpack_from("<H", sb, 56)[0] == 0xEF53:
        incompat = struct.unpack_from("<I", sb, 96)[0]
        return ("ext4" if incompat & 0x40 else "ext2/3"), sb[120:136].rstrip(b"\0").decode(errors="replace")
    f.seek(offset)
    bs = f.read(SECTOR)
    if bs[510:512] == b"\x55\xaa" and bs[82:90].startswith(b"FAT32"):
        return "fat", bs[71:82].decode(errors="replace").strip()
    return "unknown", ""


class Ext4:
    """Чтение файлов ext4 из раздела образа через debugfs (без монтирования и без root)."""

    def __init__(self, img, part, tmp):
        self.path = Path(tmp) / "root.ext4"
        with open(img, "rb") as src, open(self.path, "wb") as dst:
            src.seek(part["offset"])
            left = part["size"]
            while left:
                chunk = src.read(min(left, 8 * MiB))
                dst.write(chunk)
                left -= len(chunk)

    def run(self, cmd):
        return subprocess.run(["debugfs", "-R", cmd, str(self.path)], capture_output=True, text=True).stdout

    def cat(self, path):
        return self.run(f"cat {path}")

    def exists(self, path):
        return "Inode:" in self.run(f"stat {path}")

    def ls(self, path):
        names = []
        for line in self.run(f"ls -p {path}").splitlines():
            p = line.split("/")
            if len(p) >= 7 and p[1].isdigit() and p[5] not in ("", ".", ".."):
                names.append(p[5])
        return names


def mtype(img, part, name):
    r = subprocess.run(["mtype", "-i", f"{img}@@{part['offset']}", f"::{name}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def mexists(img, part, name):
    """Файл есть в FAT-разделе (бинарные ядро и dtb читать как текст нельзя — только наличие и размер)."""
    r = subprocess.run(["mdir", "-i", f"{img}@@{part['offset']}", f"::{name}"], capture_output=True)
    return r.returncode == 0


def config_lines(text):
    return [x.strip() for x in text.splitlines() if x.strip() and not x.lstrip().startswith("#")]


def packages_from_status(status):
    """Установленные пакеты из /var/lib/dpkg/status: «имя<TAB>версия<TAB>архитектура»."""
    rows = []
    for block in status.split("\n\n"):
        f = dict(re.findall(r"^([A-Za-z-]+): (.*)$", block, re.M))
        if f.get("Package") and "install ok installed" in f.get("Status", ""):
            rows.append(f"{f['Package']}\t{f.get('Version', '')}\t{f.get('Architecture', '')}")
    return "\n".join(sorted(rows)) + "\n"


def verify(img, variant, out):
    rep = Report()
    size = os.path.getsize(img)
    with open(img, "rb") as f:
        mbr = read_mbr(f)
        rep.add("mbr.sig", "MBR: сигнатура 0x55AA, подпись диска не нулевая", mbr["valid"] and mbr["disksig"] != 0,
                f"0x{mbr['disksig']:08x}")
        parts = mbr["partitions"]
        rep.add("layout.count", "два раздела: BOOT и ROOT", len(parts) == 2, f"{len(parts)}")
        for p in parts:
            p["fs"], p["label"] = fs_info(f, p["offset"])
    boot = next((p for p in parts if p["index"] == 1), None)
    root = next((p for p in parts if p["index"] == 2), None)
    if boot and root:
        rep.add("part.boot.type", "BOOT: тип 0x0C, загрузочный", boot["type"] == 0x0C and boot["boot"],
                f"0x{boot['type']:02x} boot={boot['boot']}")
        rep.add("part.boot.fs", "BOOT: FAT с меткой BOOT", boot["fs"] == "fat" and boot["label"] == "BOOT",
                f"{boot['fs']} «{boot['label']}»")
        rep.add("part.boot.size", f"BOOT: не меньше {BOOT_MIN // MiB} МиБ", boot["size"] >= BOOT_MIN, f"{boot['size'] // MiB} МиБ")
        rep.add("part.root.type", "ROOT: тип 0x83", root["type"] == 0x83, f"0x{root['type']:02x}")
        rep.add("part.root.fs", "ROOT: ext4 с меткой ROOT", root["fs"] == "ext4" and root["label"] == "ROOT",
                f"{root['fs']} «{root['label']}»")
        rep.add("part.root.size", f"ROOT: не меньше {ROOT_MIN // MiB} МиБ", root["size"] >= ROOT_MIN, f"{root['size'] // MiB} МиБ")
        for p, n in ((boot, "boot"), (root, "root")):
            rep.add(f"part.{n}.align", f"{n}: выравнивание 8 МиБ", p["offset"] % (8 * MiB) == 0, f"{p['offset']}")
        rep.add("layout.end", "разделы внутри образа", root["offset"] + root["size"] <= size)
    packages_tsv = None
    uuid = f"{mbr['disksig']:08x}"
    with tempfile.TemporaryDirectory() as tmp:
        if root and shutil.which("debugfs"):
            r = Ext4(img, root, tmp)
            fstab = r.cat("/etc/fstab")
            rep.add("fstab.root", "fstab: корень по PARTUUID этого диска", f"PARTUUID={uuid}-02" in fstab
                    and re.search(r"PARTUUID=\S+\s+/\s+ext4", fstab) is not None, fstab.strip().replace("\n", " | ")[:120])
            rep.add("fstab.boot", "fstab: /boot/firmware по PARTUUID этого диска", f"PARTUUID={uuid}-01" in fstab)
            rep.add("hostname", f"имя {HOSTNAME}", r.cat("/etc/hostname").strip() == HOSTNAME, r.cat("/etc/hostname").strip())
            rep.add("marker", "метка /etc/sheregesh/BUILDHOST-MODE", r.exists("/etc/sheregesh/BUILDHOST-MODE"))
            sshd = r.cat("/etc/ssh/sshd_config.d/00-sheregesh-buildhost.conf")
            rep.add("sshd.keyonly", "sshd: только ключи, root — prohibit-password",
                    all(x in sshd for x in ("PermitRootLogin prohibit-password", "PasswordAuthentication no",
                                            "AuthenticationMethods publickey")))
            ak = [x.split()[:2] for x in r.cat(f"/home/{USER}/.ssh/authorized_keys").splitlines() if x.strip()]
            rk = [x.split()[:2] for x in r.cat("/root/.ssh/authorized_keys").splitlines() if x.strip()]
            rep.add("ssh.key", f"authorized_keys {USER} задан, ключ root тот же", bool(ak) and ak == rk,
                    ak[0][0] if ak else "нет ключа")
            rep.add("ssh.nokeys", "приватных хост-ключей SSH в образе нет (создаются при первой загрузке)",
                    not [n for n in r.ls("/etc/ssh") if n.startswith("ssh_host_") and not n.endswith(".pub")])
            shadow = {x.split(":")[0]: x.split(":")[1] for x in r.cat("/etc/shadow").splitlines() if ":" in x}
            for u in ("root", USER):
                h = shadow.get(u)
                rep.add(f"passwd.{u}", f"{u}: пароль не задан (вход только по ключу)", h is not None and (h == "" or h[0] in "!*"),
                        "нет записи" if h is None else h[:1] or "пусто")
            sudoers = r.cat("/etc/sudoers.d/90-sheregesh-buildhost")
            rep.add("sudo", f"sudo без пароля для {USER}", f"{USER} ALL=(ALL) NOPASSWD: ALL" in sudoers)
            groups = r.cat("/etc/group")
            docker_members = next((x.split(":")[3].split(",") for x in groups.splitlines() if x.startswith("docker:")), [])
            rep.add("docker.group", f"{USER} в группе docker", USER in docker_members, ",".join(docker_members))
            wants = r.ls("/etc/systemd/system/multi-user.target.wants")
            rep.add("unit.growroot", "shbuild-growroot включён", "shbuild-growroot.service" in wants)
            rep.add("unit.docker", "docker включён", "docker.service" in wants)
            rep.add("unit.eeprom-masked", "rpi-eeprom-update замаскирован (EEPROM — явная процедура)",
                    "/dev/null" in r.run("stat /etc/systemd/system/rpi-eeprom-update.service"))
            rep.add("growroot.script", "скрипт расширения корня исполняемый",
                    "Mode:  0755" in r.run("stat /usr/local/sbin/shbuild-growroot"))
            missing = [t for t in TOOLS if not r.exists("/" + t)]
            rep.add("tools", "docker, git, zstd, rsync, python3, growpart, resize2fs, nvme, tmux", not missing,
                    "нет: " + " ".join(missing) if missing else "")
            rep.add("watchdog", "аппаратный сторож systemd (RuntimeWatchdogSec)",
                    "RuntimeWatchdogSec=30s" in r.cat("/etc/systemd/system.conf.d/10-sheregesh-watchdog.conf"))
            rep.add("journal", "журнал на диске (Storage=persistent)",
                    "Storage=persistent" in r.cat("/etc/systemd/journald.conf.d/10-sheregesh-persistent.conf"))
            packages_tsv = packages_from_status(r.cat("/var/lib/dpkg/status"))
            rep.add("packages", "список пакетов образа получен", packages_tsv.count("\n") > 100, f"{packages_tsv.count(chr(10))} пакетов")
        else:
            rep.add("root.content", "содержимое ROOT", None, "нет debugfs (e2fsprogs)")
        if boot and shutil.which("mtype"):
            cfg = mtype(img, boot, "config.txt")
            cmd = mtype(img, boot, "cmdline.txt")
            rep.add("boot.config", "config.txt: PCIe x1 для NVMe включён в разделе [pi5]",
                    cfg is not None and "dtparam=pciex1" in config_lines(cfg), "нет config.txt" if cfg is None else "")
            rep.add("boot.cmdline", "cmdline.txt: корень по PARTUUID этого диска, rootwait",
                    cmd is not None and f"root=PARTUUID={uuid}-02" in cmd and "rootwait" in cmd, (cmd or "нет").strip()[:160])
            rep.add("boot.kernel", "ядро Pi 5 (kernel_2712.img) в BOOT", mexists(img, boot, "kernel_2712.img"))
            rep.add("boot.dtb", "дерево устройств bcm2712-rpi-5-b.dtb в BOOT", mexists(img, boot, "bcm2712-rpi-5-b.dtb"))
        else:
            rep.add("boot.content", "содержимое BOOT", None, "нет mtools")

    name = Path(img).name.removesuffix(".img")
    out.mkdir(parents=True, exist_ok=True)
    if packages_tsv:
        (out / f"{name}.packages.tsv").write_text(packages_tsv)
    counts = {s: sum(c["status"] == s for c in rep.checks) for s in ("PASS", "FAIL", "SKIP")}
    result = {"image": Path(img).name, "variant": variant, "bytes": size, "partitions": parts, "checks": rep.checks,
              "counts": counts, "verdict": "FAIL" if counts["FAIL"] else ("PASS" if not counts["SKIP"] else "PARTIAL"),
              "not_tested": ["загрузка на Pi 5", "прошивка EEPROM и порядок загрузки NVMe", "PCIe/NVMe на HAT",
                             "температура и скорость сборки"]}
    (out / "verify.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps(counts, ensure_ascii=False), result["verdict"])
    return 1 if counts["FAIL"] else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image")
    ap.add_argument("--variant", required=True)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    return verify(a.image, a.variant, a.out)


if __name__ == "__main__":
    sys.exit(main())
