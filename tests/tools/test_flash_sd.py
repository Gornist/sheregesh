# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Запись образа на microSD (tools/image/flash_sd.py): какие диски допускаются и сверка контрольных сумм."""
import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/image"))
import flash_sd as fs  # noqa: E402

GB = 10 ** 9
IMG = 7_021_285_376


def disk(**kw):
    info = {"DeviceIdentifier": "disk4", "Internal": False, "WholeDisk": True, "Size": 32 * GB,
            "MediaName": "SD Card Reader", "BusProtocol": "USB", "RemovableMedia": True}
    info.update(kw.pop("info", {}))
    return fs.describe(info, kw.pop("parts", []))


class DiskChoiceTest(unittest.TestCase):
    def test_external_card_of_right_size_is_allowed(self):
        self.assertIsNone(fs.refusal(disk(), IMG, 256 * GB))

    def test_internal_partition_small_and_huge_disks_are_refused(self):
        self.assertEqual(fs.refusal(disk(info={"Internal": True}), IMG, 256 * GB), "внутренний диск")
        self.assertEqual(fs.refusal(disk(info={"WholeDisk": False}), IMG, 256 * GB), "не целый диск")
        self.assertIn("меньше образа", fs.refusal(disk(info={"Size": 4 * GB}), IMG, 256 * GB))
        self.assertIn("больше", fs.refusal(disk(info={"Size": 2000 * GB}), IMG, 256 * GB))

    def test_existing_raspberry_pi_system_is_detected(self):
        orig = disk(parts=[{"VolumeName": "bootfs"}, {"Content": "Linux"}])
        ours = disk(parts=[{"VolumeName": "BOOTCONFIG"}, {"VolumeName": "BOOT"}, {"Content": "Linux"}])
        blank = disk(parts=[{"VolumeName": "Untitled"}])
        self.assertEqual(fs.existing_system(orig), ["bootfs"])
        self.assertEqual(fs.existing_system(ours), ["BOOT", "BOOTCONFIG"])
        self.assertEqual(fs.existing_system(blank), [])


class SumsTest(unittest.TestCase):
    def test_sums_and_build_report_are_read_next_to_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "x.img.zst"
            img.write_bytes(b"zstd")
            digest = hashlib.sha256(b"zstd").hexdigest()
            (Path(tmp) / "SHA256SUMS").write_text(f"{digest}  x.img.zst\n{'0' * 64}  other\n")
            (Path(tmp) / "build-report.json").write_text(json.dumps({"image_sha256": "ab" * 32, "image_bytes": IMG}))
            self.assertEqual(fs.expected_sums(img), (digest, ("ab" * 32, IMG)))
            self.assertEqual(fs.sha256_file(img), digest)

    def test_missing_sums_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "x.img.zst"
            img.write_bytes(b"z")
            with self.assertRaises(SystemExit):
                fs.expected_sums(img)
            (Path(tmp) / "SHA256SUMS").write_text(f"{'0' * 64}  other.img.zst\n")
            with self.assertRaises(SystemExit):
                fs.expected_sums(img)



@unittest.skipUnless(shutil.which("zstd"), "нет zstd")
class WriteVerifyTest(unittest.TestCase):
    def test_write_then_read_back_through_same_descriptor(self):
        """Запись и сверка тем же дескриптором; на обычном файле вместо /dev/rdiskN."""
        with tempfile.TemporaryDirectory() as tmp:
            raw = bytes(range(256)) * (3 * 4096 + 2)           # не кратно блоку 4 МиБ, кратно 512
            src = Path(tmp) / "i.img"
            src.write_bytes(raw)
            subprocess.run(["zstd", "-q", str(src), "-o", str(src) + ".zst"], check=True)
            dev = Path(tmp) / "card"
            dev.write_bytes(b"\xff" * (len(raw) + 4096))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(fs.write_and_verify(Path(str(src) + ".zst"), str(dev), len(raw),
                                                     hashlib.sha256(raw).hexdigest()), 0)
                self.assertEqual(fs.write_and_verify(Path(str(src) + ".zst"), str(dev), len(raw), "0" * 64), 1)
            self.assertEqual(dev.read_bytes()[:len(raw)], raw)


if __name__ == "__main__":
    unittest.main()
