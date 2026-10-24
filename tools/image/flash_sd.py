# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Запись образа «Шерегеш» на microSD (macOS) с защитой от записи не на тот диск.

  python3 tools/image/flash_sd.py ОБРАЗ.img.zst                  проверить образ и показать подходящие карты (без записи)
  python3 tools/image/flash_sd.py ОБРАЗ.img.zst --disk disk4     записать на disk4 (с подтверждением)

Порядок:
1. SHA-256 файла .img.zst сверяется с SHA256SUMS рядом с ним (из артефакта CI или os/build.sh). Не сошлось — стоп.
2. Кандидаты — только внешние физические целые диски (не Internal) размером не меньше образа и не больше
   --max-gb (по умолчанию 256 ГБ). Без --disk скрипт только показывает их и выходит.
3. Если на выбранной карте уже есть система (разделы bootfs/rootfs/BOOT/BOOTCONFIG/SYSTEM/PERSISTENT), запись
   запрещена без --overwrite-existing: исходные карты плат не перезаписываются.
4. Подтверждение — ввести идентификатор диска ещё раз (или --confirm diskN для запуска без терминала).
5. diskutil unmountDisk → один процесс под sudo (пароль спросят один раз) пишет образ в /dev/rdiskN и, не закрывая
   устройство, читает его обратно и сравнивает SHA-256 с image_sha256 из build-report.json. Пока устройство открыто,
   macOS не перечитывает таблицу разделов и не монтирует FAT-разделы: иначе она успевает записать на карту
   .fseventsd/.Spotlight-V100 и сверка не сходится (найдено в S2-BOOT). Сразу после закрытия — diskutil eject.
