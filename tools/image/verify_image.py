# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Автоматическая проверка структуры образа Pi 4 (S2-1) против ADR-0002/0006 и варианта сборки.

  python3 tools/image/verify_image.py IMAGE.img --variant sheregesh-lab-pi4|sheregesh-lab-dev-pi4 --out DIR

Структура (стандартная библиотека): защитный MBR, заголовок и записи GPT с CRC, шесть разделов ADR-0006
в порядке autoboot | boot-A | boot-B | root-A | root-B | data, типы, размеры, выравнивание, сигнатуры и метки FAT/ext4,
побайтовая идентичность слотов A и B.
Содержимое (если доступны debugfs из e2fsprogs и mtools): корень только для чтения, службы, учётные записи
заблокированы, SSH по варианту, хост-ключей SSH в образе нет, консоль UART на GPIO, tryboot, манифест пакетов,
обновления без перестановки microSD (S3.1-DEPLOY: launcher, установщик, агент OTA, ключ подписи, polkit).
Нет инструмента — проверка SKIP с причиной (не PASS). Итог — DIR/verify.json и DIR/<имя>.packages.tsv; код 1 при FAIL.

Проверка образа не заменяет загрузку на Pi: tryboot, A/B-откат, дисплей и производительность — NOT TESTED.
"""
import argparse
import hashlib
import json
import os
import re
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

    def readlink(self, path):
        """Цель символьной ссылки (debugfs stat: «Fast link dest: "…"»); None — не ссылка или нет файла."""
        m = re.search(r'^Fast link dest: "(.*)"$', self.run(f"stat {path}"), re.M)
        return m.group(1) if m else None

    def mtime(self, path):
        """mtime (секунды) по debugfs stat: «mtime: 0x6a1b2c3d:…» или «mtime: 0x6a1b2c3d -- …»; None — нет файла."""
        m = re.search(r"^\s*mtime:\s*0x([0-9a-f]+)", self.run(f"stat {path}"), re.M)
        return int(m.group(1), 16) if m else None


CONSOLE_CONFIGS = ("/etc/default/keyboard", "/etc/default/console-setup")


def console_setup_stale(mtimes):
    """Кэш console-setup, который служба при загрузке сочтёт устаревшим и станет пересоздавать в ro-корне.
    Решение принимает /lib/console-setup/console-setup.sh: setupcon --save вызывается, если cached_setup_terminal.sh
    НЕ строго новее (-nt) /etc/default/keyboard или /etc/default/console-setup. Равные mtime (mke2fs ≥ 1.47.1 зажимает
    время к SOURCE_DATE_EPOCH) — устаревший кэш. (Проверки setupcon по cached_*.kmap.gz срабатывают только внутри
    setupcon; в образе CI 57e8fe4 kmap того же времени, что конфиг, и это не мешает.)"""
    conf = {p: mtimes.get(p) for p in CONSOLE_CONFIGS}
    caches = [p for p in mtimes if p == "/etc/console-setup/cached_setup_terminal.sh" and mtimes[p] is not None]
    if not caches:
        return ["нет cached_setup_terminal.sh"]
    return [f"{c.rsplit('/', 1)[1]} не новее {p.rsplit('/', 1)[1]} ({mtimes[c]} ≤ {t})"
            for c in sorted(caches) for p, t in conf.items() if t is not None and mtimes[c] <= t]


def mtype(img, part, name):
    r = subprocess.run(["mtype", "-i", f"{img}@@{part['offset']}", f"::{name}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


LAUNCHER = "/usr/lib/sheregesh/software/sheregesh/deploy/launcher.py"
DEPLOY_UNITS = ("sheregesh-deploy@.service", "sheregesh-ota.socket", "sheregesh-ota@.service", "sheregesh-ota-boot.service")
SIGNERS = "/etc/sheregesh/deploy/allowed_signers"


def ls_names(root, directory):
    """Имена в каталоге: строки debugfs «ls -p» (/инод/режим/uid/gid/имя/размер/) или обычные пути."""
    names = []
    for line in root.ls(directory):
        parts = line.split("/")
        names.append(parts[5] if len(parts) >= 7 and parts[1].isdigit() else Path(line).name)
    return [n for n in names if n not in ("", ".", "..")]


# Юниты systemd (trixie), упорядоченные до local-fs-pre.target. Юнит с After=local-fs.target (или RequiresMountsFor
# на смонтированный в local-fs путь) не может стоять Before= ни одного из них — это цикл упорядочения.
EARLY_UNITS = ("local-fs-pre.target", "systemd-sysusers.service", "systemd-tmpfiles-setup-dev.service",
               "systemd-tmpfiles-setup-dev-early.service", "systemd-udevd.service", "systemd-remount-fs.service",
               "systemd-fsck-root.service")


def boot_order_violations(unit_text):
    """Ранние юниты из Before= юнита, который идёт после local-fs.target (After= или RequiresMountsFor=)."""
    keys = {}
    for line in unit_text.splitlines():
        k, sep, v = line.strip().partition("=")
        if sep:
            keys.setdefault(k, []).extend(v.split())
    late = "local-fs.target" in keys.get("After", []) or bool(keys.get("RequiresMountsFor"))
    return [u for u in keys.get("Before", []) if late and u in EARLY_UNITS]


LAB_MANIFEST = Path(__file__).resolve().parents[2] / "tools/lab-video/pi2/manifest.tsv"
LAB_UNITS = {"sheregesh-lab-vivid.service", "sheregesh-lab-vivid-feed@cvbs.service",
             "sheregesh-lab-vivid-feed@hdmi.service", "sheregesh-lab-usbip@hdmi.service",
             "sheregesh-lab-usbip@cvbs.service", "sheregesh-lab-usbip@camera.service"}


def lab_paths(manifest=None):
    rows = [ln.split("\t") for ln in (manifest or LAB_MANIFEST).read_text().splitlines() if ln and not ln.startswith("#")]
    return [dest for _src, dest, _mode in rows]


def labvideo_checks(rep, root, dev, manifest=None):
    """S5 п. 7 (tools/lab-video/pi2): эмулированные источники видео блока (vivid, USB/IP shcam) — только в dev-образе;
    в строгом нет ни файлов, ни правила polkit. Модули ядра vivid/vhci-hcd есть в обоих: загрузить их некому."""
    paths = lab_paths(manifest)
    present = [p for p in paths if root.exists(p)]
    polkit = " ".join(root.cat(f"/etc/polkit-1/rules.d/{n}") for n in ls_names(root, "/etc/polkit-1/rules.d"))
    if not dev:
        rep.add("labvideo.absent", "строгий вариант: лабораторных источников видео (п. 7) нет — файлы и polkit",
                not present and "sheregesh-lab-" not in polkit, ", ".join(present) or f"0 из {len(paths)}")
        return
    rep.add("labvideo.files", "dev: файлы п. 7 на месте (manifest.tsv)", len(present) == len(paths),
            ", ".join(sorted(set(paths) - set(present))) or f"{len(paths)} файлов")
    rules = root.cat("/etc/polkit-1/rules.d/51-sheregesh-lab-video.rules")
    units = set(re.findall(r'"(sheregesh-lab-[a-z-]+(?:@[a-z]+)?\.service)"', rules))
    rep.add("labvideo.polkit", "dev: polkit п. 7 — только start/stop/restart шести юнитов у sheregesh-ui",
            units == LAB_UNITS and 'subject.user != "sheregesh-ui"' in rules
            and set(re.findall(r'verb == "([a-z-]+)"', rules)) == {"start", "stop", "restart"},
            ", ".join(sorted(units)))
    texts = {p: root.cat(p) for p in paths if p.startswith("/usr/lib/systemd/system/")}
    wants = " ".join(n for d in ls_names(root, "/etc/systemd/system") if d.endswith(".wants")
                     for n in ls_names(root, f"/etc/systemd/system/{d}"))
    rep.add("labvideo.units", "dev: юниты п. 7 только при DEV-MODE и не включены (запуск по polkit)",
            bool(texts) and all("ConditionPathExists=/etc/sheregesh/DEV-MODE" in t for t in texts.values())
            and "sheregesh-lab-" not in wants, ", ".join(p for p, t in texts.items() if "DEV-MODE" not in t) or "ok")


def deploy_checks(rep, root, data, dev, groups):
    """S3.1-DEPLOY: обновления без перестановки microSD — launcher, установщик, агент OTA, ключ подписи, polkit."""
    wrappers = {w: root.cat(f"/usr/bin/{w}") for w in ("sheregesh-console", "sheregesh-remote-emu")}
    rep.add("deploy.launcher", "службы приложения стартуют через launcher (выпуск DEV deploy или /usr)",
            root.exists(LAUNCHER) and all(f"python3 -I {LAUNCHER} " in t for t in wrappers.values()),
            "; ".join(t.strip().splitlines()[-1][:90] for t in wrappers.values() if t.strip()))
    cmds = {n: root.cat(f"/usr/bin/{n}") for n in ls_names(root, "/usr/bin") if n.startswith("sheregesh-")}
    leaky = sorted(n for n, t in cmds.items() if "python3 -s " not in t and "python3 -I " not in t)
    rep.add("deploy.nousersite", "команды sheregesh-* не читают пользовательские site-packages (python3 -s/-I)",
            bool(cmds) and not leaky, ", ".join(leaky) or f"{len(cmds)} команд")
    units = {u: root.cat(f"/usr/lib/systemd/system/{u}") for u in DEPLOY_UNITS}
    rep.add("deploy.units", "юниты установщика и агента OTA есть и требуют ключа подписи (ConditionPathExists)",
            all(units.values()) and all(f"ConditionPathExists={SIGNERS}" in units[u] for u in
                                        ("sheregesh-deploy@.service", "sheregesh-ota.socket", "sheregesh-ota-boot.service"))
            and root.exists("/usr/bin/sheregesh-deployd") and root.exists("/usr/bin/sheregesh-ota-agent"),
            ", ".join(u for u, t in units.items() if not t) or "все на месте")
    sock = units["sheregesh-ota.socket"]
    rep.add("deploy.socket", "сокет агента OTA: 0660 root:sheregesh-ui, только локальный (без сетевого Listen)",
            "SocketMode=0660" in sock and "SocketGroup=sheregesh-ui" in sock and "ListenStream=/run/" in sock
            and "sheregesh-ui" in groups)
    wants = " ".join(root.ls("/etc/systemd/system/sockets.target.wants") +
                     root.ls("/etc/systemd/system/multi-user.target.wants"))
    rep.add("deploy.enabled", "включены sheregesh-ota.socket и sheregesh-ota-boot (учёт загрузки без переключений)",
            "sheregesh-ota.socket" in wants and "sheregesh-ota-boot.service" in wants)
    seed_unit = root.cat("/usr/lib/systemd/system/sheregesh-var-seed.service")
    sysinit = " ".join(root.ls("/etc/systemd/system/sysinit.target.wants"))
    rep.add("deploy.varseed", "образец /var на системном разделе и служба sheregesh-var-seed (S3.2-OTA: /var слота "
            "после A/B)", root.exists("/usr/share/sheregesh/var-seed.tar.gz") and "--skip-old-files" in seed_unit
            and "sheregesh-var-seed.service" in sysinit)
    early = boot_order_violations(seed_unit)
    rep.add("boot.order", "sheregesh-var-seed не упорядочена раньше юнитов, идущих до local-fs-pre.target "
            "(цикл упорядочения → systemd удаляет случайное задание, OTA CLOSURE цикл 2)",
            bool(seed_unit) and not early, ", ".join(early) or "циклов через ранние юниты нет")
    build = root.cat("/usr/share/sheregesh/build.json")
    rep.add("deploy.build", "версия системной части для OTA (/usr/share/sheregesh/build.json)",
            '"ota_version"' in build, build.replace("\n", " ")[:120])
    signers = root.cat(SIGNERS)
    if dev:
        keys = data.cat("/home/sheregesh-ui/.ssh/authorized_keys").split()
        same = len(keys) >= 2 and keys[1] in signers
        rep.add("deploy.signers", "dev: отдельный открытый ключ подписи выпусков (не ключ входа), закрытых ключей нет",
                signers.startswith("sheregesh-deploy namespaces=") and "PRIVATE KEY" not in signers and not same,
                ("ключ подписи совпадает с ключом входа — отказ" if same else "отдельный ключ подписи"))
        rules = root.cat("/etc/polkit-1/rules.d/50-sheregesh-dev.rules")
        deploy_units = set(re.findall(r'"(sheregesh-deploy@[a-z]+\.service)"', rules))
        rep.add("deploy.polkit", "polkit: sheregesh-ui — только «start» install/rollback/factory установщика",
                deploy_units == {f"sheregesh-deploy@{a}.service" for a in ("install", "rollback", "factory")}
                and 'action.lookup("verb") == "start"' in rules and "sheregesh-ota" not in rules,
                ", ".join(sorted(deploy_units)))
    else:
        rep.add("deploy.absent", "строгий вариант: ключа подписи нет — DEV deploy и агент OTA не работают",
                not root.exists(SIGNERS))


VIDEO_UNITS = ("sheregesh-video-tx.service", "sheregesh-video-rx.service", "sheregesh-video-nft.service",
               "sheregesh-video-nft-remote.service", "sheregesh-video-allow@.service")
VIDEO_NFT = ("sheregesh-video-console.nft", "sheregesh-video-remote.nft")
VIDEO_POLKIT = "/etc/polkit-1/rules.d/52-sheregesh-video-allow.rules"


def video_checks(rep, root, packages_tsv):
    """ADR-0016: видеослужбы, таблицы nftables, помощник адреса, правило polkit обычного образа, пакет nftables."""
    units = {u: root.cat(f"/usr/lib/systemd/system/{u}") for u in VIDEO_UNITS}
    tx, rx = units["sheregesh-video-tx.service"], units["sheregesh-video-rx.service"]
    rep.add("video.launcher", "видеослужбы запускают выбранный DEV-выпуск через launcher",
            root.exists(LAUNCHER) and all(
                f"python3 -I {LAUNCHER} sheregesh.videosvc.{role}" in root.cat(f"/usr/bin/sheregesh-video-{role}")
                for role in ("tx", "rx")))
    common = ("Type=notify", "NotifyAccess=main", "User=sheregesh-ui", "WatchdogSec=15", "LimitCORE=0",
              "RuntimeDirectory=sheregesh-video", "RuntimeDirectoryMode=0700", "NoNewPrivileges=yes",
              "ProtectSystem=strict", "IPAddressDeny=any", "StartLimitBurst=")
    rep.add("video.units", "юниты видео: tx, rx, таблицы nftables (пульт и блок), allow@ есть",
            all(units.values()), ", ".join(u for u, x in units.items() if not x) or "все на месте")
    rep.add("video.hardening", "службы видео: notify, watchdog, LimitCORE=0, каталог 0700, песочница",
            all(k in tx and k in rx for k in common) and "ExecStart=/usr/bin/sheregesh-video-tx" in tx
            and "ExecStart=/usr/bin/sheregesh-video-rx" in rx)
    rep.add("video.rx_nft", "rx не работает без таблицы nftables (BindsTo= и After= юнита загрузки)",
            "BindsTo=sheregesh-video-nft.service" in rx and "After=" in rx and "sheregesh-video-nft.service" in
            next((x for x in rx.splitlines() if x.startswith("After=")), ""))
    rep.add("video.tx_nft", "tx не работает без таблицы блока (BindsTo= и After=)",
            "BindsTo=sheregesh-video-nft-remote.service" in tx and "sheregesh-video-nft-remote.service" in
            next((x for x in tx.splitlines() if x.startswith("After=")), ""))
    nft = {n: "\n".join(x for x in root.cat(f"/usr/share/sheregesh/nft/{n}").splitlines() if not x.startswith("#"))
           for n in VIDEO_NFT}                                  # без комментариев: в них «ct state» упоминается
    rep.add("video.nft", "файлы nft: таблица inet sheregesh_video, hook input priority -10, drop со счётчиком",
            all("table inet sheregesh_video {" in x and "priority -10" in x and "counter drop" in x and "ct state" not in x
                for x in nft.values()), ", ".join(n for n, x in nft.items() if not x) or "оба на месте")
    allow = root.cat("/usr/bin/sheregesh-video-allow")
    rep.add("video.allow", "помощник sheregesh-video-allow и его обёртка (python3 -I) есть",
            "python3 -I " in allow and root.exists("/usr/lib/sheregesh/tools/image/bin/sheregesh-video-allow"))
    rules = root.cat(VIDEO_POLKIT)
    rep.add("video.polkit", "polkit обычного образа: sheregesh-ui — только «start» sheregesh-video-allow@<адрес|none>",
            'subject.user != "sheregesh-ui"' in rules and 'action.lookup("verb") != "start"' in rules
            and "sheregesh-video-allow@" in rules and rules.count("polkit.Result.YES") >= 2
            and "sheregesh-video-tx" not in rules and "stop" not in rules and "restart" not in rules,
            VIDEO_POLKIT)
    wants = " ".join(root.ls("/etc/systemd/system/multi-user.target.wants"))
    rep.add("video.enabled", "включены службы видео и таблицы (запуск решают ConditionPathExists роли)",
            all(u in wants for u in VIDEO_UNITS[:4]))
    listed = {x.split("\t")[0].split(":")[0] for x in (packages_tsv or "").splitlines() if x}
    rep.add("video.nftables", "пакет nftables в манифесте пакетов образа", "nftables" in listed)


TIME_POLKIT = "/etc/polkit-1/rules.d/53-sheregesh-time-peer.rules"


def time_checks(rep, root, packages_tsv):
    """ADR-0017: chrony вместо timesyncd, chrony.conf без источников (их по роли задаёт sheregesh-time), юниты
    помощника и правило polkit «только start sheregesh-time-peer@<адрес|none>»."""
    listed = {x.split("\t")[0].split(":")[0] for x in (packages_tsv or "").splitlines() if x}
    rep.add("time.chrony", "chrony в образе, systemd-timesyncd нет или замаскирован",
            "chrony" in listed and root.exists("/usr/sbin/chronyd")
            and ("systemd-timesyncd" not in listed or root.exists("/etc/systemd/system/systemd-timesyncd.service")))
    conf = root.cat("/etc/chrony/chrony.conf")
    lines = [x.split()[0] for x in conf.splitlines() if x.strip() and not x.lstrip().startswith("#")]
    rep.add("time.conf", "chrony.conf образа: ADR-0017, без источников и local, cmdport 0, makestep, drift в /persistent",
            "ADR-0017" in conf and not {"server", "pool", "peer", "local"} & set(lines)
            and "sourcedir /run/sheregesh-time/sources" in conf
            and "cmdport" in lines and "makestep" in lines and "driftfile /persistent/chrony/chrony.drift" in conf
            and all(x.split()[1] in ("127.0.0.1", "::1") for x in conf.splitlines() if x.startswith("allow")))
    role = root.cat("/usr/lib/systemd/system/sheregesh-time-role.service")
    peer = root.cat("/usr/lib/systemd/system/sheregesh-time-peer@.service")
    rep.add("time.units", "юниты sheregesh-time-role (после chrony, PartOf) и sheregesh-time-peer@ (песочница)",
            "ExecStart=/usr/bin/sheregesh-time role" in role and "PartOf=chrony.service" in role
            and "ExecStart=/usr/bin/sheregesh-time peer %i" in peer
            and all("IPAddressDeny=any" in u and "ProtectSystem=strict" in u for u in (role, peer)))
    dropin = root.cat("/etc/systemd/system/chrony.service.d/50-sheregesh.conf")
    rep.add("time.boot_clock", "нет RTC: chronyd -s, drift-файл в /persistent (дополнение юнита, tmpfiles _chrony)",
            '"-F 1 -s"' in root.cat("/etc/default/chrony") and "ReadWritePaths=/persistent/chrony" in dropin
            and "RequiresMountsFor=/persistent" in dropin
            and "d /persistent/chrony 0750 _chrony _chrony" in root.cat("/usr/lib/tmpfiles.d/sheregesh-chrony.conf"))
    wants = " ".join(root.ls("/etc/systemd/system/multi-user.target.wants"))
    timers = " ".join(root.ls("/etc/systemd/system/timers.target.wants"))
    rep.add("time.enabled", "включены chrony, sheregesh-time-role, отметка времени (save, таймер tick)",
            "chrony.service" in wants and "sheregesh-time-role.service" in wants
            and "sheregesh-time-save.service" in wants and "sheregesh-time-tick.timer" in timers)
    helper = root.cat("/usr/bin/sheregesh-time")
    rep.add("time.helper", "помощник sheregesh-time и его обёртка (python3 -I) есть",
            "python3 -I " in helper and root.exists("/usr/lib/sheregesh/tools/image/bin/sheregesh-time"))
    rules = root.cat(TIME_POLKIT)
    rep.add("time.polkit", "polkit: sheregesh-ui — только «start» sheregesh-time-peer@<адрес|none>",
            'subject.user != "sheregesh-ui"' in rules and 'action.lookup("verb") != "start"' in rules
            and "sheregesh-time-peer@" in rules and "stop" not in rules and "restart" not in rules, TIME_POLKIT)


REMOTE_POWER_POLKIT = "/etc/polkit-1/rules.d/54-sheregesh-remote-power.rules"


def remote_power_checks(rep, root):
    """Протокол 1.6 (REQ-UI-006): выключение блока с пульта — юнит sheregesh-remote-poweroff (только блок, root,
    systemctl poweroff) и правило polkit «sheregesh-ui — только start этого юнита»."""
    unit = root.cat("/usr/lib/systemd/system/sheregesh-remote-poweroff.service")
    rep.add("remote_power.unit", "юнит выключения блока: только роль remote, systemctl --no-block poweroff",
            "ConditionPathExists=/home/sheregesh-ui/.config/sheregesh/remote.role" in unit
            and "ExecStart=/usr/bin/systemctl --no-block poweroff" in unit and "NoNewPrivileges=yes" in unit)
    rules = root.cat(REMOTE_POWER_POLKIT)
    rep.add("remote_power.polkit", "polkit: sheregesh-ui — только «start» sheregesh-remote-poweroff.service",
            'subject.user != "sheregesh-ui"' in rules and 'action.lookup("verb") != "start"' in rules
            and '!= "sheregesh-remote-poweroff.service"' in rules and rules.count("polkit.Result.YES") == 1
            and "login1" not in rules, REMOTE_POWER_POLKIT)


def hostname_checks(rep, root):
    """Имя системы по роли (пульт pi1, блок pi2): помощник, юнит до systemd-networkd, включён в sysinit."""
    unit = root.cat("/usr/lib/systemd/system/sheregesh-hostname.service")
    rep.add("hostname.role", "имя системы по роли: sheregesh-hostname до сети, включён (sysinit)",
            "ExecStart=/usr/bin/sheregesh-hostname" in unit and "Before=systemd-networkd.service" in unit
            and "python3 -I " in root.cat("/usr/bin/sheregesh-hostname")
            and root.exists("/usr/lib/sheregesh/tools/image/bin/sheregesh-hostname")
            and "sheregesh-hostname.service" in " ".join(root.ls("/etc/systemd/system/sysinit.target.wants")))
    target, default = root.readlink("/etc/hostname"), root.cat("/usr/share/sheregesh/hostname.default").strip()
    rep.add("hostname.static", "/etc/hostname — ссылка на имя, записанное юнитом (иначе systemd-hostnamed возвращает "
            "имя образа после DHCP); имя образа сохранено", target == "../run/sheregesh-hostname/hostname" and bool(default),
            f"{target} ; default {default!r}")


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
            con = root.cat("/usr/lib/systemd/system/sheregesh-console.service")
            rep.add("console.relay", "пульт: главный PID — ретранслятор sd_notify (PAMName выводит процессы из cgroup)",
                    "ExecStart=/usr/bin/sheregesh-notify-relay -- /usr/bin/cage" in con and "NotifyAccess=main" in con
                    and root.exists("/usr/bin/sheregesh-notify-relay"))
            net = root.cat("/usr/lib/systemd/system/sheregesh-remote-net.service")
            rep.add("net.conditions", "sheregesh-remote-net не стартует без роли, ключа, сертификата и CA",
                    all(f"ConditionPathExists=/home/sheregesh-ui/{p}" in net for p in
                        (".config/sheregesh/remote.role", ".local/share/sheregesh/link/device.key",
                         ".local/share/sheregesh/link/ca.crt")) and "IPAddressDeny=any" in net)
            rep.add("net.video_config", "блок сохраняет выбор видеоисточника (S5-D1): каталог конфигурации на запись, "
                    "роль и link.json — только чтение",
                    "ReadWritePaths=-/home/sheregesh-ui/.config/sheregesh" in net
                    and all(f"-/home/sheregesh-ui/.config/sheregesh/{f}" in net for f in ("link.json", "remote.role"))
                    and "ProtectHome=read-only" in net)
            rep.add("emu.cache", "эмулятор: каталог кэша GStreamer и HOME (CacheDirectory)",
                    "CacheDirectory=sheregesh-remote-emu" in emu and "Environment=HOME=%C/sheregesh-remote-emu" in emu)
            nss = next((x for x in root.cat("/etc/nsswitch.conf").splitlines() if x.startswith("passwd:")), "")
            rep.add("nss.systemd", "NSS: uid DynamicUser разрешается (systemd в passwd:)", "systemd" in nss.split(), nss)
            rep.add("unit.eeprom-masked", "rpi-eeprom-update замаскирован (обновление EEPROM — явная процедура)",
                    "/dev/null" in root.run("stat /etc/systemd/system/rpi-eeprom-update.service"))
            rep.add("resolved.no-llmnr", "systemd-resolved: LLMNR и mDNS выключены (нет 0.0.0.0:5355)",
                    "LLMNR=no" in (rc := root.cat("/etc/systemd/resolved.conf.d/90-sheregesh.conf")) and "MulticastDNS=no" in rc)
            masked = "/dev/null" in root.run("stat /etc/systemd/system/console-setup.service")
            cs = [f"/etc/console-setup/{n}" for n in ls_names(root, "/etc/console-setup") if n.startswith("cached_")]
            stale = [] if masked else console_setup_stale({p: root.mtime(p) for p in (*cs, *CONSOLE_CONFIGS)})
            rep.add("console-setup.ro", "console-setup не пишет в ro-корень: кэш строго новее конфигов (или служба "
                    "замаскирована; E2E-13 OTA pi1 — равные mtime → пересоздание кэша → failed)",
                    masked or not stale, "замаскирована" if masked else ("; ".join(stale) or "кэш новее конфигов"))
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
                rep.add("ssh.dev", "dev: sshd только по ключу, root — тоже только по ключу",
                        sshd and "PasswordAuthentication no" in conf and "PermitRootLogin prohibit-password" in conf)
                rkeys = root.cat("/root/.ssh/authorized_keys")
                rep.add("ssh.root", "dev: ключ root — ключ входа sheregesh-ui, закрытых ключей нет",
                        rkeys.startswith(("ssh-", "ecdsa-")) and "PRIVATE KEY" not in rkeys
                        and rkeys.split()[:2] == data.cat("/home/sheregesh-ui/.ssh/authorized_keys").split()[:2],
                        rkeys.split(" ")[0])
                keys = data.cat("/home/sheregesh-ui/.ssh/authorized_keys")
                rep.add("ssh.key", "dev: открытый ключ sheregesh-ui установлен, закрытых ключей нет",
                        keys.startswith(("ssh-", "ecdsa-")) and "PRIVATE KEY" not in keys, keys.split(" ")[0])
                rep.add("net.dev", "dev: DHCP на eth0", "DHCP=yes" in root.cat("/etc/systemd/network/20-sheregesh-dev.network"))
            else:
                rep.add("ssh.absent", "строгий вариант: sshd не установлен (ADR-0002)", not sshd)
                rep.add("net.lab", "статический адрес стенда 192.168.77.1/24 без шлюза (ADR-0007)",
                        "Address=192.168.77.1/24" in (n := root.cat("/etc/systemd/network/20-sheregesh-lab.network"))
                        and "Gateway" not in n)
            deploy_checks(rep, root, data, dev, groups)
            labvideo_checks(rep, root, dev)
            packages_tsv = root.cat("/usr/share/sheregesh/image-packages.tsv")
            video_checks(rep, root, packages_tsv)
            time_checks(rep, root, packages_tsv)
            remote_power_checks(rep, root)
            hostname_checks(rep, root)
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
            rep.add("boot.config", "config.txt: dtoverlay=miniuart-bt и core_freq_min=500 — консоль ttyAMA0 на GPIO14/15 (ADR-0002)",
                    cfg is not None and "dtoverlay=miniuart-bt" in cfg and "core_freq_min=500" in cfg)
            rep.add("boot.uart3", "config.txt: dtoverlay=uart3 — линия CRSF к JR-модулю на GPIO4/5 (ADR-0014)",
                    cfg is not None and "dtoverlay=uart3" in cfg)
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
