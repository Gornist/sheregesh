# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ремонт состояния агента OTA на карте выключенной платы (macOS, debugfs из e2fsprogs) — S3.2-OTA, pi2.

Зачем: агент OTA слота A (образ 57e8fe4) падает на state.json, записанном агентом a7f1b74 (поле "expect"), а root
в образе заблокирован. Без state.json агент начинает с чистого IDLE на загруженном слоте (Agent.updater) —
слоты, autoboot.txt, ключи и конфигурация пульта не затрагиваются. Меняется ровно одно имя на разделе persistent:
state.json переименовывается в state.json.a7f1b74 (свидетельство остаётся; JSON вручную не правится).

  python3 tools/image/repair_ota_state.py                          найти раздел persistent платы (без записи)
  python3 tools/image/repair_ota_state.py --disk disk4s6 --dump    показать state.json (только чтение)
  python3 tools/image/repair_ota_state.py --disk disk4s6 --confirm disk4s6   переименовать state.json

Раздел опознаётся по GUID раздела GPT (по умолчанию p6 pi2), а не по номеру диска. Переименование выполняется, только
если файл — тот самый (состояние и цель из --expect-state/--expect-target, есть поле expect); копия содержимого
сохраняется в --backup до записи. После — e2fsck -fn (только проверка) и diskutil eject.
"""
import argparse
import json
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

PI2_PERSISTENT = "45060026-07fc-6508-1875-00471802f811"
STATE = "/sheregesh-ota/state.json"
KEPT = STATE + ".a7f1b74"


def tool(name):
    for prefix in ("/opt/homebrew/opt/e2fsprogs/sbin", "/usr/local/opt/e2fsprogs/sbin"):
        if Path(prefix, name).exists():
            return str(Path(prefix, name))
    if shutil.which(name):
        return shutil.which(name)
    raise SystemExit(f"нет {name}: brew install e2fsprogs")


def plist(verb, *args):
    return plistlib.loads(subprocess.run(["diskutil", verb, "-plist", *args], capture_output=True, check=True).stdout)


def find(partuuid):
    """Разделы внешних физических дисков с данным GUID раздела GPT."""
    hits = []
    for disk in plist("list", "external", "physical").get("AllDisksAndPartitions", []):
        for p in disk.get("Partitions", []):
            info = plist("info", p["DeviceIdentifier"])
            if str(info.get("DiskUUID", "")).lower() == partuuid.lower():
                hits.append(p["DeviceIdentifier"])
    return hits


def debugfs(dev, request, write=False, run=subprocess.run):
    """Запрос debugfs; несколько строк — одним запуском (-f -)."""
    cmd = ["sudo", tool("debugfs"), *(["-w"] if write else []), *(["-f", "-"] if "\n" in request else ["-R", request]),
           f"/dev/{dev}"]
    r = run(cmd, input=request if "\n" in request else None, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"debugfs {request!r}: {r.stderr.strip()}")
    return r.stdout


def check(text, want_state, want_target):
    """Содержимое state.json → (данные, причина отказа или None)."""
    try:
        d = json.loads(text)
    except ValueError:
        return None, "state.json не JSON"
    if "expect" not in d:
        return d, "поля expect нет — агент 57e8fe4 его прочтёт, ремонт не нужен"
    if (d.get("state"), d.get("target")) != (want_state, want_target):
        return d, f"состояние {d.get('state')}/{d.get('target')}, ожидалось {want_state}/{want_target}"
    return d, None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="repair_ota_state", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--partuuid", default=PI2_PERSISTENT)
    ap.add_argument("--disk", help="раздел, например disk4s6")
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--confirm", help="повтор идентификатора раздела — разрешение на переименование state.json")
    ap.add_argument("--expect-state", default="TRYBOOT")
    ap.add_argument("--expect-target", default="B")
    ap.add_argument("--backup", default="docs/acceptance/ota-closure/hardware/state-before-repair.json")
    a = ap.parse_args(argv)

    hits = find(a.partuuid)
    if not a.disk:
        print(f"раздел {a.partuuid}: {', '.join(hits) or 'не найден'}")
        return 0 if len(hits) == 1 else 1
    if hits != [a.disk]:
        raise SystemExit(f"{a.disk} не раздел {a.partuuid} (найдено: {hits or 'ничего'}) — стоп")
    if plist("info", a.disk).get("MountPoint"):
        raise SystemExit(f"{a.disk} смонтирован — стоп")
    text = debugfs(a.disk, f"cat {STATE}")
    data, why = check(text, a.expect_state, a.expect_target)
    print(text.strip())
    if a.dump:
        return 0
    if a.confirm != a.disk:
        raise SystemExit("нужно --confirm с тем же разделом")
    if why:
        raise SystemExit(f"не меняю: {why}")
    if Path(KEPT).name in debugfs(a.disk, "ls /sheregesh-ota").split():
        raise SystemExit(f"{KEPT} уже есть — стоп")
    Path(a.backup).parent.mkdir(parents=True, exist_ok=True)
    Path(a.backup).write_text(text)
    # link + unlink = переименование: debugfs не меняет счётчик ссылок ни в link, ни в unlink (проверено e2fsck)
    debugfs(a.disk, f"link {STATE} {KEPT}\nunlink {STATE}\n", write=True)
    print(debugfs(a.disk, "ls -l /sheregesh-ota").strip())
    r = subprocess.run(["sudo", tool("e2fsck"), "-fn", f"/dev/{a.disk}"], capture_output=True, text=True)
    print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip())
    if r.returncode:
        raise SystemExit(f"e2fsck -fn: код {r.returncode} — карту не вставлять, сообщить")
    subprocess.run(["diskutil", "eject", a.disk.rsplit("s", 1)[0]], check=True)
    print(f"state.json → {KEPT}; карта извлечена. После загрузки: агент слота A — IDLE на A.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