"""
import argparse
import hashlib
import json
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

SYSTEM_VOLUMES = {"bootfs", "rootfs", "boot", "bootconfig", "system", "persistent"}
CHUNK = 4 * 1024 * 1024


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def expected_sums(image: Path):
    """(SHA-256 .img.zst из SHA256SUMS, SHA-256 и размер распакованного образа из build-report.json или None)."""
    sums = image.with_name("SHA256SUMS")
    if not sums.exists():
        raise SystemExit(f"нет {sums}: без контрольной суммы образ не пишется")
    want = next((ln.split()[0] for ln in sums.read_text().splitlines() if ln.split()[-1:] == [image.name]), None)
    if not want:
        raise SystemExit(f"{image.name} не указан в {sums}")
    raw = None
    report = image.with_name("build-report.json")
    if report.exists():
        r = json.loads(report.read_text())
        raw = (r.get("image_sha256"), r.get("image_bytes"))
    return want, raw


def plist(verb, *args):
    """diskutil VERB -plist ARGS (флаг -plist — сразу после подкоманды)."""
    return plistlib.loads(subprocess.run(["diskutil", verb, "-plist", *args], capture_output=True, check=True).stdout)


def describe(info, partitions):
    return {"id": info.get("DeviceIdentifier"), "internal": bool(info.get("Internal")),
            "whole": bool(info.get("WholeDisk")), "size": int(info.get("Size") or info.get("TotalSize") or 0),
            "name": info.get("MediaName") or "?", "bus": info.get("BusProtocol") or "?",
            "removable": bool(info.get("RemovableMedia") or info.get("Removable") or info.get("Ejectable")),
            "volumes": [p.get("VolumeName") or p.get("Content") or "" for p in partitions]}


def refusal(disk, image_bytes, max_bytes):
    """Причина, по которой диск не подходит, или None."""
    if disk["internal"]:
        return "внутренний диск"
    if not disk["whole"]:
        return "не целый диск"
    if disk["size"] < image_bytes:
        return f"меньше образа ({disk['size'] / 1e9:.1f} ГБ)"
    if disk["size"] > max_bytes:
        return f"больше {max_bytes / 1e9:.0f} ГБ — похоже не на microSD (--max-gb)"
    return None


def existing_system(disk):
    return sorted({v for v in disk["volumes"] if v.lower() in SYSTEM_VOLUMES})


def candidates():
    out = []
    for dev in plist("list", "external", "physical").get("WholeDisks", []):
        info = plist("info", dev)
        parts = next((d.get("Partitions", []) for d in plist("list", dev).get("AllDisksAndPartitions", [])
                      if d.get("DeviceIdentifier") == dev), [])
        out.append(describe(info, parts))
    return out


def write_and_verify(image, target, size, want_sha, zstd="zstd"):
    """Под root: zstd -dc → target (/dev/rdiskN), fsync, чтение обратно тем же дескриптором, сверка SHA-256."""
    import os
    fd = os.open(target, os.O_RDWR)
    try:
        z = subprocess.Popen([zstd, "-dc", str(image)], stdout=subprocess.PIPE)
        written, buf = 0, bytearray()
        while True:
            chunk = z.stdout.read(CHUNK - len(buf))
            if chunk:
                buf += chunk
            if len(buf) == CHUNK or (not chunk and buf):
                os.write(fd, bytes(buf))
                written += len(buf)
                buf.clear()
                if written % (512 * 1024 * 1024) < CHUNK:
                    print(f"  записано {written / 1e9:.2f} из {size / 1e9:.2f} ГБ", flush=True)
            if not chunk:
                break
        if z.wait() != 0 or written != size:
            print(f"ОШИБКА записи: zstd={z.returncode}, записано {written} из {size} Б", flush=True)
            return 2
        os.fsync(fd)
        print("Проверка записи: чтение тем же дескриптором …", flush=True)
        os.lseek(fd, 0, os.SEEK_SET)
        h, left = hashlib.sha256(), size
        while left:
            data = os.read(fd, min(CHUNK, left))
            if not data:
                break
            h.update(data)
            left -= len(data)
    finally:
        os.close(fd)
    if left or h.hexdigest() != want_sha:
        print(f"ПРОВЕРКА НЕ ПРОЙДЕНА: на карте {h.hexdigest()}, ожидалось {want_sha} (не прочитано {left} Б)", flush=True)
        return 1
    print(f"Проверка OK: {want_sha}", flush=True)
    return 0


def run(cmd, **kw):
    print("$", " ".join(cmd) if isinstance(cmd, list) else cmd, flush=True)
    return subprocess.run(cmd, check=True, **kw)


def main(argv=None):
    if len(sys.argv) == 7 and sys.argv[1] == "--write-as-root":       # внутренний режим, запускается через sudo
        _, _, image, disk, size, sha, zstd = sys.argv                 # sudo сбрасывает PATH — zstd передаётся путём
        if not re.fullmatch(r"disk\d+", disk) or not re.fullmatch(r"[0-9a-f]{64}", sha) or \
                not (Path(zstd).is_absolute() and Path(zstd).name == "zstd" and Path(zstd).is_file()):
            raise SystemExit("неверные параметры")
        return write_and_verify(Path(image), f"/dev/r{disk}", int(size), sha, zstd)
    if sys.platform != "darwin":
        raise SystemExit("скрипт для macOS (diskutil); в Linux используйте Raspberry Pi Imager")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", type=Path)
    ap.add_argument("--disk", help="идентификатор целого диска, например disk4")
    ap.add_argument("--confirm", help="повтор идентификатора для запуска без терминала")
    ap.add_argument("--overwrite-existing", action="store_true", help="разрешить запись на карту с установленной системой")
    ap.add_argument("--max-gb", type=float, default=256)
    a = ap.parse_args(argv)
    if not shutil.which("zstd"):
        raise SystemExit("нужен zstd: brew install zstd")

    want, raw = expected_sums(a.image)
    print(f"Проверка {a.image.name} …", flush=True)
    got = sha256_file(a.image)
    if got != want:
        raise SystemExit(f"SHA-256 не совпадает:\n  ожидалось {want}\n  получено  {got}")
    print(f"SHA-256 OK: {got}")
    image_bytes = raw[1] if raw and raw[1] else 0
    if not image_bytes:
        image_bytes = int(subprocess.run(f"zstd -dc '{a.image}' | wc -c", shell=True, capture_output=True,
                                         text=True, check=True).stdout.strip())

    disks = candidates()
    rows = [(d, refusal(d, image_bytes, a.max_gb * 1e9)) for d in disks]
    print(f"\nВнешние физические диски (образ {image_bytes / 1e9:.2f} ГБ):")
    for d, why in rows or []:
        sys_parts = existing_system(d)
        mark = "не подходит: " + why if why else ("ЕСТЬ СИСТЕМА: " + ", ".join(sys_parts) if sys_parts else "подходит")
        print(f"  /dev/{d['id']:<7} {d['size'] / 1e9:7.1f} ГБ  {d['name']:<28} {d['bus']:<8} разделы: "
              f"{', '.join(v for v in d['volumes'] if v) or '—'}  [{mark}]")
    if not rows:
        print("  нет — вставьте карту в кардридер")
    if not a.disk:
        print("\nЗаписи не было. Для записи: --disk diskN (проверьте объём и модель).")
        return 0

    disk = next((d for d, _ in rows if d["id"] == a.disk), None)
    if not re.fullmatch(r"disk\d+", a.disk or "") or disk is None:
        raise SystemExit(f"{a.disk}: нет среди внешних физических дисков — запись отменена")
    why = refusal(disk, image_bytes, a.max_gb * 1e9)
    if why:
        raise SystemExit(f"{a.disk}: {why} — запись отменена")
    if existing_system(disk) and not a.overwrite_existing:
        raise SystemExit(f"{a.disk}: на карте уже есть система ({', '.join(existing_system(disk))}). Исходные карты плат "
                         "не перезаписываются; для другой карты добавьте --overwrite-existing")
    print(f"\nБУДЕТ СТЁРТ /dev/{a.disk}: {disk['name']}, {disk['size'] / 1e9:.1f} ГБ, {disk['bus']}.")
    confirm = a.confirm
    if confirm is None:
        if not sys.stdin.isatty():
            raise SystemExit("нужно подтверждение: запустите в терминале или добавьте --confirm " + a.disk)
        confirm = input(f"Введите «{a.disk}» для подтверждения: ").strip()
    if confirm != a.disk:
        raise SystemExit("подтверждение не совпало — запись отменена")

    ref = raw[0] if raw and raw[0] else None
    if ref is None:
        print("Нет build-report.json: считаю SHA-256 распакованного образа …", flush=True)
        ref = subprocess.run(f"zstd -dc '{a.image}' | shasum -a 256", shell=True, capture_output=True, text=True,
                             check=True).stdout.split()[0]
    run(["diskutil", "unmountDisk", f"/dev/{a.disk}"])
    rc = subprocess.run(["sudo", sys.executable, "-I", str(Path(__file__).resolve()), "--write-as-root",
                         str(a.image.resolve()), a.disk, str(image_bytes), ref, shutil.which("zstd")]).returncode
    ej = subprocess.run(["diskutil", "eject", f"/dev/{a.disk}"]).returncode   # сразу, чтобы macOS не смонтировала FAT
    if rc:
        raise SystemExit(f"запись или проверка не удались (код {rc}); карту в плату не ставить")
    if ej:
        print(f"Карта проверена, но не извлечена (diskutil eject, код {ej}): извлеките её в Finder перед тем, "
              "как вынимать.")
        return 3
    print("Готово: карта проверена и извлечена.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
