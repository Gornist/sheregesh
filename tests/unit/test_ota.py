# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""OTA A/B на файлах-разделах (S3.1-DEPLOY): определение слотов, запись только неактивного слота, tryboot,
подтверждение по health, откат, сохранность persistent. Без root, без блочных устройств и без перезагрузок:
разделы — файлы, /dev/disk/by-slot, sysfs, mountinfo и device-tree — каталоги во временном каталоге,
прошивка tryboot — функция firmware_boot() по autoboot.txt.
"""
import hashlib
import io
import json
import lzma
import os
import socket
import struct
import tempfile
import threading
from pathlib import Path

import pytest
from deploy_helpers import keypair, needs_ssh_keygen, signers_file

from sheregesh.deploy import sig
from sheregesh.update import confirm, ota, slots
from sheregesh.update.model import UpdateError

PARTS = {1: ("bootconfig", 1 << 16), 2: ("boot_a", 1 << 20), 3: ("boot_b", 1 << 20), 4: ("system_a", 2 << 20),
         5: ("system_b", 2 << 20), 6: ("persistent", 1 << 16)}
SERIAL = "10000000568152de"


def h(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def image(kind, version, size):
    head = f"{kind.upper()}-v{version}\n".encode()
    return (head * (size // len(head) // 2 + 1))[: size // 2]          # половина раздела: хвост не трогается


class Board:
    def __init__(self, tmp, dev_mode=True):
        self.t = Path(tmp)
        self.dev = self.t / "dev"
        (self.dev / "disk/by-slot").mkdir(parents=True)
        for n, (label, size) in PARTS.items():
            f = self.dev / f"mmcblk0p{n}"
            f.write_bytes(image(label.split("_")[0], "1.0.0", size) + b"\0" * (size - len(image("x", "1", size))))
            d = self.t / f"sys/devices/mmcblk0/mmcblk0p{n}"
            d.mkdir(parents=True)
            (d / "uevent").write_text(f"MAJOR=179\nDEVNAME=mmcblk0p{n}\nPARTN={n}\nPARTNAME={label}\n")
            (self.t / "sys/class/block").mkdir(parents=True, exist_ok=True)
            os.symlink(d, self.t / f"sys/class/block/mmcblk0p{n}")
        self.bootfs = self.t / "bootfs"
        self.bootfs.mkdir()
        (self.bootfs / "autoboot.txt").write_text(slots.render_autoboot(2, 3))
        self.persistent = self.t / "persistent"
        for s in ("a", "b"):
            (self.persistent / f"slots/system_{s}/var/lib/dpkg").mkdir(parents=True)
            (self.persistent / f"slots/system_{s}/var/lib/dpkg/status").write_text("old image 1.0.0\n")
        (self.persistent / "home/sheregesh-ui/.ssh").mkdir(parents=True)
        (self.persistent / "home/sheregesh-ui/.ssh/authorized_keys").write_text("ssh-ed25519 AAAA owner\n")
        (self.persistent / "shared/etc/ssh").mkdir(parents=True)
        (self.persistent / "shared/etc/ssh/ssh_host_ed25519_key").write_text("HOSTKEY\n")
        (self.persistent / "shared/etc/ssh/ssh_host_ed25519_key.pub").write_text("ssh-ed25519 HOSTKEY-PUB\n")
        (self.persistent / "home/sheregesh-ui/.local/share/sheregesh/link").mkdir(parents=True)
        (self.persistent / "home/sheregesh-ui/.local/share/sheregesh/link/device.crt").write_text("CERT\n")
        (self.persistent / "common/etc").mkdir(parents=True)
        (self.persistent / "common/etc/machine-id").write_text("0123456789abcdef0123456789abcdef\n")
        # Корень, как его видит система: /home, /etc/ssh, /etc/machine-id — с persistent (генераторы image-rota)
        self.root = self.t / "root"
        (self.root / "etc").mkdir(parents=True)
        os.symlink(self.persistent / "home", self.root / "home")
        os.symlink(self.persistent / "shared/etc/ssh", self.root / "etc/ssh")
        os.symlink(self.persistent / "common/etc/machine-id", self.root / "etc/machine-id")
        self.units = {}                          # имя → {"ActiveState", "NRestarts"} поверх «всё active»
        self.status_files = {}
        self.uptime = 300.0
        self.etc = self.t / "etc"
        self.etc.mkdir()
        if dev_mode:
            (self.etc / "DEV-MODE").write_text("dev\n")
        (self.t / "serial").write_bytes(SERIAL.encode() + b"\0")
        (self.t / "build.json").write_text(json.dumps({"ota_version": "1.0.0"}))
        self.images_var = {}                     # версия образа → дерево /var этого образа
        self.key = keypair(self.t, "owner")
        self.signers = signers_file(self.t, self.key)
        self.commands = []
        self.health = "PASS"
        self.boot("a", tryboot=False)

    # ---------- «железо» ----------
    def boot(self, slot, tryboot):
        other = "b" if slot == "a" else "a"
        pn = {"a": (2, 4), "b": (3, 5)}
        bs = self.dev / "disk/by-slot"
        for k in ("active", "other"):
            (bs / k).mkdir(exist_ok=True)
            for i, part in enumerate(("boot", "system")):
                link = bs / k / part
                if link.is_symlink():
                    link.unlink()
                os.symlink(self.dev / f"mmcblk0p{pn[slot if k == 'active' else other][i]}", link)
        for name, n in (("bootconfig", 1), ("persistent", 6)):
            if not (bs / name).is_symlink():
                os.symlink(self.dev / f"mmcblk0p{n}", bs / name)
        b, s = pn[slot]
        self.mountinfo = self.t / "mountinfo"
        self.mountinfo.write_text(
            f"22 1 179:{s} / / ro,relatime - ext4 {bs}/active/system ro\n"
            f"23 22 179:{b} / /boot/firmware ro - vfat {self.dev}/mmcblk0p{b} ro\n"
            f"24 22 179:1 / /bootfs rw - vfat {self.dev}/mmcblk0p1 rw\n"
            f"25 22 179:6 / /persistent rw - ext4 {self.dev}/mmcblk0p6 rw\n"
            f"26 22 179:6 /slots/system_{slot}/var /var rw - ext4 {self.dev}/mmcblk0p6 rw\n")
        chosen = self.t / "chosen"
        chosen.mkdir(exist_ok=True)
        (chosen / "partition").write_bytes(struct.pack(">I", b))
        (chosen / "tryboot").write_bytes(struct.pack(">I", int(tryboot)))
        self.var = self.persistent / f"slots/system_{slot}/var"
        self.slot = slot

    def firmware_boot(self, tryboot_flag):
        """Что загрузит прошивка Pi 4 по autoboot.txt (tryboot однократен)."""
        default, trial = slots.autoboot_parts((self.bootfs / "autoboot.txt").read_text())
        part = trial if tryboot_flag else default
        self.boot({2: "a", 3: "b"}[part], tryboot=tryboot_flag)

    def paths(self):
        return slots.Paths(by_slot=str(self.dev / "disk/by-slot"), sys_block=str(self.t / "sys/class/block"),
                           mountinfo=str(self.mountinfo), chosen=str(self.t / "chosen"), bootfs=str(self.bootfs),
                           persistent=str(self.persistent), var=str(self.var))

    def agent(self):
        cfg = ota.AgentConfig(paths=self.paths(), state_dir=self.persistent / "sheregesh-ota", signers=self.signers,
                              build_json=self.t / "build.json", serial=self.t / "serial", dev_mode=self.etc / "DEV-MODE",
                              health_cmd=("health",), reboot_cmd=("reboot",), tryboot_cmd=("reboot", "0 tryboot"),
                              root=self.root, probe=FakeProbe(self))
        return ota.Agent(cfg, run=self.run, seeder=TestSeeder(self))

    def run(self, cmd, **kw):
        self.commands.append(list(cmd))

        class R:
            returncode, stderr = 0, ""
            stdout = json.dumps({"verdict": self.health})
        return R()

    def request(self, action, stream=b"", **extra):
        out = []
        res = self.agent().handle({"action": action, **extra}, io.BytesIO(stream), out.append)
        return res, out

    # ---------- обновление ----------
    def payload(self, version="1.1.0", key=None, namespace=sig.NS_OTA, compatible="sheregesh-lab-pi4-dev-1",
                tamper=None, boot_size=None):
        boot = image("boot", version, boot_size or PARTS[3][1])
        system = image("system", version, PARTS[5][1])
        self.images_var[version] = {"lib/dpkg/status": f"image {version}\n"}
        imgs, blobs = [], []
        for part, raw in (("boot", boot), ("system", system)):
            comp = lzma.compress(raw, format=lzma.FORMAT_XZ)
            imgs.append({"part": part, "compression": "xz", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                         "compressed_size": len(comp), "compressed_sha256": hashlib.sha256(comp).hexdigest()})
            blobs.append(comp)
        m = {"format": 1, "kind": ota.KIND, "layout": ota.LAYOUT, "compatible": compatible, "version": version,
             "commit": "c" * 40, "images": imgs}
        raw_m = json.dumps(m).encode()
        signature = sig.sign(raw_m, key or self.key, namespace)
        if tamper == "image":
            blobs[1] = blobs[1][:-20] + bytes(20)
        buf = io.BytesIO()
        with tempfile.TemporaryDirectory() as d:
            files = []
            for i, blob in enumerate(blobs):
                f = Path(d) / f"{i}.xz"
                f.write_bytes(blob)
                files.append(f)
            ota.write_payload(buf, raw_m, signature, files)
        data = buf.getvalue()
        return data[:-100] if tamper == "truncate" else data

    def enable(self, serial=SERIAL, key=None):
        token = json.dumps({"kind": "sheregesh-ota-enable", "serial": serial, "switching": True}).encode()
        s = sig.sign(token, key or self.key, sig.NS_OTA_ENABLE)
        return self.request("enable", token=token.decode(), sig=s.decode())

    def part_hashes(self):
        return {n: h(self.dev / f"mmcblk0p{n}") for n in PARTS}

    def persistent_hashes(self):
        out = {}
        for p in sorted(self.persistent.rglob("*")):
            rel = p.relative_to(self.persistent).as_posix()
            if p.is_file() and not rel.startswith(("slots/", "sheregesh-ota/")):
                out[rel] = h(p)
        return out


class FakeProbe(confirm.Probe):
    """Факты загруженного слота тестовой платы: версия — по содержимому активного system, остальное — по полям."""

    __test__ = False

    def __init__(self, board):
        super().__init__(board.root)
        self.b = board

    def build(self):
        sysdev = Path(os.path.realpath(self.b.dev / "disk/by-slot/active/system"))
        return {"ota_version": sysdev.read_bytes()[:40].split(b"\n")[0].decode().split("-v")[1], "commit": "c" * 40}

    def mount_opts(self, target):
        return {"/persistent": "/dev/mmcblk0p6 rw,noatime", "/home": "/dev/mmcblk0p6[/home] rw",
                "/etc/ssh": "/dev/mmcblk0p6[/shared/etc/ssh] rw"}[target]

    def unit(self, name):
        return {"ActiveState": "active", "SubState": "running", "NRestarts": "0", **self.b.units.get(name, {})}

    def failed_units(self):
        return [n for n, u in self.b.units.items() if u.get("ActiveState") == "failed"]

    def uptime(self):
        return self.b.uptime

    def health(self, role_name):
        net = [{"id": c, "status": "PASS"} for c in confirm.NET_CHECKS]
        return {"verdict": self.b.health, "counts": {}, "checks": net}

    def json_file(self, path):
        return self.b.status_files.get(str(path), {})


class TestSeeder(slots.VarSeeder):
    """Вместо mount+rsync: /var «нового образа» берётся по версии, записанной в начало раздела system."""

    __test__ = False

    def __init__(self, board):
        super().__init__(board.paths())
        self.board = board

    def seed(self, layout, slot_name, system_dev):
        dst = self.target_var(layout, slot_name)
        version = Path(system_dev).read_bytes()[:40].split(b"\n")[0].decode().split("-v")[1]
        src = self.board.t / f"imgvar-{version}"
        for rel, text in self.board.images_var[version].items():
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_text(text)
        slots.copy_tree_py(src, dst)
        self.carry(dst)

    def copy_tree(self, src, dst):
        slots.copy_tree_py(src, dst)


pytestmark = needs_ssh_keygen


# ---------------------------------------------------------------- слоты


def test_detect_layout_from_independent_sources(tmp_path):
    b = Board(tmp_path)
    lay = slots.detect(b.paths())
    assert (lay.active.name, lay.other.name, lay.active.boot_part, lay.other.boot_part) == ("a", "b", 2, 3)
    assert lay.disk == "mmcblk0" and not lay.tryboot
    b.boot("b", tryboot=True)
    lay = slots.detect(b.paths())
    assert (lay.active.name, lay.other.name, lay.tryboot) == ("b", "a", True)


@pytest.mark.parametrize("breakage", ["other_mounted", "root_elsewhere", "firmware_mismatch", "labels", "same_dev"])
def test_inconsistent_slot_facts_refuse(tmp_path, breakage):
    b = Board(tmp_path)
    if breakage == "other_mounted":
        with open(b.mountinfo, "a") as f:
            f.write(f"30 22 179:5 / /mnt rw - ext4 {b.dev}/mmcblk0p5 rw\n")
    elif breakage == "root_elsewhere":
        b.mountinfo.write_text(b.mountinfo.read_text().replace(f"{b.dev}/disk/by-slot/active/system",
                                                               f"{b.dev}/mmcblk0p5"))
    elif breakage == "firmware_mismatch":
        (b.t / "chosen/partition").write_bytes(struct.pack(">I", 3))
    elif breakage == "labels":
        (b.t / "sys/devices/mmcblk0/mmcblk0p5/uevent").write_text("PARTN=5\nPARTNAME=system_a\n")
    elif breakage == "same_dev":
        link = b.dev / "disk/by-slot/other/system"
        link.unlink()
        os.symlink(b.dev / "mmcblk0p4", link)
    with pytest.raises(UpdateError):
        slots.detect(b.paths())


def test_autoboot_render_matches_rpi_slot_tryboot_format():
    assert slots.render_autoboot(2, 3) == "[all]\ntryboot_a_b=1\nboot_partition=2\n[tryboot]\nboot_partition=3\n"
    assert slots.autoboot_parts(slots.render_autoboot(3, 2)) == (3, 2)
    with pytest.raises(UpdateError):
        slots.autoboot_parts("[all]\nboot_partition=2\n")


# ---------------------------------------------------------------- установка в неактивный слот


@pytest.mark.test_id("TEST-UPD-001")
def test_install_writes_only_inactive_slot_and_keeps_persistent(tmp_path):
    b = Board(tmp_path)
    (b.var / "lib/sheregesh/config").mkdir(parents=True)
    (b.var / "lib/sheregesh/config/active").write_text("r7\n")
    before, pers = b.part_hashes(), b.persistent_hashes()
    autoboot = (b.bootfs / "autoboot.txt").read_text()
    res, msgs = b.request("install", b.payload("1.1.0"))
    assert res["result"] == "OK", res
    after = b.part_hashes()
    assert [n for n in PARTS if before[n] != after[n]] == [3, 5]               # только boot_b и system_b
    assert (b.dev / "mmcblk0p5").read_bytes().startswith(b"SYSTEM-v1.1.0")
    assert (b.bootfs / "autoboot.txt").read_text() == autoboot                 # основной слот не тронут
    assert b.persistent_hashes() == pers                                       # ключи, сертификаты, home
    new_var = b.persistent / "slots/system_b/var"
    assert (new_var / "lib/dpkg/status").read_text() == "image 1.1.0\n"         # /var нового образа
    assert (new_var / "lib/sheregesh/config/active").read_text() == "r7\n"     # конфигурация пульта перенесена
    assert (b.persistent / "slots/system_a/var/lib/dpkg/status").read_text() == "old image 1.0.0\n"
    assert res["state"]["state"] == "INSTALLED" and res["state"]["target"] == "B"
    assert b.commands == []                                                    # никаких перезагрузок
    assert any("подпись принята" in m.get("msg", "") for m in msgs)


@pytest.mark.parametrize("attack, result", [("foreign_key", "REJECTED"), ("wrong_namespace", "REJECTED"),
                                            ("strict_image", "REJECTED"), ("older", "FAILED"),
                                            ("tampered_image", "FAILED"), ("truncated", "FAILED"),
                                            ("oversize", "FAILED")])
def test_bad_updates_never_touch_active_slot(tmp_path, attack, result):
    b = Board(tmp_path)
    before = b.part_hashes()
    autoboot = (b.bootfs / "autoboot.txt").read_text()
    kw = {"foreign_key": {"key": keypair(tmp_path, "intruder")}, "wrong_namespace": {"namespace": sig.NS_APP},
          "strict_image": {"compatible": "sheregesh-lab-pi4-1"}, "older": {"version": "0.9.0"},
          "tampered_image": {"tamper": "image"}, "truncated": {"tamper": "truncate"},
          "oversize": {"boot_size": PARTS[3][1] * 4}}[attack]
    res, _ = b.request("install", b.payload(**kw))
    assert res["result"] == result, res
    after = b.part_hashes()
    assert all(before[n] == after[n] for n in (1, 2, 4, 6))
    assert (b.bootfs / "autoboot.txt").read_text() == autoboot and b.commands == []


def test_install_refused_when_state_disagrees_with_slots(tmp_path):
    b = Board(tmp_path)
    assert b.request("install", b.payload("1.1.0"))[0]["result"] == "OK"
    (b.bootfs / "autoboot.txt").write_text(slots.render_autoboot(3, 2))   # кто-то переключил слот мимо агента
    b.firmware_boot(tryboot_flag=False)
    res, _ = b.request("install", b.payload("1.2.0"))
    assert res["result"] == "REJECTED" and "не согласовано" in res["reason"]
    assert b.request("abort")[0]["result"] == "OK"
    assert b.request("install", b.payload("1.2.0"))[0]["result"] == "OK"     # теперь пишется слот A (неактивный)
    assert (b.dev / "mmcblk0p4").read_bytes().startswith(b"SYSTEM-v1.2.0")


# ---------------------------------------------------------------- переключение: только с разрешением


def test_switching_is_disabled_without_signed_enable_token(tmp_path):
    b = Board(tmp_path)
    b.request("install", b.payload("1.1.0"))
    res, _ = b.request("tryboot")
    assert res["result"] == "REFUSED" and b.commands == []
    assert b.enable(serial="100000009d497e91")[0]["result"] == "REJECTED"     # разрешение другой платы
    assert b.enable(key=keypair(tmp_path, "intruder"))[0]["result"] == "REJECTED"
    assert b.request("tryboot")[0]["result"] == "REFUSED"
    assert b.enable()[0]["result"] == "OK"
    assert b.request("tryboot")[0]["result"] == "OK"
    assert b.commands == [["reboot", "0 tryboot"]]
    assert b.request("disable")[0]["result"] == "OK"
    assert b.request("confirm")[0]["result"] == "REFUSED"


@pytest.mark.test_id("TEST-UPD-004")
def test_full_cycle_tryboot_health_commit(tmp_path):
    b = Board(tmp_path)
    pers = b.persistent_hashes()
    b.enable()
    assert b.request("install", b.payload("1.1.0"))[0]["result"] == "OK"
    assert b.request("tryboot")[0]["result"] == "OK"
    assert slots.autoboot_parts((b.bootfs / "autoboot.txt").read_text()) == (2, 3)   # основной всё ещё A
    b.firmware_boot(tryboot_flag=True)
    assert b.agent().on_boot()["state"] == "TRYBOOT → HEALTH_PENDING"
    res, _ = b.request("confirm")
    assert res["result"] == "OK" and res["state"]["state"] == "COMMITTED" and res["state"]["active"] == "B"
    ids = {c["id"]: c["status"] for c in res["checks"]}
    assert ids["health"] == ids["slot"] == ids["version"] == ids["same.machine_id"] == ids["same.ssh_host_key"] == "PASS"
    assert slots.autoboot_parts((b.bootfs / "autoboot.txt").read_text()) == (3, 2)   # mark-good: основной B
    b.firmware_boot(tryboot_flag=False)                                              # обычная перезагрузка
    assert b.slot == "b" and b.agent().on_boot()["state"] == "COMMITTED → COMMITTED"
    assert b.persistent_hashes() == pers
    assert b.request("status")[0]["state"]["version_active"] == "1.1.0"


@pytest.mark.test_id("TEST-UPD-004")
def test_health_fail_never_commits_and_returns_to_previous_slot(tmp_path):
    b = Board(tmp_path)
    b.enable()
    b.request("install", b.payload("1.1.0"))
    b.health = "FAIL"
    for attempt in (1, 2):                       # повтор пробной загрузки — с нового слота, после первого FAIL
        assert b.request("tryboot")[0]["result"] == "OK"
        b.firmware_boot(tryboot_flag=True)
        assert b.slot == "b" and "HEALTH_PENDING" in b.agent().on_boot()["state"]
        res, _ = b.request("confirm")
        assert res["result"] == "FAILED"
        assert slots.autoboot_parts((b.bootfs / "autoboot.txt").read_text())[0] == 2      # основной — A
    st = b.request("status")[0]["state"]
    assert st["state"] == "ROLLED_BACK" and "health FAIL" in st["reason"]
    assert b.request("tryboot")[0]["result"] == "REJECTED"                          # третьей попытки нет
    b.commands.clear()
    assert b.request("abort")[0]["result"] == "OK" and b.commands == [["reboot"]]   # возврат обычной перезагрузкой
    b.firmware_boot(tryboot_flag=False)
    b.agent().on_boot()
    st = b.request("status")[0]["state"]
    assert b.slot == "a" and st["state"] == "ROLLED_BACK" and st["active"] == "A" and st["version_active"] == "1.0.0"


@pytest.mark.parametrize("cut", ["after_install", "after_tryboot_request", "during_trial"])
def test_power_loss_returns_to_committed_slot(tmp_path, cut):
    b = Board(tmp_path)
    b.enable()
    b.request("install", b.payload("1.1.0"))
    if cut != "after_install":
        b.request("tryboot")
    if cut == "during_trial":
        b.firmware_boot(tryboot_flag=True)
        b.agent().on_boot()
    b.firmware_boot(tryboot_flag=False)          # питание пропало: флаг tryboot потерян, прошивка грузит [all]
    r = b.agent().on_boot()
    assert b.slot == "a" and slots.autoboot_parts((b.bootfs / "autoboot.txt").read_text())[0] == 2
    assert r["state"].split(" → ")[1] in ("INSTALLED", "ROLLED_BACK")


def test_abort_on_trial_reboots_normally(tmp_path):
    b = Board(tmp_path)
    b.enable()
    b.request("install", b.payload("1.1.0"))
    b.request("tryboot")
    b.firmware_boot(tryboot_flag=True)
    b.agent().on_boot()
    b.commands.clear()
    assert b.request("abort")[0]["result"] == "OK" and b.commands == [["reboot"]]
    b.firmware_boot(tryboot_flag=False)
    assert "ROLLED_BACK" in b.agent().on_boot()["state"] and b.slot == "a"


def test_confirm_only_on_trial_boot(tmp_path):
    b = Board(tmp_path)
    b.enable()
    b.request("install", b.payload("1.1.0"))
    res, _ = b.request("confirm")
    assert res["result"] == "REJECTED" and ["health"] not in b.commands


def test_backend_refuses_active_slot_and_mounted_target(tmp_path):
    b = Board(tmp_path)
    lay = slots.detect(b.paths())
    be = slots.BlockSlots(lay, b.paths())
    with pytest.raises(UpdateError):
        be.write("A", "1.1.0")
    with pytest.raises(UpdateError):
        be.set_default("B")                                       # не на пробной загрузке
    with open(b.mountinfo, "a") as f:
        f.write(f"30 22 179:5 / /mnt rw - ext4 {b.dev}/mmcblk0p5 rw\n")
    with pytest.raises(UpdateError):
        slots.write_image(str(b.dev / "mmcblk0p5"), io.BytesIO(b""), 1, 1, "0" * 64, "0" * 64, b.mountinfo)


# ---------------------------------------------------------------- сокет и клиент


def test_serve_protocol_and_bad_requests(tmp_path):
    b = Board(tmp_path)
    out = io.BytesIO()
    assert ota.serve(b.agent(), io.BytesIO(b"not json\n"), out) == 2
    assert json.loads(out.getvalue())["result"] == "REJECTED"
    out = io.BytesIO()
    assert ota.serve(b.agent(), io.BytesIO(b'{"action": "rm -rf"}\n'), out) == 1
    out = io.BytesIO()
    rc = ota.serve(b.agent(), io.BytesIO(b'{"action": "install"}\n' + b.payload("1.1.0")), out)
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert rc == 0 and lines[-1]["result"] == "OK" and any("msg" in x for x in lines)


def test_client_streams_over_unix_socket(tmp_path):
    b = Board(tmp_path)
    d = tempfile.mkdtemp(prefix="sho", dir="/tmp")
    path = os.path.join(d, "s")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def one():
        conn, _ = srv.accept()
        with conn, conn.makefile("rb") as r, conn.makefile("wb") as w:
            ota.serve(b.agent(), r, w)
    t = threading.Thread(target=one)
    t.start()
    out = io.StringIO()
    rc = ota.client("install", io.BytesIO(b.payload("1.1.0")), out, path=path)
    t.join(10)
    srv.close()
    os.unlink(path)
    os.rmdir(d)
    assert rc == 0 and json.loads(out.getvalue().splitlines()[-1])["state"]["state"] == "INSTALLED"


def test_on_boot_failure_is_unit_failure_only_during_update(tmp_path, capsys):
    b = Board(tmp_path)
    agent = b.agent()
    agent.detect = lambda p: (_ for _ in ()).throw(UpdateError("нет by-slot"))
    assert ota.on_boot_main(agent) == 0                      # обновления нет: только предупреждение
    b.request("install", b.payload("1.1.0"))
    b.enable()
    b.request("tryboot")
    agent = b.agent()
    agent.detect = lambda p: (_ for _ in ()).throw(UpdateError("нет by-slot"))
    assert ota.on_boot_main(agent) == 1                      # пробная загрузка в процессе: сбой виден
    assert "слоты не определены" in capsys.readouterr().err
