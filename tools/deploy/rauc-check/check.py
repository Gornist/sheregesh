# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""RAUC на файловом образе диска с разметкой image-rota (прототип варианта C ADR-0009; L3 — файловый образ, не плата).

Запускается в привилегированном контейнере Debian trixie (root, /dev хоста) из tools/deploy/rauc-check/run.py:
  python3 /work/check.py
В /work: software/ (код репозитория), tools/rauc/ (system.conf, testpki.py, build_bundle.py). Всё прочее создаётся
здесь: тестовые CA и подписанты (только в /work, удаляется вместе с каталогом), синтетический диск GPT image-rota
(bootconfig | boot_a | boot_b | system_a | system_b | persistent) на loop-устройстве, образы boot.vfat/system.ext4
двух версий, бандлы RAUC. Настоящий `rauc service` (D-Bus) с system.conf репозитория (подставлены устройства loop
и пути /work) выполняет установку; бэкенд custom, хуки и health — модули sheregesh.update.rauc_*. Прошивка Pi 4
(tryboot) эмулируется по autoboot.txt: каждая «перезагрузка» — новые device-tree, by-slot, mountinfo и перезапуск
rauc service. Платы Pi не затрагиваются. Итог — строки PASS/FAIL и /work/result.json.
"""
import hashlib
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

W = Path("/work")
sys.path.insert(0, str(W / "software"))
sys.path.insert(0, str(W / "tools/rauc"))
import build_bundle  # noqa: E402
import testpki  # noqa: E402

from sheregesh.update import confirm, guard, ota, rauc_backend, rauc_hooks, slots  # noqa: E402

RESULTS, NOTES = [], []
MIB = 1 << 20
LAYOUT = (("bootconfig", 16), ("boot_a", 40), ("boot_b", 40), ("system_a", 256), ("system_b", 256), ("persistent", 80))
FORMAT = os.environ.get("RAUC_BUNDLE_FORMAT", "verity")   # plain — rauc 1.13 на ядре с иным статусом dm-verity
ENV = {**os.environ, "PYTHONPATH": str(W / "software"), "PYTHONDONTWRITEBYTECODE": "1"}


def check(name, ok, detail=""):
    RESULTS.append({"name": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)[:600]})
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f" — {str(detail)[:600]}" if detail and not ok else ""), flush=True)
    return ok


def note(text):
    NOTES.append(text)
    print(f"NOTE {text}", flush=True)


def sh(*cmd, check_rc=True, env=None, timeout=600):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, env=env or ENV, timeout=timeout)
    if check_rc and r.returncode:
        raise RuntimeError(f"{cmd}: rc={r.returncode} {r.stderr.strip()[-800:]}")
    return r


def sha(path, size=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        left = size
        while left is None or left > 0:
            chunk = f.read(8 * MIB if left is None else min(8 * MIB, left))
            if not chunk:
                break
            h.update(chunk)
            if left is not None:
                left -= len(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- образы


def make_images(version, out: Path, filler_mib):
    """boot.vfat и system.ext4 версии `version` (как артефакты image-rota: /var системного раздела почти пуст,
    образец /var — var-seed.tar.gz внутри образа)."""
    out.mkdir(parents=True, exist_ok=True)
    boot = out / "boot.vfat"
    boot.unlink(missing_ok=True)
    sh("mkfs.vfat", "-C", "-n", "BOOT", boot, 32 * 1024)
    for name, text in (("config.txt", "[all]\narm_64bit=1\n"), ("sheregesh-version.txt", f"{version}\n")):
        (out / name).write_text(text)
        sh("mcopy", "-i", boot, out / name, f"::/{name}")
    root = out / "rootfs"
    shutil.rmtree(root, ignore_errors=True)
    for d in ("etc", "usr/share/sheregesh", "usr/lib", "var/cache", "var/log", "var/tmp"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "etc/os-release").write_text(f'PRETTY_NAME="Sheregesh TEST {version}"\n')
    (root / "usr/share/sheregesh/build.json").write_text(json.dumps({"ota_version": version, "commit": "f" * 40}))
    seed = out / "seed"
    shutil.rmtree(seed, ignore_errors=True)
    for rel, text in (("lib/dpkg/status", f"image {version}\n"), ("lib/systemd/catalog/database", "cat\n"),
                      ("lib/sheregesh/config.json", '{"rev": 0, "from": "seed"}')):
        (seed / rel).parent.mkdir(parents=True, exist_ok=True)
        (seed / rel).write_text(text)
    sh("tar", "-czf", root / "usr/share/sheregesh/var-seed.tar.gz", "-C", seed, ".")
    with open(root / "usr/lib/filler.bin", "wb") as f:            # окно для обрыва записи
        f.write(os.urandom(filler_mib * MIB))
    system = out / "system.ext4"
    system.unlink(missing_ok=True)
    sh("mkfs.ext4", "-q", "-F", "-L", "system", "-E", "root_owner=0:0", "-d", root, system, "192M")
    return boot, system


# ---------------------------------------------------------------- «плата»


class Board:
    def __init__(self, loop):
        self.loop = loop
        self.p = {n: f"{loop}p{n}" for n in range(1, 7)}
        self.bs = W / "by-slot"
        self.mnt = W / "mnt"
        self.pers = self.mnt / "persistent"
        self.bootfs = self.mnt / "bootfs"
        self.root = W / "root"
        self.boots = 0
        self.svc = None
        self.slot = None
        self.cmdline_root = True     # как на плате: root=/dev/disk/by-slot/active/system (иначе — get-current)

    def boot(self, slot, tryboot):
        other = "b" if slot == "a" else "a"
        pn = {"a": (2, 4), "b": (3, 5)}
        shutil.rmtree(self.bs, ignore_errors=True)
        for k, s in (("active", slot), ("other", other)):
            (self.bs / k).mkdir(parents=True)
            os.symlink(self.p[pn[s][0]], self.bs / k / "boot")
            os.symlink(self.p[pn[s][1]], self.bs / k / "system")
        os.symlink(self.p[1], self.bs / "bootconfig")
        os.symlink(self.p[6], self.bs / "persistent")
        chosen = W / "chosen"
        chosen.mkdir(exist_ok=True)
        (chosen / "partition").write_bytes(struct.pack(">I", pn[slot][0]))
        (chosen / "tryboot").write_bytes(struct.pack(">I", int(tryboot)))
        (W / "mountinfo").write_text(f"22 1 7:4 / / ro - ext4 {self.p[pn[slot][1]]} ro\n"
                                     f"24 22 7:1 / /bootfs rw - vfat {self.p[1]} rw\n"
                                     f"25 22 7:6 / /persistent rw - ext4 {self.p[6]} rw\n")
        self.boots += 1
        (W / "boot_id").write_text(f"boot-{self.boots}\n")
        # /proc/cmdline контейнера подменён (bind): RAUC 1.13 ищет загруженный слот по rauc.slot=, затем root=
        # (realpath), и только без root= спрашивает бэкенд custom (get-current)
        (W / "cmdline").write_text("console=tty1 rootwait" + (f" root={self.bs}/active/system" if self.cmdline_root else "") + "\n")
        self.slot = slot
        self.write_conf()

    def firmware_boot(self, tryboot_flag):
        """Прошивка Pi 4: tryboot-флаг → [tryboot], иначе [all] (флаг однократен)."""
        default, trial = slots.autoboot_parts((self.bootfs / "autoboot.txt").read_text())
        part = trial if tryboot_flag else default
        self.stop_service()
        self.boot({2: "a", 3: "b"}[part], tryboot_flag)
        self.start_service()

    def paths(self):
        return slots.Paths(by_slot=str(self.bs), sys_block="/sys/class/block", mountinfo=str(W / "mountinfo"),
                           chosen=str(W / "chosen"), bootfs=str(self.bootfs), persistent=str(self.pers),
                           var=str(self.pers / f"slots/system_{self.slot}/var"))

    def conf_dict(self):
        return {"paths": vars(self.paths()), "state_dir": str(self.pers / "sheregesh-rauc"),
                "rauc_system_conf": str(W / "rauc/system.conf"), "boot_id": str(W / "boot_id"), "root": str(self.root),
                "reboot_cmd": ["true"], "tryboot_cmd": ["true"], "health_timeout": 0, "lab_video": False}

    def write_conf(self):
        (W / "rauc-conf.json").write_text(json.dumps(self.conf_dict()))

    def cfg(self):
        return rauc_backend.Config.load(W / "rauc-conf.json")

    def autoboot(self):
        return slots.autoboot_parts((self.bootfs / "autoboot.txt").read_text())

    def backend(self):
        return rauc_backend.TrybootBackend(self.cfg())

    # ---------- rauc service ----------
    def start_service(self):
        log = open(W / f"rauc-service-{self.boots}.log", "w")
        cmd = ["rauc", "service", f"--conf={W / 'rauc/system.conf'}", f"--mount={W / 'rauc-mnt'}"]
        env = {**ENV, rauc_backend.CONF_ENV: str(W / "rauc-conf.json")}
        self.svc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env)
        for _ in range(100):
            if sh("rauc", "status", check_rc=False).returncode == 0:
                return
            time.sleep(0.1)
        raise RuntimeError("rauc service не поднялся: " + (W / f"rauc-service-{self.boots}.log").read_text()[-800:])

    def stop_service(self):
        if self.svc and self.svc.poll() is None:
            self.svc.terminate()
            try:
                self.svc.wait(10)
            except subprocess.TimeoutExpired:
                self.svc.kill()
                self.svc.wait()
        self.svc = None

    def status(self):
        r = sh("rauc", "status", "--detailed", "--output-format=json", check_rc=False)
        try:
            st = json.loads(r.stdout)
        except ValueError:
            return {"error": r.stdout + r.stderr}
        primary = st.get("boot_primary")
        slots_ = {k: v for d in st.get("slots", []) for k, v in d.items()}
        st["primary_bootname"] = (slots_.get(primary) or {}).get("bootname", primary)
        st["by_name"] = slots_
        # «booted» RAUC — строка, по которой найден слот (bootname или устройство из root=); слот — state=booted
        st["booted_bootname"] = next((v.get("bootname") for v in slots_.values() if v.get("state") == "booted"), None)
        return st


class StubProbe(rauc_hooks.RaucProbe):
    """Факты загруженного слота для критериев confirm.gates: версия — из build.json настоящего раздела system
    загруженного слота; службы, health и mTLS — заданы (systemd в контейнере нет)."""

    def __init__(self, board, health="PASS"):
        super().__init__(board.root)
        self.b, self.verdict = board, health

    def build(self):
        dev = os.path.realpath(self.b.bs / "active/system")
        d = W / "probe-mnt"
        d.mkdir(exist_ok=True)
        sh("mount", "-o", "ro,noload", "-t", "ext4", dev, d)
        try:
            return json.loads((d / "usr/share/sheregesh/build.json").read_text())
        finally:
            sh("umount", d)

    def mount_opts(self, target):
        return {"/persistent": f"{self.b.p[6]} rw,noatime", "/home": f"{self.b.p[6]}[/home] rw",
                "/etc/ssh": f"{self.b.p[6]}[/shared/etc/ssh] rw"}[target]

    def unit(self, name):
        super_name = rauc_hooks.MANAGER_UNIT if name == "sheregesh-ota.socket" else name
        self.seen = getattr(self, "seen", []) + [super_name]
        return {"ActiveState": "active", "SubState": "running", "NRestarts": "0"}

    def failed_units(self):
        return []

    def uptime(self):
        return 300.0

    def health(self, role_name):
        return {"verdict": self.verdict, "counts": {}, "checks": [{"id": c, "status": "PASS"} for c in confirm.NET_CHECKS]}

    def json_file(self, path):
        return {"/run/sheregesh-ui/link-status.json": {"phase": "connected", "tls": "TLSv1.3"}}.get(str(path), {})


# ---------------------------------------------------------------- подготовка


def setup():
    img = W / "disk.img"
    total = sum(s for _, s in LAYOUT) + 4
    with open(img, "wb") as f:
        f.truncate(total * MIB)
    script = "label: gpt\n" + "".join(f"size={s}MiB, name={n}\n" for n, s in LAYOUT)
    subprocess.run(["sfdisk", "-q", str(img)], input=script, text=True, check=True, capture_output=True)
    loop = sh("losetup", "-P", "--find", "--show", img).stdout.strip()
    for _ in range(50):
        if all(os.path.exists(f"{loop}p{n}") for n in range(1, 7)):
            break
        time.sleep(0.2)
    b = Board(loop)
    labels = {n: Path(f"/sys/class/block/{os.path.basename(b.p[n])}/uevent").read_text() for n in range(1, 7)}
    check("диск: GPT image-rota, PARTNAME у loop-разделов",
          all(f"PARTNAME={name}" in labels[n] for n, (name, _) in zip(range(1, 7), LAYOUT)))
    sh("mkfs.vfat", "-n", "BOOTCONFIG", b.p[1])
    sh("mkfs.ext4", "-q", "-F", "-L", "persistent", b.p[6])
    for d in (b.pers, b.bootfs):
        d.mkdir(parents=True, exist_ok=True)
    sh("mount", "-t", "vfat", b.p[1], b.bootfs)
    sh("mount", "-t", "ext4", b.p[6], b.pers)
    (W / "cmdline").write_text("console=tty1\n")
    sh("mount", "--bind", W / "cmdline", "/proc/cmdline")
    slots.write_atomic(b.bootfs / "autoboot.txt", slots.render_autoboot(2, 3))
    # Слот A — версия 0.1.0, как на плате после записи карты
    boot1, sys1 = make_images("0.1.0", W / "img-0.1.0", 8)
    sh("dd", f"if={boot1}", f"of={b.p[2]}", "bs=4M", "conv=fsync", "status=none")
    sh("dd", f"if={sys1}", f"of={b.p[4]}", "bs=4M", "conv=fsync", "status=none")
    # persistent, как его оставляет image-rota + выпуск ключей стенда
    for rel, text in (("slots/system_a/var/lib/dpkg/status", "image 0.1.0\n"),
                      ("slots/system_a/var/lib/sheregesh/config.json", '{"rev": 7}'),
                      ("home/sheregesh-ui/.ssh/authorized_keys", "ssh-ed25519 AAAA owner\n"),
                      ("home/sheregesh-ui/.config/sheregesh/link.json", '{"role": "console"}\n'),
                      ("home/sheregesh-ui/.config/sheregesh/console.role", "u\n"),
                      ("home/sheregesh-ui/.local/share/sheregesh/link/device.key", "TEST-ONLY-NOT-A-KEY\n"),
                      ("home/sheregesh-ui/.local/share/sheregesh/link/device.crt", "CERT\n"),
                      ("shared/etc/ssh/ssh_host_ed25519_key.pub", "ssh-ed25519 HOSTKEY-PUB\n"),
                      ("common/etc/machine-id", "0123456789abcdef0123456789abcdef\n")):
        (b.pers / rel).parent.mkdir(parents=True, exist_ok=True)
        (b.pers / rel).write_text(text)
    (b.pers / "slots/system_b/var").mkdir(parents=True)
    (b.pers / "rauc").mkdir()
    (b.root / "etc").mkdir(parents=True, exist_ok=True)
    os.symlink(b.pers / "home", b.root / "home")
    os.symlink(b.pers / "shared/etc/ssh", b.root / "etc/ssh")
    os.symlink(b.pers / "common/etc/machine-id", b.root / "etc/machine-id")
    # RAUC: system.conf репозитория с подстановкой устройств loop и путей /work
    conf = (W / "tools/rauc/system.conf").read_text()
    for n in range(1, 7):
        conf = conf.replace(f"/dev/mmcblk0p{n}", b.p[n])
    if FORMAT == "plain":
        conf = conf.replace("bundle-formats=-plain", "bundle-formats=plain")
        note("испытательный system.conf: bundle-formats=plain (выпускной — только verity)")
    conf = (conf.replace("/persistent/rauc", str(b.pers / "rauc")).replace("/etc/rauc/keyring.pem", str(W / "pki/keyring.pem"))
            .replace("/usr/libexec/sheregesh/", str(W / "rauc/bin") + "/"))
    (W / "rauc/bin").mkdir(parents=True, exist_ok=True)
    (W / "rauc/system.conf").write_text(conf)
    for name, mod, arg in (("rauc-tryboot-backend", "rauc_backend", '"$@"'), ("rauc-pre-install", "rauc_hooks", "pre-install"),
                           ("rauc-post-install", "rauc_hooks", "post-install")):
        f = W / "rauc/bin" / name
        f.write_text(f"#!/bin/sh\nexport PYTHONPATH={W / 'software'} {rauc_backend.CONF_ENV}={W / 'rauc-conf.json'}\n"
                     f"exec python3 -m sheregesh.update.{mod} {arg}\n")
        f.chmod(0o755)
    return b


def bundles():
    pki = testpki.make(W / "pki")
    boot2, sys2 = make_images("0.2.0", W / "img-0.2.0", 120)
    out = W / "bundles"
    out.mkdir(exist_ok=True)
    made = {}
    for name, cert, compat in (("good", "signing", "sheregesh-lab-pi4-dev-1"), ("foreign", "foreign", None),
                               ("nocodesign", "nocodesign", None), ("wrongcompat", "signing", "sheregesh-lab-pi4-1")):
        args = ["--boot", boot2, "--system", sys2, "--version", "0.2.0", "--build", "f" * 40, "--format", FORMAT,
                "--compatible", compat or "sheregesh-lab-pi4-dev-1", "--cert", pki / f"{cert}.cert.pem",
                "--key", pki / f"{cert}.key.pem", "--out", out / f"{name}.raucb"]
        if cert == "signing":
            args += ["--keyring", pki / "keyring.pem"]
        t = time.monotonic()
        rc = build_bundle.main([str(a) for a in args])
        check(f"сборка бандла {name}", rc == 0 and (out / f"{name}.raucb").exists(), f"rc={rc}")
        made[name] = out / f"{name}.raucb"
        if name == "good":
            note(f"бандл good: {made[name].stat().st_size} байт, сборка {time.monotonic() - t:.1f} с")
    info = sh("rauc", f"--conf={W / 'rauc/system.conf'}", "info", "--output-format=json", made["good"], check_rc=False)
    check("rauc info: подпись тестового CA, compatible, verity", info.returncode == 0
          and '"sheregesh-lab-pi4-dev-1"' in info.stdout and FORMAT in info.stdout, info.stdout[-300:] + info.stderr[-300:])
    tampered = out / "tampered.raucb"
    shutil.copyfile(made["good"], tampered)
    with open(tampered, "r+b") as f:                  # байты внутри squashfs (данные system.ext4), подпись цела
        for off in (40 * MIB, 80 * MIB):
            f.seek(off)
            byte = f.read(1)
            f.seek(off)
            f.write(bytes([byte[0] ^ 0xFF]))
    made["tampered"] = tampered
    return made, boot2, sys2


def cleanup_rauc_leftovers():
    """После SIGKILL rauc service бандл остаётся смонтированным: снять только своё (mount prefix /work/rauc-mnt,
    dm-устройства с «rauc» в имени, loop над /work/bundles)."""
    for line in reversed(Path("/proc/self/mounts").read_text().splitlines()):
        mp = line.split()[1]
        if mp.startswith(str(W / "rauc-mnt")):
            sh("umount", "-l", mp, check_rc=False)
    r = sh("dmsetup", "ls", check_rc=False)
    for line in r.stdout.splitlines():
        name = line.split()[0] if line.split() else ""
        if "rauc" in name:
            sh("dmsetup", "remove", "--force", name, check_rc=False)
    for line in sh("losetup", "-a", check_rc=False).stdout.splitlines():
        if str(W / "bundles") in line:
            sh("losetup", "-d", line.split(":")[0], check_rc=False)


def install(bundle, timeout=600):
    t = time.monotonic()
    r = sh("rauc", "install", bundle, check_rc=False, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip(), time.monotonic() - t


def unchanged(b, base, tag):
    check(f"{tag}: слот A (boot и system) не изменён", sha(b.p[2]) == base["p2"] and sha(b.p[4]) == base["p4"])
    check(f"{tag}: autoboot.txt не изменён ([all]=2, [tryboot]=3)", b.autoboot() == (2, 3), b.autoboot())
    st = b.backend().load()
    check(f"{tag}: пробная загрузка не назначена", st["trial"] is None, st)


def main():
    t0 = time.monotonic()
    b = None
    try:
        b = setup()
        made, boot2, sys2 = bundles()
        want = {"boot": sha(boot2), "system": sha(sys2), "boot_size": boot2.stat().st_size,
                "system_size": sys2.stat().st_size}
        base = {"p2": sha(b.p[2]), "p4": sha(b.p[4])}
        shared = {p: sha(b.pers / p) for p in ("home/sheregesh-ui/.ssh/authorized_keys", "common/etc/machine-id",
                                                "home/sheregesh-ui/.local/share/sheregesh/link/device.key",
                                                "shared/etc/ssh/ssh_host_ed25519_key.pub")}
        b.cmdline_root = False
        b.boot("a", tryboot=False)
        b.start_service()
        st = b.status()
        check("rauc status без root= в cmdline: загружен A по get-current бэкенда", st.get("booted_bootname") == "A",
              {k: st.get(k) for k in ("booted", "booted_bootname", "error")})
        b.stop_service()
        b.cmdline_root = True
        b.boot("a", tryboot=False)
        b.start_service()
        st = b.status()
        check("rauc status (root=by-slot/active/system, как на плате): загружен A, основной A (get-primary), compatible",
              st.get("booted_bootname") == "A" and st.get("primary_bootname") == "A"
              and st.get("compatible") == "sheregesh-lab-pi4-dev-1", {k: st.get(k) for k in ("booted", "booted_bootname", "boot_primary", "compatible", "error")})
        check("rauc status: boot.0/boot.1 — зависимые слоты rootfs.0/rootfs.1",
              (st["by_name"].get("boot.1") or {}).get("parent") == "rootfs.1", st.get("by_name", {}).get("boot.1"))

        # --- отказ: подпись, назначение сертификата, compatible, повреждённые данные
        for name, why, reason in (("foreign", "посторонний CA", "signature"),
                                  ("nocodesign", "сертификат без codeSigning", "purpose"),
                                  ("wrongcompat", "compatible sheregesh-lab-pi4-1", "compatible"),
                                  ("tampered", "повреждены данные бандла", "")):
            before_b = (sha(b.p[3]), sha(b.p[5]))
            rc, out, dt = install(made[name])
            last = next((x for x in out.splitlines() if x.startswith("LastError")), out[-300:])
            check(f"install {name} ({why}) → отказ RAUC по причине «{reason or 'данные'}»",
                  rc != 0 and reason.lower() in out.lower() and "dm-verity status" not in out, last)
            print(f"     rauc: {last}", flush=True)
            if name != "tampered":
                check(f"install {name}: слот B не тронут (отказ до записи)", (sha(b.p[3]), sha(b.p[5])) == before_b)
            else:
                check("install tampered: B помечен плохим (запись могла начаться)", b.backend().get_state("B") == "bad")
            unchanged(b, base, f"install {name}")

        # --- охранник: незавершённое обновление dev-агента
        ag = b.pers / "sheregesh-ota"
        ag.mkdir(exist_ok=True)
        (ag / "state.json").write_text(json.dumps({"state": "INSTALLED", "active": "A"}))
        rc, out, _ = install(made["good"])
        svc_log = (W / f"rauc-service-{b.boots}.log").read_text()
        refusal = next((x for x in svc_log.splitlines() if "ОТКАЗ" in x), "")
        check("охранник: агент в INSTALLED → RAUC pre-install отказывает (журнал rauc service)",
              rc != 0 and "Pre-install handler" in out and "sheregesh-ota" in refusal, refusal or out[-300:])
        print(f"     {refusal.strip()}", flush=True)
        unchanged(b, base, "охранник RAUC")
        (ag / "state.json").write_text(json.dumps({"state": "IDLE", "active": "A"}))
        cfg = ota.AgentConfig(paths=b.paths(), state_dir=ag, signers=W / "nonexistent", root=b.root)
        GA = guard.guarded_agent_class()
        import io
        res = GA(cfg, guard=guard.Guard(guard.GuardPaths.under(b.pers, W / "rauc/system.conf", W / "boot_id"))).handle(
            {"action": "install"}, io.BytesIO(made["good"].read_bytes()[:4096]), lambda m: None)
        check("охранник: при system.conf RAUC агент (GuardedAgent) отказывает в install", res.get("result") == "REFUSED", res)

        # --- обрыв записи: SIGKILL rauc service, пока открыт раздел слота B
        targets = {os.path.realpath(b.p[5])}                          # system B (boot B пишется раньше и короче)
        client = subprocess.Popen(["rauc", "install", str(made["good"])], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, env=ENV)
        hit, deadline = None, time.monotonic() + 120
        while time.monotonic() < deadline and hit is None and client.poll() is None:
            try:
                for fd in os.listdir(f"/proc/{b.svc.pid}/fd"):
                    try:
                        if os.path.realpath(os.readlink(f"/proc/{b.svc.pid}/fd/{fd}")) in targets:
                            hit = os.readlink(f"/proc/{b.svc.pid}/fd/{fd}")
                    except OSError:
                        pass
            except OSError:
                break
            if hit is None:
                time.sleep(0.002)
        if hit:
            os.kill(b.svc.pid, signal.SIGKILL)
            b.svc.wait()
        else:
            b.stop_service()
        client.wait(30)
        check("обрыв: rauc service убит (SIGKILL) во время записи слота B", hit is not None, f"fd={hit}")
        note(f"обрыв: запись шла в {hit}; клиент rauc install rc={client.returncode}")
        cleanup_rauc_leftovers()
        check("обрыв: B помечен плохим (set-state bad до записи)", b.backend().get_state("B") == "bad")
        unchanged(b, base, "обрыв")
        check("обрыв: system B не равен образу (запись не завершена)", sha(b.p[5], want["system_size"]) != want["system"])
        b.boot("a", tryboot=False)                                    # отключение питания → прошивка грузит [all]
        b.start_service()
        r = sh("python3", "-m", "sheregesh.update.rauc_hooks", "tryboot", check_rc=False,
               env={**ENV, rauc_backend.CONF_ENV: str(W / "rauc-conf.json")})
        check("обрыв: sheregesh-rauc tryboot отказывает (пробная загрузка не назначена)", r.returncode == 1, r.stderr[-200:])
        claim = guard.Guard(guard.GuardPaths.under(b.pers, W / "rauc/system.conf", W / "boot_id")).claim_info()
        check("обрыв: заявка RAUC прошлой загрузки не держит слоты", claim is None or not claim["live"], claim)

        # --- полная установка
        rc, out, dt = install(made["good"])
        check(f"install good → OK ({dt:.1f} с)", rc == 0, out[-500:])
        note(f"install good: {dt:.1f} с; {out.splitlines()[-1] if out else ''}")
        check("install: boot B = boot.vfat бандла (SHA-256)", sha(b.p[3], want["boot_size"]) == want["boot"])
        check("install: system B = system.ext4 бандла (SHA-256)", sha(b.p[5], want["system_size"]) == want["system"])
        check("install: слот A не изменён", sha(b.p[2]) == base["p2"] and sha(b.p[4]) == base["p4"])
        check("install: autoboot.txt [all]=2 (основной не изменён), [tryboot]=3", b.autoboot() == (2, 3), b.autoboot())
        bst = b.backend().load()
        check("install: RAUC set-primary B → назначена пробная загрузка B", bst["trial"] == {"slot": "B", "attempts": 0}, bst)
        st = b.status()
        check("rauc status после установки: основной A, B «bad» до подтверждения",
              st.get("primary_bootname") == "A" and st["by_name"]["rootfs.1"].get("boot_status") == "bad",
              {"primary": st.get("boot_primary"), "B": st["by_name"].get("rootfs.1")})
        rec = json.loads((b.pers / "sheregesh-rauc/install.json").read_text())
        check("post-install: install.json — цель B, версия 0.2.0, отпечатки",
              rec["target"] == "B" and rec["version"] == "0.2.0" and rec["expect"]["machine_id"], {k: rec[k] for k in ("target", "version")})
        var_b = b.pers / "slots/system_b/var"
        check("post-install: /var B из образца нового образа (lib/dpkg, lib/systemd)",
              (var_b / "lib/dpkg/status").read_text() == "image 0.2.0\n" and (var_b / "lib/systemd/catalog/database").exists())
        check("post-install: конфигурация пульта перенесена с A (rev 7, не образец)",
              json.loads((var_b / "lib/sheregesh/config.json").read_text()) == {"rev": 7})
        check("post-install: /var A не изменён", (b.pers / "slots/system_a/var/lib/dpkg/status").read_text() == "image 0.1.0\n")

        # --- пробная загрузка 1: отказ health → mark-bad → откат
        (b.pers / "slots/system_a/var/lib/sheregesh/config.json").write_text('{"rev": 8}')
        hooks = rauc_hooks.Hooks(b.cfg(), probe=StubProbe(b, "FAIL"))
        msg = hooks.tryboot()
        check("tryboot: попытка 1/2, свежая конфигурация (rev 8) перенесена", "1/2" in msg
              and json.loads((var_b / "lib/sheregesh/config.json").read_text()) == {"rev": 8}, msg)
        b.firmware_boot(tryboot_flag=True)
        st = b.status()
        check("пробная загрузка: RAUC видит загруженный B, основной A", st.get("booted_bootname") == "B" and st.get("primary_bootname") == "A",
              {k: st.get(k) for k in ("booted", "booted_bootname", "boot_primary")})
        res = rauc_hooks.Hooks(b.cfg(), probe=StubProbe(b, "FAIL")).health()
        check("health FAIL → rauc status mark-bad booted (rc 0), результат ROLLED_BACK",
              res["result"] == "ROLLED_BACK" and res["rauc_rc"] == 0 and "health" in res["failed"], {k: res[k] for k in ("result", "failed", "rauc_rc", "rauc")})
        check("после mark-bad: [all]=2, проба снята, B bad", b.autoboot() == (2, 3) and b.backend().load()["trial"] is None
              and b.backend().get_state("B") == "bad")
        b.firmware_boot(tryboot_flag=False)                        # перезагрузка из health (обычная)
        st = b.status()
        check("откат: прошивка загрузила A, RAUC: загружен A, основной A", st.get("booted_bootname") == "A" and st.get("primary_bootname") == "A",
              {k: st.get(k) for k in ("booted", "booted_bootname", "boot_primary")})
        check("откат: слот A не изменён", sha(b.p[2]) == base["p2"] and sha(b.p[4]) == base["p4"])

        # --- повторная установка, отключение питания на пробе, вторая попытка, health PASS → mark-good
        rc, out, dt = install(made["good"])
        check(f"повторная install good → OK ({dt:.1f} с)", rc == 0, out[-300:])
        rauc_hooks.Hooks(b.cfg()).tryboot()
        b.firmware_boot(tryboot_flag=True)
        b.firmware_boot(tryboot_flag=False)                         # питание пропало на пробной загрузке
        st = b.status()
        check("питание на пробе: загружен A, основной A, попытка 1 учтена", st.get("booted_bootname") == "A"
              and b.backend().load()["trial"] == {"slot": "B", "attempts": 1})
        print("     " + rauc_hooks.Hooks(b.cfg()).on_boot(), flush=True)
        msg = rauc_hooks.Hooks(b.cfg()).tryboot()
        check("tryboot: попытка 2/2", "2/2" in msg, msg)
        b.firmware_boot(tryboot_flag=True)
        probe = StubProbe(b, "PASS")
        res = rauc_hooks.Hooks(b.cfg(), probe=probe).health()
        check("health PASS (confirm.gates: slot, version 0.2.0 из build.json B, same.*, health) → rauc mark-good booted",
              res["result"] == "COMMITTED" and res["rauc_rc"] == 0, {k: res[k] for k in ("result", "failed", "rauc_rc", "rauc")})
        check("критерий менеджера обновлений проверял rauc.service", "rauc.service" in getattr(probe, "seen", []))
        check("mark-good: основной слот сменился в autoboot.txt ([all]=3, [tryboot]=2)", b.autoboot() == (3, 2), b.autoboot())
        st = b.status()
        check("rauc status: основной B", st.get("primary_bootname") == "B", st.get("boot_primary"))
        b.firmware_boot(tryboot_flag=False)
        st = b.status()
        check("обычная перезагрузка: загружен B, основной B, B good, A good (запасной)",
              st.get("booted_bootname") == "B" and st.get("primary_bootname") == "B"
              and st["by_name"]["rootfs.1"].get("boot_status") == "good" and st["by_name"]["rootfs.0"].get("boot_status") == "good",
              {k: st.get(k) for k in ("booted", "booted_bootname", "boot_primary")})
        check("итог: слот A (запасной) не изменён", sha(b.p[2]) == base["p2"] and sha(b.p[4]) == base["p4"])
        check("итог: общие данные persistent не изменились", {p: sha(b.pers / p) for p in shared} == shared)
        print(json.dumps({"rauc_status_final": {k: st.get(k) for k in ("compatible", "booted", "boot_primary")},
                          "backend": b.backend().status()}, ensure_ascii=False), flush=True)
    except Exception as e:  # noqa: BLE001 — итог должен быть записан
        import traceback
        traceback.print_exc()
        check("сценарий выполнен без исключений", False, repr(e))
    finally:
        if b:
            b.stop_service()
            cleanup_rauc_leftovers()
            for d in (W / "probe-mnt", b.bootfs, b.pers, "/proc/cmdline"):
                sh("umount", d, check_rc=False)
            sh("losetup", "-d", b.loop, check_rc=False)
        for f in sorted(W.glob("rauc-service-*.log")):
            text = f.read_text()
            interesting = [x for x in text.splitlines() if any(k in x.lower() for k in
                           ("handler", "backend", "mark", "primary", "error", "fail", "verif", "signature", "install"))]
            print(f"--- {f.name} ({len(text.splitlines())} строк), выборка:\n" + "\n".join(interesting[-40:]), flush=True)
    passed = sum(r["status"] == "PASS" for r in RESULTS)
    summary = {"passed": passed, "total": len(RESULTS), "seconds": round(time.monotonic() - t0, 1),
               "rauc": sh("rauc", "--version", check_rc=False).stdout.strip(), "results": RESULTS, "notes": NOTES}
    (W / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"ИТОГ: {passed}/{len(RESULTS)} PASS за {summary['seconds']} с ({summary['rauc']})", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
