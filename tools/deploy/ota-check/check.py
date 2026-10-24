# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Установка настоящего системного обновления на настоящий образ диска (S3.2-OTA, L3 — файловый образ, не плата).

Запускается в привилегированном контейнере (root, /dev хоста) из tools/deploy/ota-check/run.py:
  python3 /work/check.py
В /work: base.img.zst — образ плат (как на картах), payload.shota — подписанный поток, foreign.sig — подпись
манифеста посторонним ключом, allowed_signers, enable.json(.sig) — разрешение для тестового серийного номера,
software/ — код агента из репозитория. Образ подключается как loop-диск с разделами GPT image-rota; агент OTA
(sheregesh.update.ota, тот же код, что на платах) пишет слот B настоящими средствами (O_EXCL на блочном устройстве,
повторное чтение, mount ro + rsync для /var). Платы Pi не затрагиваются. Итог — строки PASS/FAIL и /work/result.json.
"""
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

W = Path("/work")
sys.path.insert(0, str(W / "software"))
from sheregesh.update import ota, slots  # noqa: E402

RESULTS = []
SERIAL = "TESTSERIAL00ota"


def check(name, ok, detail=""):
    RESULTS.append({"name": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)[:400]})
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail and not ok else ""), flush=True)
    return ok


def sh(*cmd, check_rc=True):
    r = subprocess.run(list(cmd), capture_output=True, text=True)
    if check_rc and r.returncode:
        raise RuntimeError(f"{cmd}: {r.stderr.strip()}")
    return r.stdout.strip()


def sha_dev(dev, size=None):
    h = hashlib.sha256()
    with open(dev, "rb") as f:
        left = size
        while left is None or left > 0:
            chunk = f.read(8 << 20 if left is None else min(8 << 20, left))
            if not chunk:
                break
            h.update(chunk)
            if left is not None:
                left -= len(chunk)
    return h.hexdigest()


def tree_hashes(root, skip=()):
    out = {}
    for p in sorted(Path(root).rglob("*")):
        rel = p.relative_to(root).as_posix()
        if any(rel == s or rel.startswith(s + "/") for s in skip):
            continue
        if p.is_file() and not p.is_symlink():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class Chain(io.RawIOBase):
    """Поток: заменённый блок подписи + остаток файла (подпись посторонним ключом без копии 300 МБ)."""

    def __init__(self, head, f):
        self.head, self.f = io.BytesIO(head), f

    def readable(self):
        return True

    def read(self, n=-1):
        data = self.head.read(n)
        if n is not None and n >= 0 and len(data) < n:
            data += self.f.read(n - len(data))
        elif n is None or n < 0:
            data += self.f.read()
        return data


def split_payload(path):
    with open(path, "rb") as f:
        assert f.read(len(ota.MAGIC)) == ota.MAGIC
        n1 = int(f.readline())
        manifest = f.read(n1)
        n2 = int(f.readline())
        f.read(n2)
        return manifest, f.tell()


def main():
    t0 = time.monotonic()
    img = W / "disk.img"
    if not img.exists():
        sh("zstd", "-d", "-q", "--sparse", "-o", str(img), str(W / "base.img.zst"))
    loop = sh("losetup", "-P", "--find", "--show", str(img))
    p = lambda n: f"{loop}p{n}"                                              # noqa: E731
    for _ in range(50):
        if all(os.path.exists(p(n)) for n in range(1, 7)):
            break
        time.sleep(0.2)
    mnt = W / "mnt"
    try:
        labels = {n: Path(f"/sys/class/block/{os.path.basename(p(n))}/uevent").read_text() for n in range(1, 7)}
        check("loop: разделы GPT image-rota с PARTNAME", all(f"PARTNAME={x}" in labels[n] for n, x in
              zip(range(1, 7), ("bootconfig", "boot_a", "boot_b", "system_a", "system_b", "persistent"))))
        bs = W / "by-slot"
        shutil.rmtree(bs, ignore_errors=True)
        for k, (b, s) in (("active", (2, 4)), ("other", (3, 5))):
            (bs / k).mkdir(parents=True)
            os.symlink(p(b), bs / k / "boot")
            os.symlink(p(s), bs / k / "system")
        os.symlink(p(1), bs / "bootconfig")
        os.symlink(p(6), bs / "persistent")
        for d in ("persistent", "bootfs"):
            (mnt / d).mkdir(parents=True, exist_ok=True)
        sh("mount", "-t", "ext4", p(6), str(mnt / "persistent"))
        sh("mount", "-t", "vfat", p(1), str(mnt / "bootfs"))
        chosen = W / "chosen"
        chosen.mkdir(exist_ok=True)
        (chosen / "partition").write_bytes(struct.pack(">I", 2))
        (chosen / "tryboot").write_bytes(struct.pack(">I", 0))
        mountinfo = W / "mountinfo"
        mountinfo.write_text(f"22 1 179:4 / / ro - ext4 {p(4)} ro\n"
                             f"24 22 179:1 / /bootfs rw - vfat {p(1)} rw\n"
                             f"25 22 179:6 / /persistent rw - ext4 {p(6)} rw\n")
        pers = mnt / "persistent"
        # Данные, которые обязаны пережить обновление (как на платах после первой загрузки и выпуска ключей)
        home = pers / "home/sheregesh-ui"
        for rel, text in ((".ssh/authorized_keys", "ssh-ed25519 AAAA owner\n"),
                          (".config/sheregesh/link.json", '{"role": "console"}\n'), (".config/sheregesh/console.role", "u\n"),
                          (".local/share/sheregesh/link/device.key", "PRIVATE-TEST-ONLY\n"),
                          (".local/share/sheregesh/link/device.crt", "CERT\n"), (".local/share/sheregesh/link/ca.crt", "CA\n")):
            (home / rel).parent.mkdir(parents=True, exist_ok=True)
            (home / rel).write_text(text)
        (pers / "shared/etc/ssh").mkdir(parents=True, exist_ok=True)
        (pers / "shared/etc/ssh/ssh_host_ed25519_key.pub").write_text("ssh-ed25519 HOST\n")
        (pers / "common/etc").mkdir(parents=True, exist_ok=True)
        (pers / "common/etc/machine-id").write_text("0123456789abcdef0123456789abcdef\n")
        cfg_a = pers / "slots/system_a/var/lib/sheregesh/config/active"
        cfg_a.parent.mkdir(parents=True, exist_ok=True)
        cfg_a.write_text("r7\n")
        root = W / "root"
        shutil.rmtree(root, ignore_errors=True)
        (root / "etc").mkdir(parents=True)
        os.symlink(pers / "home", root / "home")
        os.symlink(pers / "shared/etc/ssh", root / "etc/ssh")
        os.symlink(pers / "common/etc/machine-id", root / "etc/machine-id")
        etc = W / "etc"
        etc.mkdir(exist_ok=True)
        (etc / "DEV-MODE").write_text("dev\n")
        (W / "serial").write_bytes(SERIAL.encode() + b"\0")
        (W / "build.json").write_text(json.dumps({"ota_version": "0.1.1792843040", "commit": "57e8fe4"}))
        paths = slots.Paths(by_slot=str(bs), sys_block="/sys/class/block", mountinfo=str(mountinfo),
                            chosen=str(chosen), bootfs=str(mnt / "bootfs"), persistent=str(pers),
                            var=str(pers / "slots/system_a/var"))

        def agent():
            cfg = ota.AgentConfig(paths=paths, state_dir=pers / "sheregesh-ota-test", signers=W / "allowed_signers",
                                  build_json=W / "build.json", serial=W / "serial", dev_mode=etc / "DEV-MODE",
                                  tryboot_cmd=("true",), reboot_cmd=("true",), root=root)
            return ota.Agent(cfg)

        def request(action, stream=None, **extra):
            out = []
            res = agent().handle({"action": action, **extra}, stream or io.BytesIO(), out.append)
            return res, out

        lay = slots.detect(paths)
        check("detect: активный A, неактивный B по 4 признакам", (lay.active.name, lay.other.name) == ("a", "b"),
              f"{lay}")
        sz = {n: int(sh("blockdev", "--getsize64", p(n))) for n in (2, 3, 4, 5)}
        a_before = {n: sha_dev(p(n)) for n in (2, 4)}
        b_before = {n: sha_dev(p(n)) for n in (3, 5)}
        autoboot0 = (mnt / "bootfs/autoboot.txt").read_text()
        keep = ("slots/system_b", "sheregesh-ota-test", "lost+found")
        pers0 = tree_hashes(pers, skip=keep)
        manifest_raw, data_off = split_payload(W / "payload.shota")
        manifest = json.loads(manifest_raw)
        print(f"payload: версия {manifest['version']} коммит {manifest['commit'][:12]} {manifest.get('compatible')}")

        # 1. Подпись посторонним ключом — отказ до записи
        foreign = (W / "foreign.sig").read_bytes()
        head = ota.MAGIC + b"%d\n" % len(manifest_raw) + manifest_raw + b"%d\n" % len(foreign) + foreign
        with open(W / "payload.shota", "rb") as f:
            f.seek(data_off)
            res, _ = request("install", Chain(head, f))
        check("чужая подпись: REJECTED", res["result"] == "REJECTED", res.get("reason"))
        check("чужая подпись: слот B не тронут", {n: sha_dev(p(n)) for n in (3, 5)} == b_before)

        # 2. Обрезанный поток — FAILED, активный слот и autoboot не тронуты, пробная загрузка невозможна
        cut = (W / "payload.shota").stat().st_size // 2
        with open(W / "payload.shota", "rb") as f:
            res, _ = request("install", io.BytesIO(f.read(cut)))
        check("обрезанный поток: FAILED", res["result"] == "FAILED", res.get("reason"))
        check("обрезанный поток: A не тронут", {n: sha_dev(p(n)) for n in (2, 4)} == a_before)
        check("обрезанный поток: autoboot не тронут", (mnt / "bootfs/autoboot.txt").read_text() == autoboot0)
        (pers / "sheregesh-ota-test/enable.json").parent.mkdir(parents=True, exist_ok=True)
        res, _ = request("enable", token=(W / "enable.json").read_text(), sig=(W / "enable.json.sig").read_text())
        check("разрешение переключения для тестового серийного номера принято", res["result"] == "OK", res.get("reason"))
        res, _ = request("tryboot")
        check("после обрыва пробная загрузка запрещена", res["result"] == "REJECTED", res.get("reason"))

        # 3. Полная установка
        t1 = time.monotonic()
        with open(W / "payload.shota", "rb") as f:
            res, msgs = request("install", f)
        dt = round(time.monotonic() - t1, 1)
        check("установка: OK, слот B INSTALLED", res["result"] == "OK" and res["state"]["state"] == "INSTALLED",
              f"{res.get('reason')} ({dt} с)")
        imgs = {i["part"]: i for i in manifest["images"]}
        check("B/boot: SHA-256 = манифест", sha_dev(p(3), imgs["boot"]["size"]) == imgs["boot"]["sha256"])
        check("B/system: SHA-256 = манифест", sha_dev(p(5), imgs["system"]["size"]) == imgs["system"]["sha256"])
        check("A/boot и A/system не изменились", {n: sha_dev(p(n)) for n in (2, 4)} == a_before)
        check("autoboot.txt не изменён установкой", (mnt / "bootfs/autoboot.txt").read_text() == autoboot0)
        check("общие данные (home, ключи, сертификаты, роль, /etc/ssh, machine-id) не изменились",
              tree_hashes(pers, skip=keep) == pers0)
        var_b = pers / "slots/system_b/var"
        check("/var слота B создан из нового образа", (var_b / "lib/dpkg/status").exists())
        check("конфигурация пульта перенесена в B", (var_b / "lib/sheregesh/config/active").read_text() == "r7\n")
        check("/var слота A не изменён", cfg_a.read_text() == "r7\n")
        exp = res["state"].get("expect", {})
        check("отпечатки для подтверждения сняты (роль console, ключ, machine-id)",
              exp.get("role") == "console" and exp.get("device_key") and exp.get("machine_id"), exp)
        # содержимое нового слота: версия, исправление app-stop, юниты = манифест
        (mnt / "newsys").mkdir(exist_ok=True)
        sh("mount", "-o", "ro,noload", "-t", "ext4", p(5), str(mnt / "newsys"))
        try:
            nb = json.loads((mnt / "newsys/usr/share/sheregesh/build.json").read_text())
            check("новый слот: build.json = коммит и версия манифеста",
                  nb.get("commit") == manifest["commit"] and nb.get("ota_version") == manifest["version"], nb)
            relay = (mnt / "newsys/usr/lib/sheregesh/software/sheregesh/notify_relay.py").read_text()
            check("новый слот: исправление app-stop (02a9b64) в ретрансляторе", "return 0" in relay
                  and "штатный останов" in relay)
            inst = (mnt / "newsys/usr/lib/sheregesh/software/sheregesh/deploy/installer.py").read_text()
            check("новый слот: установщик выставляет время коммита", "os.utime(dst, (stamp, stamp))" in inst)
            conf = (mnt / "newsys/usr/lib/sheregesh/software/sheregesh/update/confirm.py").exists()
            check("новый слот: критерии подтверждения confirm.py", conf)
            units = {u: hashlib.sha256((mnt / f"newsys/usr/lib/systemd/system/{u}").read_bytes()).hexdigest()
                     for u in manifest.get("units", {})}
            check("новый слот: юниты = манифест", units == manifest.get("units") and len(units) >= 9,
                  f"{len(units)} юнитов")
            priv = sh("sh", "-c", f"grep -rIl 'PRIVATE KEY' {mnt}/newsys/usr/lib/sheregesh {mnt}/newsys/etc/sheregesh "
                      f"2>/dev/null | head -3", check_rc=False)
            check("новый слот: закрытых ключей в /usr/lib/sheregesh и /etc/sheregesh нет", not priv, priv)
        finally:
            sh("umount", str(mnt / "newsys"), check_rc=False)

        # 4. Перезапуск агента — состояние восстановлено
        res, _ = request("status")
        check("перезапуск агента: INSTALLED, согласовано со слотами",
              res["state"]["state"] == "INSTALLED" and res["consistent"], res.get("state"))
        # 5. Изменение конфигурации после установки → свежая копия перед tryboot (без перезагрузки: tryboot_cmd=true)
        cfg_a.write_text("r8\n")
        res, _ = request("tryboot")
        check("tryboot: OK (перезагрузка подменена)", res["result"] == "OK", res.get("reason"))
        check("autoboot.txt на FAT: [all]=2 (основной A), [tryboot]=3",
              slots.autoboot_parts((mnt / "bootfs/autoboot.txt").read_text()) == (2, 3))
        check("конфигурация перенесена заново перед tryboot (r8)", (var_b / "lib/sheregesh/config/active").read_text()
              == "r8\n")
        # 6. Повторная установка во время пробной загрузки — отказ; отмена — без смены основного слота
        with open(W / "payload.shota", "rb") as f:
            res, _ = request("install", f)
        check("повторная установка в TRYBOOT: REJECTED", res["result"] == "REJECTED", res.get("reason"))
        res, _ = request("abort")
        check("abort: OK, основной слот A", res["result"] == "OK"
              and slots.autoboot_parts((mnt / "bootfs/autoboot.txt").read_text())[0] == 2, res.get("reason"))
        check("в конце A не изменён", {n: sha_dev(p(n)) for n in (2, 4)} == a_before)
        check("в конце общие данные не изменились", tree_hashes(pers, skip=keep) == pers0)
        print(f"размеры: boot {sz[3]} system {sz[5]}; установка {dt} с; всего {round(time.monotonic() - t0)} с")
    finally:
        for d in ("newsys", "bootfs", "persistent"):
            subprocess.run(["umount", str(mnt / d)], capture_output=True)
        subprocess.run(["losetup", "-d", loop], capture_output=True)
    ok = sum(r["status"] == "PASS" for r in RESULTS)
    (W / "result.json").write_text(json.dumps({"pass": ok, "fail": len(RESULTS) - ok, "results": RESULTS},
                                              ensure_ascii=False, indent=1))
    print(f"== итог: PASS {ok}, FAIL {len(RESULTS) - ok}")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
