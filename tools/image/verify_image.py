# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Автоматическая проверка структуры образа Pi 4 (S2-1) против ADR-0002/0006 и варианта сборки.

  python3 tools/image/verify_image.py IMAGE.img --variant sheregesh-lab-pi4|sheregesh-lab-dev-pi4 --out DIR

Структура (стандартная библиотека): защитный MBR, заголовок и записи GPT с CRC, шесть разделов ADR-0006
в порядке autoboot | boot-A | boot-B | root-A | root-B | data, типы, размеры, выравнивание, сигнатуры и метки FAT/ext4,
побайтовая идентичность слотов A и B.
Содержимое (если доступны debugfs из e2fsprogs и mtools): корень только для чтения, службы, учётные записи
заблокированы, SSH по варианту, хост-ключей SSH в образе нет, Wi-Fi/Bluetooth выключены, tryboot, манифест пакетов.
Нет инструмента — проверка SKIP с причиной (не PASS). Итог — DIR/verify.json и DIR/<имя>.packages.tsv; код 1 при FAIL.

Проверка образа не заменяет загрузку на Pi: tryboot, A/B-откат, дисплей и производительность — NOT TESTED.
"""
import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import zlib
from pathlib import Path

MiB = 1024 * 1024
SECTOR = 512
FAT_TYPE = uuid.UUID("EBD0A0A2-B9E5-4433-87C0-68B6B72699C7")      # Microsoft basic data (FAT)
LINUX_TYPE = uuid.UUID("0FC63DAF-8483-4772-8E79-3D69D8477DE4")    # Linux filesystem
# ADR-0006 и os/config/sheregesh-lab-pi4.yaml (совпадение проверяет tests/tools/test_image_tools.py).
LAYOUT = [  # (имя GPT, ФС, размер в байтах или None — не меньше MIN_DATA, метка ФС, назначение по ADR-0006)
    ("bootconfig", "fat", 32 * MiB, "BOOTCONFIG", "autoboot: autoboot.txt, выбор слота tryboot"),
    ("boot_a", "fat", 256 * MiB, "BOOT", "boot-A: ядро и прошивка слота A"),
    ("boot_b", "fat", 256 * MiB, "BOOT", "boot-B: ядро и прошивка слота B"),
    ("system_a", "ext4", 2560 * MiB, "SYSTEM", "root-A: корневая ФС слота A, только чтение"),
    ("system_b", "ext4", 2560 * MiB, "SYSTEM", "root-B: корневая ФС слота B, только чтение"),
    ("persistent", "ext4", None, "PERSISTENT", "data: конфигурация, журналы, /home, /var слотов"),
]
MIN_DATA = 1024 * MiB
ALIGN = 8 * MiB
UNITS = ("seatd", "sheregesh-remote-emu", "sheregesh-console", "sheregesh-remote-net")


class Report:
    def __init__(self):
        self.checks = []

    def add(self, cid, desc, ok, detail=""):
        status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        self.checks.append({"id": cid, "desc": desc, "status": status, "detail": detail})
        print(f"[{status}] {cid}: {desc}" + (f" — {detail}" if detail else ""))
        return ok


def read_gpt(f):
    """Защитный MBR, основной заголовок GPT и записи; проверка CRC32."""
    f.seek(0)
    mbr = f.read(SECTOR)
    protective = mbr[510:512] == b"\x55\xaa" and mbr[446 + 4] == 0xEE
    f.seek(SECTOR)
    hdr = f.read(92)
    sig, rev, hsize, hcrc, _, cur, backup, first, last, disk_guid, ent_lba, n, esize, ecrc = \
        struct.unpack("<8sIIIIQQQQ16sQIII", hdr)
    if sig != b"EFI PART":
        raise ValueError("нет заголовка GPT")
    hdr_ok = zlib.crc32(hdr[:16] + b"\0\0\0\0" + hdr[20:hsize]) & 0xFFFFFFFF == hcrc
    f.seek(ent_lba * SECTOR)
    raw = f.read(n * esize)
    ent_ok = zlib.crc32(raw) & 0xFFFFFFFF == ecrc
    parts = []
    for i in range(n):
        e = raw[i * esize:(i + 1) * esize]
        ptype = uuid.UUID(bytes_le=e[:16])
        if ptype.int == 0:
            continue
        start, end = struct.unpack("<QQ", e[32:48])
        name = e[56:128].decode("utf-16-le").rstrip("\0")
        parts.append({"index": i + 1, "name": name, "type": str(ptype).upper(), "guid": str(uuid.UUID(bytes_le=e[16:32])),
                      "offset": start * SECTOR, "size": (end - start + 1) * SECTOR})
    return {"protective_mbr": protective, "header_crc_ok": hdr_ok, "entries_crc_ok": ent_ok,
            "backup_lba": backup, "partitions": parts}


def fs_info(f, offset):
    """Тип ФС и метка по сигнатурам: ext2/3/4 — магия 0xEF53 суперблока; FAT — 0x55AA и строка типа."""
    f.seek(offset + 1024)
    sb = f.read(1024)
    if len(sb) == 1024 and struct.unpack_from("<H", sb, 56)[0] == 0xEF53:
        incompat = struct.unpack_from("<I", sb, 96)[0]
        kind = "ext4" if incompat & 0x40 else "ext2/3"           # INCOMPAT_EXTENTS
        return kind, sb[120:136].rstrip(b"\0").decode(errors="replace")
    f.seek(offset)
    bs = f.read(SECTOR)
    if bs[510:512] == b"\x55\xaa":
        if bs[82:90].startswith(b"FAT32"):
            return "fat", bs[71:82].decode(errors="replace").strip()
        if bs[54:59] in (b"FAT16", b"FAT12"):
            return "fat", bs[43:54].decode(errors="replace").strip()
    return "unknown", ""


def sha256_range(path, offset, size):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        f.seek(offset)
        left = size
        while left:
            chunk = f.read(min(left, 8 * MiB))
            if not chunk:
                break
            h.update(chunk)
            left -= len(chunk)
    return h.hexdigest()


class Ext4:
    """Чтение файлов ext4 из раздела образа через debugfs (без монтирования и без root)."""

    def __init__(self, img, part, tmp):
        self.path = Path(tmp) / f"{part['name']}.ext4"
        with open(img, "rb") as src, open(self.path, "wb") as dst:
            src.seek(part["offset"])
            left = part["size"]
            while left:
                chunk = src.read(min(left, 8 * MiB))
                dst.write(chunk)
                left -= len(chunk)

    def run(self, cmd):
        r = subprocess.run(["debugfs", "-R", cmd, str(self.path)], capture_output=True, text=True)
        return r.stdout

    def cat(self, path):
        return self.run(f"cat {path}")

    def exists(self, path):
        r = subprocess.run(["debugfs", "-R", f"stat {path}", str(self.path)], capture_output=True, text=True)
        return "Inode:" in r.stdout

    def ls(self, path):
        return [x for x in self.run(f"ls -p {path}").splitlines() if x.startswith("/")]


def mtype(img, part, name):
    r = subprocess.run(["mtype", "-i", f"{img}@@{part['offset']}", f"::{name}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def verify(img, variant, out):
    rep = Report()
    dev = "-dev-" in variant
    with open(img, "rb") as f:
        gpt = read_gpt(f)
        rep.add("gpt.mbr", "защитный MBR (тип 0xEE)", gpt["protective_mbr"])
        rep.add("gpt.crc", "CRC заголовка и записей GPT", gpt["header_crc_ok"] and gpt["entries_crc_ok"])
        parts = gpt["partitions"]
        names = [p["name"] for p in parts]
        rep.add("layout.order", "шесть разделов ADR-0006 в порядке autoboot|boot-A|boot-B|root-A|root-B|data",
                names == [x[0] for x in LAYOUT], " ".join(names))
        for p, (name, fs, size, label, purpose) in zip(parts, LAYOUT):
            p["purpose"], p["fs"], p["label"] = purpose, *fs_info(f, p["offset"])
            want_type = str(FAT_TYPE if fs == "fat" else LINUX_TYPE).upper()
            rep.add(f"part.{name}.type", f"{name}: тип раздела {fs}", p["type"] == want_type, p["type"])
            rep.add(f"part.{name}.fs", f"{name}: ФС {fs}, метка {label}", p["fs"] == fs and p["label"] == label,
                    f"{p['fs']} «{p['label']}»")
            ok = p["size"] == size if size else p["size"] >= MIN_DATA
            rep.add(f"part.{name}.size", f"{name}: размер {'≥ 1 ГиБ' if not size else f'{size // MiB} МиБ'}", ok,
                    f"{p['size'] // MiB} МиБ")
            rep.add(f"part.{name}.align", f"{name}: выравнивание 8 МиБ", p["offset"] % ALIGN == 0, f"{p['offset']}")
    size = os.path.getsize(img)
    rep.add("gpt.backup", "резервный заголовок GPT в последнем секторе", gpt["backup_lba"] == size // SECTOR - 1)
    by = {p["name"]: p for p in parts}
    if {"boot_a", "boot_b", "system_a", "system_b"} <= by.keys():
        for a, b in (("boot_a", "boot_b"), ("system_a", "system_b")):
            ha, hb = (sha256_range(img, by[x]["offset"], by[x]["size"]) for x in (a, b))
            by[a]["sha256"], by[b]["sha256"] = ha, hb
            rep.add(f"slots.{a[:-2]}", f"{a} и {b} побайтово совпадают (image-rota)", ha == hb, ha[:16])

    packages_tsv = None
    with tempfile.TemporaryDirectory() as tmp:
        if shutil.which("debugfs") and "system_a" in by:
            root = Ext4(img, by["system_a"], tmp)
            data = Ext4(img, by["persistent"], tmp)       # image-rota: /home и /var слотов — на разделе данных
            fstab = root.cat("/etc/fstab")
            rootline = next((x.split() for x in fstab.splitlines() if len(x.split()) >= 4 and x.split()[1] == "/"), [])
            rep.add("root.ro", "корень монтируется только для чтения (/etc/fstab)", bool(rootline) and
                    "ro" in rootline[3].split(","), " ".join(rootline))
            rep.add("root.data", "/home и журнал — на разделе данных", "/persistent/home" in fstab and
                    "/persistent/log/journal" in fstab)
            for path in ("/usr/bin/sheregesh-console", "/usr/bin/sheregesh-remote-emu",
                         "/usr/lib/sheregesh/software/sheregesh/video.py",
                         "/usr/lib/sheregesh/software/console/sheregesh_console/i18n/sheregesh_ru.qm"):
                rep.add(f"file.{Path(path).name}", f"есть {path}", root.exists(path))
            wants = root.ls("/etc/systemd/system/multi-user.target.wants")
            for u in UNITS:
                rep.add(f"unit.{u}", f"служба {u} включена", f"{u}.service" in " ".join(wants))
            emu = root.cat("/usr/lib/systemd/system/sheregesh-remote-emu.service")
            rep.add("emu.loopback", "эмулятор: только 127.0.0.1 (--host и IPAddressDeny/Allow)",
                    "--host 127.0.0.1" in emu and "IPAddressDeny=any" in emu and "IPAddressAllow=localhost" in emu)
            for u in ("sheregesh-console", "sheregesh-remote-emu", "sheregesh-remote-net"):
                text = root.cat(f"/usr/lib/systemd/system/{u}.service")
                rep.add(f"unit.{u}.notify", f"{u}: Type=notify, WatchdogSec, ограничение перезапусков (ADR-0002)",
                        "Type=notify" in text and "WatchdogSec=" in text and "StartLimitBurst=" in text)
            net = root.cat("/usr/lib/systemd/system/sheregesh-remote-net.service")
            rep.add("net.conditions", "sheregesh-remote-net не стартует без роли, ключа, сертификата и CA",
                    all(f"ConditionPathExists=/home/sheregesh-ui/{p}" in net for p in
                        (".config/sheregesh/remote.role", ".local/share/sheregesh/link/device.key",
                         ".local/share/sheregesh/link/ca.crt")) and "IPAddressDeny=any" in net)
            rep.add("emu.cache", "эмулятор: каталог кэша GStreamer и HOME (CacheDirectory)",
                    "CacheDirectory=sheregesh-remote-emu" in emu and "Environment=HOME=%C/sheregesh-remote-emu" in emu)
            nss = next((x for x in root.cat("/etc/nsswitch.conf").splitlines() if x.startswith("passwd:")), "")
            rep.add("nss.systemd", "NSS: uid DynamicUser разрешается (systemd в passwd:)", "systemd" in nss.split(), nss)
            rep.add("unit.eeprom-masked", "rpi-eeprom-update замаскирован (обновление EEPROM — явная процедура)",
                    "/dev/null" in root.run("stat /etc/systemd/system/rpi-eeprom-update.service"))
            rep.add("resolved.no-llmnr", "systemd-resolved: LLMNR и mDNS выключены (нет 0.0.0.0:5355)",
                    "LLMNR=no" in (rc := root.cat("/etc/systemd/resolved.conf.d/90-sheregesh.conf")) and "MulticastDNS=no" in rc)
            rep.add("console-setup.ro", "console-setup не пишет в ro-корень (кэш собран или служба замаскирована)",
                    any("cached_" in x for x in root.ls("/etc/console-setup")) or
                    "/dev/null" in root.run("stat /etc/systemd/system/console-setup.service"))
            shadow = {x.split(":")[0]: x.split(":")[1] for x in root.cat("/etc/shadow").splitlines() if x.count(":") >= 2}
            locked = lambda h: h.startswith(("!", "*"))                                    # noqa: E731
            rep.add("users.locked", "root и sheregesh-ui без пароля (заблокированы)",
                    locked(shadow.get("root", "")) and locked(shadow.get("sheregesh-ui", "x")),
                    f"root={shadow.get('root', '?')[:1]} ui={shadow.get('sheregesh-ui', '?')[:1]}")
            groups = {x.split(":")[0]: x.split(":")[3].split(",") for x in root.cat("/etc/group").splitlines()
                      if x.count(":") == 3}
            rules = [x for x in (root.cat("/etc/sudoers") + "".join(root.cat(f) for f in root.ls("/etc/sudoers.d")
                     if not f.endswith(("/.", "/..", "README")))).splitlines()
                     if x.strip() and not x.lstrip().startswith(("#", "Defaults", "@include"))]
            admin = [g for g in ("sudo", "admin", "wheel") if "sheregesh-ui" in groups.get(g, [])]
            rep.add("users.nosudo", "у sheregesh-ui нет прав sudo (не в sudo/admin/wheel, правил для него нет)",
                    not admin and all(r.split()[0] in ("root", "%sudo", "%admin") for r in rules),
                    "; ".join(rules) + (f" группы: {admin}" if admin else ""))
            sshd = root.exists("/usr/sbin/sshd")
            hostkeys = [x for x in root.ls("/etc/ssh") if "ssh_host_" in x]
            rep.add("ssh.hostkeys", "хост-ключей SSH в образе нет (генерируются на устройстве)", not hostkeys,
                    " ".join(hostkeys))
            if dev:
                conf = root.cat("/etc/ssh/sshd_config.d/00-sheregesh-dev.conf")
                rep.add("ssh.dev", "dev: sshd только по ключу, без root", sshd and "PasswordAuthentication no" in conf
                        and "PermitRootLogin no" in conf)
                keys = data.cat("/home/sheregesh-ui/.ssh/authorized_keys")
                rep.add("ssh.key", "dev: открытый ключ sheregesh-ui установлен, закрытых ключей нет",
                        keys.startswith(("ssh-", "ecdsa-")) and "PRIVATE KEY" not in keys, keys.split(" ")[0])
                rep.add("net.dev", "dev: DHCP на eth0", "DHCP=yes" in root.cat("/etc/systemd/network/20-sheregesh-dev.network"))
            else:
                rep.add("ssh.absent", "строгий вариант: sshd не установлен (ADR-0002)", not sshd)
                rep.add("net.lab", "статический адрес стенда 192.168.77.1/24 без шлюза (ADR-0007)",
                        "Address=192.168.77.1/24" in (n := root.cat("/etc/systemd/network/20-sheregesh-lab.network"))
                        and "Gateway" not in n)
            packages_tsv = root.cat("/usr/share/sheregesh/image-packages.tsv")
            status = data.cat("/slots/system_a/var/lib/dpkg/status")
            # Установлен — если последнее слово Status «installed» (в т. ч. «purge ok installed» после неудачного purge).
            installed = sorted(b.split("\n")[0][9:] for b in status.split("\n\n") if b.startswith("Package: ") and
                               any(x.startswith("Status: ") and x.split()[-1] == "installed" for x in b.splitlines()))
            listed = sorted(x.split("\t")[0].split(":")[0] for x in packages_tsv.splitlines() if x)
            rep.add("manifest.complete", "манифест пакетов совпадает с dpkg status образа",
                    bool(listed) and listed == installed, f"{len(listed)} в манифесте, {len(installed)} установлено")
            nohash = [x.split("\t")[0] for x in packages_tsv.splitlines() if x.endswith("\tlocal")]
            rep.add("manifest.sha256", "SHA-256 из индекса репозитория у всех пакетов, кроме собственного",
                    bool(listed) and nohash == ["sheregesh"], " ".join(nohash))
            sheregesh = next((x for x in packages_tsv.splitlines() if x.startswith("sheregesh\t")), "")
            rep.add("pkg.sheregesh", "пакет sheregesh установлен", bool(sheregesh), sheregesh.replace("\t", " "))
        else:
            rep.add("root.content", "содержимое корневой ФС", None, "нет debugfs (e2fsprogs)")
        if shutil.which("mtype") and "boot_a" in by:
            cfg = mtype(img, by["boot_a"], "config.txt")
            if cfg is not None and "boot_ramdisk" in cfg:                 # image-rota: прошивка внутри boot.img
                inner = Path(tmp) / "boot.img"
                subprocess.run(["mcopy", "-n", "-i", f"{img}@@{by['boot_a']['offset']}", "::boot.img", str(inner)],
                               capture_output=True)
                r = subprocess.run(["mtype", "-i", str(inner), "::config.txt"], capture_output=True, text=True)
                cfg = cfg + "\n" + r.stdout
            rep.add("boot.config", "config.txt: dtoverlay=disable-wifi и disable-bt (ADR-0002)",
                    cfg is not None and "dtoverlay=disable-wifi" in cfg and "dtoverlay=disable-bt" in cfg)
            auto = mtype(img, by["bootconfig"], "autoboot.txt") if "bootconfig" in by else None
            rep.add("boot.tryboot", "autoboot.txt: tryboot_a_b=1 (ADR-0006)", auto is not None and "tryboot_a_b=1" in auto,
                    (auto or "").strip().replace("\n", " "))
        else:
            rep.add("boot.content", "содержимое загрузочных разделов", None, "нет mtools")

    name = Path(img).name.removesuffix(".img")
    out.mkdir(parents=True, exist_ok=True)
    if packages_tsv:
        (out / f"{name}.packages.tsv").write_text(packages_tsv)
    counts = {s: sum(c["status"] == s for c in rep.checks) for s in ("PASS", "FAIL", "SKIP")}
    result = {"image": Path(img).name, "variant": variant, "bytes": size, "partitions": parts, "checks": rep.checks,
              "counts": counts, "verdict": "FAIL" if counts["FAIL"] else ("PASS" if not counts["SKIP"] else "PARTIAL"),
              "not_tested": ["загрузка на Pi 4", "tryboot и A/B-откат (S2-5)", "дисплей (S2-3/S2-4)",
                             "производительность (S2-4)", "время загрузки (S2-2)"]}
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
