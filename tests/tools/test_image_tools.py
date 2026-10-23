# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Инструменты образа S2-1: детерминированный .deb и проверка структуры образа (GPT, ФС, слоты A/B)."""
import contextlib
import io
import json
import re
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import uuid
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/image"))
import build_deb  # noqa: E402
import verify_image as vi  # noqa: E402

REAL_LAYOUT = {n: size for n, _, size, _, _ in vi.LAYOUT}   # до подмен в тестах

SECTOR = 512


def write_gpt(path, parts, total):
    """Мини-образ: защитный MBR, GPT (128 записей) с CRC, резервный заголовок; сигнатуры ФС в разделах."""
    with open(path, "wb") as f:
        f.truncate(total)
        mbr = bytearray(SECTOR)
        mbr[446 + 4], mbr[510:512] = 0xEE, b"\x55\xaa"
        f.seek(0)
        f.write(mbr)
        entries = bytearray(128 * 128)
        for i, (name, ptype, off, size, fs, label) in enumerate(parts):
            e = struct.pack("<16s16sQQQ", ptype.bytes_le, uuid.uuid4().bytes_le, off // SECTOR, (off + size) // SECTOR - 1, 0)
            entries[i * 128:(i + 1) * 128] = e + name.encode("utf-16-le").ljust(72, b"\0")
            if fs == "ext4":
                sb = bytearray(1024)
                struct.pack_into("<H", sb, 56, 0xEF53)
                struct.pack_into("<I", sb, 96, 0x40)
                sb[120:120 + len(label)] = label.encode()
                f.seek(off + 1024)
                f.write(sb)
            else:
                bs = bytearray(SECTOR)
                bs[82:90], bs[71:82], bs[510:512] = b"FAT32   ", label.encode().ljust(11), b"\x55\xaa"
                f.seek(off)
                f.write(bs)
        f.seek(2 * SECTOR)
        f.write(entries)
        last = total // SECTOR - 1
        hdr = bytearray(struct.pack("<8sIIIIQQQQ16sQIII", b"EFI PART", 0x10000, 92, 0, 0, 1, last, 34, last - 33,
                                    uuid.uuid4().bytes_le, 2, 128, 128, zlib.crc32(entries) & 0xFFFFFFFF))
        struct.pack_into("<I", hdr, 16, zlib.crc32(hdr) & 0xFFFFFFFF)
        f.seek(SECTOR)
        f.write(hdr)


class VerifyImageTest(unittest.TestCase):
    MiB = 1024 * 1024
    SMALL = [("bootconfig", "fat", 1, "BOOTCONFIG"), ("boot_a", "fat", 2, "BOOT"), ("boot_b", "fat", 2, "BOOT"),
             ("system_a", "ext4", 3, "SYSTEM"), ("system_b", "ext4", 3, "SYSTEM"), ("persistent", "ext4", None, "PERSISTENT")]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        layout = [(n, fs, s * self.MiB if s else None, lab, "") for n, fs, s, lab in self.SMALL]
        self.patches = [mock.patch.object(vi, "LAYOUT", layout), mock.patch.object(vi, "ALIGN", self.MiB),
                        mock.patch.object(vi, "MIN_DATA", 4 * self.MiB),
                        mock.patch.object(vi.shutil, "which", return_value=None)]   # содержимое — SKIP
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def image(self, order=None, sizes=None):
        parts, off = [], self.MiB
        for i, (name, fs, size, label) in enumerate(self.SMALL):
            size = (sizes or {}).get(name, size or 4) * self.MiB
            parts.append((name, vi.FAT_TYPE if fs == "fat" else vi.LINUX_TYPE, off, size, fs, label))
            off += size
        if order:
            parts = [parts[i] for i in order]
        img = self.dir / "t.img"
        write_gpt(img, parts, off + self.MiB)
        return img

    def run_verify(self, img):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = vi.verify(str(img), "sheregesh-lab-pi4", self.dir / "out")
        return rc, json.loads((self.dir / "out/verify.json").read_text())

    def status(self, r, cid):
        return next(c["status"] for c in r["checks"] if c["id"] == cid)

    def test_valid_layout_passes_structure_and_skips_content(self):
        rc, r = self.run_verify(self.image())
        self.assertEqual(rc, 0, [c for c in r["checks"] if c["status"] == "FAIL"])
        self.assertEqual(r["verdict"], "PARTIAL")                     # без debugfs/mtools — не PASS
        for cid in ("gpt.mbr", "gpt.crc", "gpt.backup", "layout.order", "part.system_a.fs", "slots.boot", "slots.system"):
            self.assertEqual(self.status(r, cid), "PASS", cid)
        self.assertEqual(self.status(r, "root.content"), "SKIP")
        self.assertIn("tryboot и A/B-откат (S2-5)", r["not_tested"])

    def test_wrong_order_and_size_fail(self):
        rc, r = self.run_verify(self.image(order=[0, 1, 2, 4, 3, 5], sizes={"boot_b": 3}))
        self.assertEqual(rc, 1)
        self.assertEqual(self.status(r, "layout.order"), "FAIL")
        self.assertEqual(self.status(r, "part.boot_b.size"), "FAIL")

    def test_corrupted_gpt_crc_fails(self):
        img = self.image()
        with open(img, "r+b") as f:
            f.seek(2 * SECTOR + 32)                     # младший байт начального LBA первой записи
            f.write(b"\x01")
        rc, r = self.run_verify(img)
        self.assertEqual(self.status(r, "gpt.crc"), "FAIL")

    def test_layout_matches_image_config(self):
        """Ожидания проверки совпадают с os/config/sheregesh-lab-pi4.yaml и ADR-0006."""
        cfg = (ROOT / "os/config/sheregesh-lab-pi4.yaml").read_text()
        get = lambda k: int(re.search(rf"^\s*{k}:\s*(\d+)[MG]", cfg, re.M)[1])          # noqa: E731
        self.assertEqual(REAL_LAYOUT["boot_a"], get("boot_part_size") * self.MiB)
        self.assertEqual(REAL_LAYOUT["system_a"], get("system_part_size") * self.MiB)
        self.assertIn("data_part_size: 1G", cfg)
        self.assertIn("rootfs_type: ext4", cfg)
        self.assertIn("layer: image-rota", cfg)
        adr = (ROOT / "docs/adr/ADR-0006-updates-and-recovery.md").read_text()
        self.assertIn("autoboot | FAT, 32 МБ", adr)
        self.assertIn("boot-A | FAT, 256 МБ", adr)


class BuildDebTest(unittest.TestCase):
    def test_deterministic_package_with_translations_and_units(self):
        if not build_deb.lrelease():
            self.skipTest("нет lrelease")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"SOURCE_DATE_EPOCH": "1790000000"}):
            with contextlib.redirect_stdout(io.StringIO()):
                a = build_deb.build(Path(tmp) / "a", "0.0~test")
                b = build_deb.build(Path(tmp) / "b", "0.0~test")
            self.assertEqual(a.read_bytes(), b.read_bytes())
            raw = a.read_bytes()
            self.assertTrue(raw.startswith(b"!<arch>\ndebian-binary   "))
            data = raw[raw.index(b"data.tar.xz"):]
            size = int(data[48:58])
            with tarfile.open(fileobj=io.BytesIO(data[60:60 + size]), mode="r:xz") as tar:
                names = tar.getnames()
                self.assertTrue(all(m.mtime == 1790000000 and m.uid == 0 for m in tar.getmembers()))
            for must in ("./usr/bin/sheregesh-console", "./usr/lib/systemd/system/sheregesh-console.service",
                         "./usr/lib/sheregesh/software/console/sheregesh_console/i18n/sheregesh_ru.qm"):
                self.assertIn(must, names)
            self.assertFalse([n for n in names if "__pycache__" in n or "/tests/" in n])

    def test_units_keep_emulator_on_loopback(self):
        emu = (ROOT / "tools/image/units/sheregesh-remote-emu.service").read_text()
        for line in ("--host 127.0.0.1", "IPAddressDeny=any", "IPAddressAllow=localhost", "DynamicUser=yes"):
            self.assertIn(line, emu)
        ui = (ROOT / "tools/image/units/sheregesh-console.service").read_text()
        self.assertIn("User=sheregesh-ui", ui)
        self.assertIn("--peer 127.0.0.1:7700", ui)


if __name__ == "__main__":
    unittest.main()
