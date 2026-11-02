# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Образ сборочного хоста Pi 5 (ADR-0019): конфигурация, слой и проверка структуры согласованы."""
import importlib.util
import re
import struct
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("verify_buildhost", ROOT / "tools/image/verify_buildhost.py")
vb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vb)

CONFIG = (ROOT / "os/config/sheregesh-buildhost-pi5.yaml").read_text()
LAYER = (ROOT / "os/layer/sheregesh-buildhost.yaml").read_text()


def mbr(sig=0xA1B2C3D4, parts=((0x0C, 16384, 1048576), (0x83, 1064960, 12582912))):
    b = bytearray(512)
    struct.pack_into("<I", b, 440, sig)
    for i, (t, lba, n) in enumerate(parts):
        b[446 + 16 * i] = 0x80 if i == 0 else 0
        b[446 + 16 * i + 4] = t
        struct.pack_into("<II", b, 446 + 16 * i + 8, lba, n)
    b[510:512] = b"\x55\xaa"
    return bytes(b)


class BuildhostImageTest(unittest.TestCase):
    def test_config_matches_verifier(self):
        self.assertIn(f"hostname: {vb.HOSTNAME}", CONFIG)
        self.assertIn(f"user1: {vb.USER}", CONFIG)
        self.assertIn("name: sheregesh-buildhost-pi5", CONFIG)
        self.assertIn("devid_scheme: partuuid", CONFIG)             # verify_buildhost и boot_check читают PARTUUID
        self.assertIn(f"boot_part_size: {vb.BOOT_MIN // vb.MiB}M", CONFIG)
        self.assertIn(f"root_part_size: {vb.ROOT_MIN // vb.MiB}M", CONFIG)
        self.assertIn("-b 4096", CONFIG)                            # корень монтируется ядром Debian (4 КиБ) в QEMU
        self.assertNotIn("lab-pi4", CONFIG)                         # ни пульта, ни A/B, ни пакета sheregesh

    def test_layer_provides_what_verifier_checks(self):
        for needle in ("docker.io", "docker-cli", "git", "zstd", "rsync", "python3", "cloud-guest-utils", "nvme-cli",
                       "dtparam=pciex1", "PermitRootLogin prohibit-password", "RuntimeWatchdogSec=30s",
                       "Storage=persistent", "shbuild-growroot.service", "90-sheregesh-buildhost",
                       "mask rpi-eeprom-update.service", "BUILDHOST-MODE"):
            self.assertIn(needle, LAYER)
        self.assertNotIn("PasswordAuthentication yes", LAYER)

    def test_growroot_is_idempotent_and_documented(self):
        sh = (ROOT / "os/layer/sheregesh-buildhost.d/shbuild-growroot").read_text()
        self.assertIn("NOCHANGE", sh)                               # растить нечего — не ошибка (загрузочный тест)
        self.assertIn("/var/lib/shbuild-growroot.done", LAYER)

    def test_mbr_parse(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(mbr())
            f.flush()
            with open(f.name, "rb") as r:
                m = vb.read_mbr(r)
        self.assertTrue(m["valid"])
        self.assertEqual(m["disksig"], 0xA1B2C3D4)
        self.assertEqual([(p["index"], p["type"], p["boot"]) for p in m["partitions"]], [(1, 0x0C, True), (2, 0x83, False)])
        self.assertEqual(m["partitions"][0]["offset"], 16384 * 512)
        self.assertEqual(m["partitions"][0]["offset"] % (8 * vb.MiB), 0)

    def test_packages_from_status(self):
        status = ("Package: git\nStatus: install ok installed\nVersion: 1:2.47\nArchitecture: arm64\n\n"
                  "Package: gone\nStatus: deinstall ok config-files\nVersion: 1\nArchitecture: all\n\n"
                  "Package: zstd\nStatus: install ok installed\nVersion: 1.5\nArchitecture: arm64\n")
        self.assertEqual(vb.packages_from_status(status), "git\t1:2.47\tarm64\nzstd\t1.5\tarm64\n")

    def test_build_script_has_buildhost_branch(self):
        text = (ROOT / "os/build.sh").read_text()
        self.assertIn("verify_buildhost.py", text)
        self.assertRegex(text, r'BUILDHOST=; \[\[ "\$CFG" == sheregesh-buildhost-\* \]\]')
        self.assertTrue(re.search(r"if \[\[ -z \"\$BUILDHOST\" \]\]; then\n  echo \"== пакет sheregesh\"", text))


if __name__ == "__main__":
    unittest.main()
