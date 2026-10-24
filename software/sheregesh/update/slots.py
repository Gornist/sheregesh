# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Слоты A/B разметки image-rota на Pi 4: определение активного и неактивного слота, запись неактивного слота,
autoboot.txt для tryboot (S3.1-DEPLOY, подготовка OTA; ADR-0006, предложение ADR-0009).

Разметка (os/config/sheregesh-lab-pi4.yaml): bootconfig | boot_a | boot_b | system_a | system_b | persistent.
Активный слот определяется по нескольким независимым признакам, и любое расхождение — отказ (запись не начнётся):
  • /dev/disk/by-slot/{active,other}/{boot,system}, bootconfig, persistent (udev image-rota) — шесть разных
    разделов одного диска;
  • GPT PARTNAME из sysfs: boot_X/system_X у active и boot_Y/system_Y у other, X ≠ Y;
  • /proc/self/mountinfo: «/» смонтирован с active/system, разделы other нигде не смонтированы;
  • прошивка: /proc/device-tree/chosen/bootloader/partition — номер раздела active/boot.
Запись — только в разделы other (O_EXCL на блочном устройстве дополнительно отказывает, если раздел смонтирован),
поток xz → раздел с SHA-256, fsync, сброс кэша и повторное чтение для сверки. /var целевого слота
(/persistent/slots/system_Y/var) пересоздаётся из /var нового образа и дополняется его образцом
/usr/share/sheregesh/var-seed.tar.gz (на системном разделе image-rota оставляет /var почти пустым), затем в него
переносится конфигурация пульта /var/lib/sheregesh (REQ-CFG-006: у прежнего слота остаётся своя); перед tryboot
перенос повторяется с рабочего слота.
autoboot.txt пишется в том же виде, что rpi-slot-tryboot: [all] boot_partition — основной, [tryboot] — пробный.
"""
import hashlib
import lzma
import os
import re
import shutil
import stat
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .model import UpdateError

CHUNK = 1 << 20
LABELS = {"a": ("boot_a", "system_a"), "b": ("boot_b", "system_b")}


@dataclass(frozen=True)
class Slot:
    name: str          # "a" | "b"
    boot: str          # путь к блочному устройству
    system: str
    boot_part: int     # номер раздела boot (для autoboot.txt)


@dataclass(frozen=True)
class SlotLayout:
    active: Slot
    other: Slot
    bootconfig: str
    persistent: str
    disk: str
    tryboot: bool      # текущая загрузка — пробная (tryboot)

    def slot(self, name):
        return {self.active.name: self.active, self.other.name: self.other}[name.lower()]


@dataclass
class Paths:
    by_slot: str = "/dev/disk/by-slot"
    sys_block: str = "/sys/class/block"
    mountinfo: str = "/proc/self/mountinfo"
    chosen: str = "/proc/device-tree/chosen/bootloader"
    bootfs: str = "/bootfs"                     # раздел bootconfig (autoboot.txt), смонтирован rw
    persistent: str = "/persistent"
    var: str = "/var"


def _uevent(sys_block, dev):
    name = os.path.basename(dev)
    kv = {}
    try:
        for line in Path(sys_block, name, "uevent").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k] = v
    except OSError:
        pass
    parent = os.path.basename(os.path.realpath(os.path.join(sys_block, name, "..")))
    return kv, parent


def _be32(path):
    try:
        raw = Path(path).read_bytes()
        return struct.unpack(">I", raw[:4])[0] if len(raw) >= 4 else None
    except OSError:
        return None


def mounts(mountinfo):
    """[(источник, точка монтирования)] из mountinfo; источник — realpath, если это путь."""
    out = []
    for line in Path(mountinfo).read_text().splitlines():
        left, _, right = line.partition(" - ")
        f, r = left.split(), right.split()
        if len(f) < 5 or len(r) < 2:
            continue
        src = r[1].replace("\\040", " ")
        out.append((os.path.realpath(src) if src.startswith("/") else src, f[4].replace("\\040", " ")))
    return out


def detect(p: Paths | None = None) -> SlotLayout:
    p = p or Paths()
    link = lambda *a: os.path.realpath(os.path.join(p.by_slot, *a))           # noqa: E731
    devs = {"active.boot": link("active", "boot"), "active.system": link("active", "system"),
            "other.boot": link("other", "boot"), "other.system": link("other", "system"),
            "bootconfig": link("bootconfig"), "persistent": link("persistent")}
    for k, d in devs.items():
        if not os.path.exists(d):
            raise UpdateError(f"нет устройства слота {k}: {d}")
    if len(set(devs.values())) != 6:
        raise UpdateError(f"устройства слотов не различны: {devs}")
    info = {k: _uevent(p.sys_block, d) for k, d in devs.items()}
    disks = {parent for _, parent in info.values()}
    if len(disks) != 1:
        raise UpdateError(f"разделы слотов на разных дисках: {sorted(disks)}")
    name = {k: kv.get("PARTNAME", "") for k, (kv, _) in info.items()}
    m_act = re.fullmatch(r"boot_([ab])", name["active.boot"])
    m_oth = re.fullmatch(r"boot_([ab])", name["other.boot"])
    if not m_act or not m_oth or m_act[1] == m_oth[1]:
        raise UpdateError(f"метки boot не образуют пару A/B: {name['active.boot']!r}, {name['other.boot']!r}")
    a, o = m_act[1], m_oth[1]
    if name["active.system"] != f"system_{a}" or name["other.system"] != f"system_{o}":
        raise UpdateError(f"метки system не согласованы с boot: {name}")
    if name["bootconfig"] != "bootconfig" or name["persistent"] != "persistent":
        raise UpdateError(f"неожиданные метки bootconfig/persistent: {name}")
    try:
        part = {k: int(kv["PARTN"]) for k, (kv, _) in info.items()}
    except (KeyError, ValueError):
        raise UpdateError("нет номеров разделов (PARTN) в sysfs") from None
    mt = mounts(p.mountinfo)
    root_src = [s for s, t in mt if t == "/"]
    if not root_src or root_src[-1] != devs["active.system"]:
        raise UpdateError(f"«/» смонтирован не с active/system: {root_src[-1:] or 'нет'}")
    busy = [(s, t) for s, t in mt if s in (devs["other.boot"], devs["other.system"])]
    if busy:
        raise UpdateError(f"разделы неактивного слота смонтированы: {busy}")
    fw = _be32(os.path.join(p.chosen, "partition"))
    if fw is not None and fw != part["active.boot"]:
        raise UpdateError(f"прошивка загрузила раздел {fw}, а active/boot — {part['active.boot']}")
    tb = _be32(os.path.join(p.chosen, "tryboot"))
    return SlotLayout(active=Slot(a, devs["active.boot"], devs["active.system"], part["active.boot"]),
                      other=Slot(o, devs["other.boot"], devs["other.system"], part["other.boot"]),
                      bootconfig=devs["bootconfig"], persistent=devs["persistent"], disk=disks.pop(),
                      tryboot=bool(tb))


# ---------------------------------------------------------------- autoboot.txt


def parse_autoboot(text):
    sections, cur = {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[(\w+)\]", line)
        if m:
            cur = sections.setdefault(m[1], {})
        elif "=" in line and cur is not None:
            k, v = line.split("=", 1)
            cur[k.strip()] = v.strip()
    return sections


def render_autoboot(default_part: int, try_part: int) -> str:
    """Тот же текст, что печатает rpi-slot-tryboot (image-rota)."""
    return f"[all]\ntryboot_a_b=1\nboot_partition={default_part}\n[tryboot]\nboot_partition={try_part}\n"


def autoboot_parts(text):
    s = parse_autoboot(text)
    try:
        if s["all"].get("tryboot_a_b") != "1":
            raise UpdateError("autoboot.txt: нет tryboot_a_b=1")
        return int(s["all"]["boot_partition"]), int(s["tryboot"]["boot_partition"])
    except (KeyError, ValueError):
        raise UpdateError("autoboot.txt: нет boot_partition в [all] или [tryboot]") from None


def write_atomic(path, text):
    path = Path(path)
    tmp = path.with_name(path.name + ".new")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    if Path(path).read_text() != text:
        raise UpdateError(f"{path}: записанное не совпало при повторном чтении")


# ---------------------------------------------------------------- запись слота


def _device_size(fd):
    return os.lseek(fd, 0, os.SEEK_END)


def _drop_cache(fd):
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    except (AttributeError, OSError):
        pass


def write_image(dev, stream, compressed_size, raw_size, raw_sha256, compressed_sha256, mountinfo, progress=None):
    """Поток xz (ровно compressed_size байт из stream) → раздел dev. Проверки до, во время и после записи."""
    real = os.path.realpath(dev)
    if any(s == real for s, _ in mounts(mountinfo)):
        raise UpdateError(f"{dev} смонтирован — запись запрещена")
    st = os.stat(real)
    flags = os.O_WRONLY | (os.O_EXCL if stat.S_ISBLK(st.st_mode) else 0)
    fd = os.open(real, flags)
    try:
        size = _device_size(fd)
        if raw_size > size:
            raise UpdateError(f"образ {raw_size} байт больше раздела {size}")
        os.lseek(fd, 0, os.SEEK_SET)
        dec = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
        hc, hr = hashlib.sha256(), hashlib.sha256()
        left, written, last = compressed_size, 0, 0
        while left:
            chunk = stream.read(min(CHUNK, left))
            if not chunk:
                raise UpdateError("поток обновления оборвался")
            left -= len(chunk)
            hc.update(chunk)
            data = chunk
            while True:
                try:
                    out = dec.decompress(data, max_length=8 * CHUNK)
                except lzma.LZMAError as e:
                    raise UpdateError(f"повреждённый поток xz: {e}") from None
                data = b""
                if out:
                    written += len(out)
                    if written > raw_size:
                        raise UpdateError("распакованный образ больше заявленного")
                    hr.update(out)
                    _write_all(fd, out)
                if dec.needs_input or dec.eof:
                    break
            if progress and written - last >= 64 * CHUNK:
                last = written
                progress(written, raw_size)
        if not dec.eof:
            raise UpdateError("поток xz не завершён")
        if hc.hexdigest() != compressed_sha256:
            raise UpdateError("SHA-256 сжатого образа не совпал с подписанным манифестом")
        if written != raw_size or hr.hexdigest() != raw_sha256:
            raise UpdateError("SHA-256 образа не совпал с подписанным манифестом")
        os.fsync(fd)
    finally:
        os.close(fd)
    return readback(real, raw_size) == raw_sha256


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def readback(dev, raw_size):
    """Повторное чтение после сброса кэша страниц: сверяется то, что на носителе, а не в памяти."""
    fd = os.open(dev, os.O_RDONLY)
    try:
        _drop_cache(fd)
        h, left = hashlib.sha256(), raw_size
        while left:
            chunk = os.read(fd, min(CHUNK * 4, left))
            if not chunk:
                break
            h.update(chunk)
            left -= len(chunk)
        return h.hexdigest()
    finally:
        os.close(fd)


# ---------------------------------------------------------------- /var целевого слота


class VarSeeder:
    """Пересоздание /persistent/slots/system_Y/var из /var записанного образа и перенос конфигурации пульта."""

    CARRY = ("lib/sheregesh",)      # конфигурация пульта (ADR-0004); выпуски DEV deploy не переносятся

    def __init__(self, paths: Paths):
        self.p = paths

    def target_var(self, layout: SlotLayout, slot_name: str) -> Path:
        if slot_name == layout.active.name:
            raise UpdateError("пересоздание /var активного слота запрещено")
        return Path(self.p.persistent) / "slots" / f"system_{slot_name}" / "var"

    def seed(self, layout: SlotLayout, slot_name: str, system_dev: str):
        dst = self.target_var(layout, slot_name)
        with tempfile.TemporaryDirectory(prefix="sheregesh-ota-") as mnt:
            r = subprocess.run(["mount", "-o", "ro,noload", "-t", "ext4", system_dev, mnt], capture_output=True, text=True)
            if r.returncode:
                raise UpdateError(f"не удалось смонтировать новый system только для чтения: {r.stderr.strip()}")
            try:
                self.copy_tree(Path(mnt) / "var", dst)
                self.fill_from_seed(Path(mnt) / "usr/share/sheregesh/var-seed.tar.gz", dst)
            finally:
                subprocess.run(["umount", mnt], capture_output=True)
        self.carry(dst)

    SEED_TAR = ("tar", "-xzf", "{seed}", "-C", "{dst}", "--skip-old-files", "--numeric-owner", "--no-overwrite-dir")

    def fill_from_seed(self, seed: Path, dst: Path):
        """На системном разделе image-rota оставляет почти пустой /var (содержимое — на persistent образа).
        Образец /var нового образа дополняет недостающее; существующее не перезаписывается (S3.2-OTA).
        Нет образца (старый образ) — новый слот дополнит /var службой sheregesh-var-seed при первой загрузке."""
        if not seed.is_file():
            return False
        r = subprocess.run([a.format(seed=seed, dst=dst) for a in self.SEED_TAR], capture_output=True, text=True)
        if r.returncode:
            raise UpdateError(f"образец /var: {r.stderr.strip()[-200:]}")
        return True

    def copy_tree(self, src: Path, dst: Path):
        dst.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["rsync", "-aHAXS", "--numeric-ids", "--delete", f"{src}/", f"{dst}/"],
                           capture_output=True, text=True)
        if r.returncode:
            raise UpdateError(f"rsync /var: {r.stderr.strip()[-200:]}")

    def carry(self, dst: Path):
        for rel in self.CARRY:
            src = Path(self.p.var) / rel
            if src.is_dir():
                self.copy_tree(src, dst / rel)


class BlockSlots:
    """Бэкенд модели Updater для реальных разделов (или файлов-разделов в испытаниях).

    write(slot, version) потребляет поток обновления payload (ota.Payload); verify — итог повторного чтения;
    set_tryboot/set_default — autoboot.txt. Активный слот не пишется никогда: проверка по layout на каждом шаге.
    """

    def __init__(self, layout: SlotLayout, paths: Paths, payload=None, seeder=None, progress=None):
        self.layout, self.p, self.payload = layout, paths, payload
        self.seeder = seeder or VarSeeder(paths)
        self.progress = progress
        self.verified = {}

    def _other(self, slot):
        if slot.lower() != self.layout.other.name:
            raise UpdateError(f"запись разрешена только в неактивный слот {self.layout.other.name.upper()}, не {slot}")
        return self.layout.other

    def write(self, slot, version):
        target = self._other(slot)
        pl = self.payload
        if pl is None or pl.manifest["version"] != version:
            raise UpdateError("нет потока обновления для этой версии")
        ok = True
        for img in pl.manifest["images"]:
            dev = target.boot if img["part"] == "boot" else target.system
            if os.path.realpath(dev) in (self.layout.active.boot, self.layout.active.system):
                raise UpdateError("попытка записи в активный слот")
            ok &= write_image(dev, pl.stream, img["compressed_size"], img["size"], img["sha256"],
                              img["compressed_sha256"], self.p.mountinfo,
                              progress=(lambda w, t, n=img["part"]: self.progress(n, w, t)) if self.progress else None)
        if ok:
            self.seeder.seed(self.layout, target.name, target.system)
        self.verified[(slot.upper(), version)] = ok

    def verify(self, slot, version):
        return self.verified.get((slot.upper(), version), False)

    def _autoboot(self):
        return Path(self.p.bootfs) / "autoboot.txt"

    def set_tryboot(self, slot):
        if slot is None:
            return                       # tryboot однократен: без «reboot 0 tryboot» прошивка грузит [all]
        target = self.layout.slot(slot)
        if target is self.layout.active and not self.layout.tryboot:
            raise UpdateError("пробная загрузка возможна только для неактивного слота")
        default, _ = autoboot_parts(self._autoboot().read_text())
        if default == target.boot_part:
            raise UpdateError("слот уже основной — пробная загрузка не нужна")
        write_atomic(self._autoboot(), render_autoboot(default, target.boot_part))

    def set_default(self, slot):
        """Сделать основным (mark-good) слот, с которого идёт текущая пробная загрузка."""
        if slot.lower() != self.layout.active.name or not self.layout.tryboot:
            raise UpdateError("подтверждать можно только слот текущей пробной загрузки (tryboot)")
        write_atomic(self._autoboot(), render_autoboot(self.layout.active.boot_part, self.layout.other.boot_part))


def copy_tree_py(src: Path, dst: Path):
    """Замена rsync для испытаний на Mac (без root): содержимое dst становится копией src."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True)
