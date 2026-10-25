# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Инструменты образа S2-1: детерминированный .deb и проверка структуры образа (GPT, ФС, слоты A/B)."""
import contextlib
import io
import json
import os
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


class FakeRoot:
    """Корневая ФС образа как словарь путь → текст (заменяет debugfs в проверках S3.1-DEPLOY)."""

    def __init__(self, files):
        self.files = files

    def cat(self, path):
        return self.files.get(path, "")

    def exists(self, path):
        return path in self.files

    def ls(self, path):
        return [p for p in self.files if p.startswith(path.rstrip("/") + "/")]


class DeployImageTest(unittest.TestCase):
    """S3.1-DEPLOY: обёртки через launcher, юниты, polkit и ключ подписи — в .deb и в проверке образа."""

    def deb_tree(self):
        real_run = subprocess.run

        def fake_run(cmd, *a, **kw):                     # lrelease не нужен: .qm подменяется заглушкой
            if cmd and cmd[0] == "true":
                Path(cmd[-1]).write_bytes(b"qm")
                return subprocess.CompletedProcess(cmd, 0, b"", b"")
            return real_run(cmd, *a, **kw)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(build_deb, "lrelease", return_value="true"), \
                mock.patch.object(build_deb.subprocess, "run", side_effect=fake_run):
            stage = Path(tmp)
            build_deb.build_tree(stage)
            return {p.relative_to(stage).as_posix(): p.read_text(errors="replace") for p in stage.rglob("*")
                    if p.is_file() and not p.name.endswith((".qm", ".ts"))}

    def test_deb_wrappers_units_and_build_info(self):
        t = self.deb_tree()
        for w in ("usr/bin/sheregesh-console", "usr/bin/sheregesh-remote-emu"):
            self.assertIn(f"exec /usr/bin/python3 -I {build_deb.LAUNCHER} ", t[w])
        self.assertIn(build_deb.LAUNCHER.lstrip("/"), t)
        self.assertIn("python3 -s -m sheregesh.deploy.installer", t["usr/bin/sheregesh-deployd"])
        self.assertIn("-m sheregesh.health", t["usr/bin/sheregesh-health"])            # диагностика — из /usr
        for u in vi.DEPLOY_UNITS:
            self.assertIn(f"usr/lib/systemd/system/{u}", t)
        info = json.loads(t["usr/share/sheregesh/build.json"])
        self.assertRegex(info["ota_version"], r"^\d+\.\d+\.\d+$")
        deploy = t["usr/lib/systemd/system/sheregesh-deploy@.service"]
        for line in ("ExecStart=/usr/bin/sheregesh-deployd %i", "ProtectSystem=strict", "IPAddressDeny=any",
                     f"ConditionPathExists={vi.SIGNERS}"):
            self.assertIn(line, deploy)
        sock = t["usr/lib/systemd/system/sheregesh-ota.socket"]
        self.assertNotRegex(sock, r"ListenStream=\d|ListenStream=\[|ListenDatagram")      # только UNIX-сокет

    def test_polkit_rule_and_layer(self):
        rules = (ROOT / "os/layer/sheregesh-dev-access.d/50-sheregesh-dev.rules").read_text()
        self.assertIn('action.lookup("verb") == "start"', rules)
        self.assertNotIn("sheregesh-ota", rules)
        layer = (ROOT / "os/layer/sheregesh-dev-access.yaml").read_text()
        self.assertIn("/etc/sheregesh/deploy/allowed_signers", layer)
        lab = (ROOT / "os/layer/sheregesh-lab.yaml").read_text()
        self.assertIn("sheregesh-ota.socket sheregesh-ota-boot", lab)

    def image_root(self, dev=True, signer_same=False):
        t = self.deb_tree()
        files = {"/" + k: v for k, v in t.items()}
        files["/etc/systemd/system/sockets.target.wants/sheregesh-ota.socket"] = "x"
        files["/etc/systemd/system/multi-user.target.wants/sheregesh-ota-boot.service"] = "x"
        files["/etc/systemd/system/sysinit.target.wants/sheregesh-var-seed.service"] = "x"
        files["/usr/share/sheregesh/var-seed.tar.gz"] = "seed"
        if dev:
            files[vi.SIGNERS] = 'sheregesh-deploy namespaces="sheregesh-deploy" ssh-ed25519 SIGNERKEY\n'
            files["/etc/polkit-1/rules.d/50-sheregesh-dev.rules"] = \
                (ROOT / "os/layer/sheregesh-dev-access.d/50-sheregesh-dev.rules").read_text()
        data = FakeRoot({"/home/sheregesh-ui/.ssh/authorized_keys":
                         f"ssh-ed25519 {'SIGNERKEY' if signer_same else 'LOGINKEY'} owner\n"})
        return FakeRoot(files), data

    def run_checks(self, root, data, dev):
        rep = vi.Report()
        with contextlib.redirect_stdout(io.StringIO()):
            vi.deploy_checks(rep, root, data, dev, {"sheregesh-ui": []})
        return {c["id"]: c for c in rep.checks}

    def test_verify_deploy_checks_pass_on_dev_and_strict(self):
        root, data = self.image_root(dev=True)
        r = self.run_checks(root, data, True)
        self.assertEqual({k: v["status"] for k, v in r.items() if v["status"] != "PASS"}, {})
        self.assertIn("отдельный", r["deploy.signers"]["detail"])
        root, data = self.image_root(dev=True, signer_same=True)
        same = self.run_checks(root, data, True)["deploy.signers"]
        self.assertEqual(same["status"], "FAIL")                 # ключ входа вместо ключа подписи — не принимается
        self.assertIn("совпадает с ключом входа", same["detail"])
        root, data = self.image_root(dev=False)
        r = self.run_checks(root, data, False)
        self.assertEqual(r["deploy.absent"]["status"], "PASS")

    def test_verify_deploy_checks_catch_regressions(self):
        root, data = self.image_root(dev=True)
        root.files["/usr/bin/sheregesh-console"] = "exec /usr/bin/python3 -m sheregesh_console\n"
        root.files["/etc/polkit-1/rules.d/50-sheregesh-dev.rules"] += '\n"sheregesh-deploy@shell.service"\n'
        del root.files["/etc/systemd/system/sockets.target.wants/sheregesh-ota.socket"]
        del root.files["/usr/share/sheregesh/var-seed.tar.gz"]       # S3.2-OTA: без образца новый слот без /var
        r = self.run_checks(root, data, True)
        for cid in ("deploy.launcher", "deploy.polkit", "deploy.enabled", "deploy.varseed"):
            self.assertEqual(r[cid]["status"], "FAIL", cid)
        root, data = self.image_root(dev=False)
        root.files[vi.SIGNERS] = "x"
        self.assertEqual(self.run_checks(root, data, False)["deploy.absent"]["status"], "FAIL")

    def test_var_seed_has_no_ordering_cycle(self):
        """OTA CLOSURE цикл 2: Before=systemd-sysusers при After=local-fs.target — цикл, systemd удалял случайное
        задание (в т. ч. local-fs.target) — слот B без /persistent. Юнит репозитория и проверка образа."""
        unit = (ROOT / "tools/image/units/sheregesh-var-seed.service").read_text()
        self.assertEqual(vi.boot_order_violations(unit), [])
        root, data = self.image_root(dev=True)
        path = "/usr/lib/systemd/system/sheregesh-var-seed.service"
        self.assertEqual(self.run_checks(root, data, True)["boot.order"]["status"], "PASS")
        root.files[path] = root.files[path].replace("Before=sysinit.target systemd-tmpfiles-setup.service",
                                                    "Before=sysinit.target systemd-tmpfiles-setup.service "
                                                    "systemd-sysusers.service")
        r = self.run_checks(root, data, True)["boot.order"]
        self.assertEqual(r["status"], "FAIL")
        self.assertIn("systemd-sysusers.service", r["detail"])


if __name__ == "__main__":
    unittest.main()


class BuildShSignerTest(unittest.TestCase):
    """os/build.sh: dev-образ без отдельного ключа подписи (или с ключом входа вместо него) не собирается."""

    def run_build(self, deploy):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            login = Path(d) / "login.pub"
            login.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILOGINKEYLOGINKEYLOGINKEYLOGINKEYLOGINKEY0 owner\n")
            env = {**os.environ, "SSH_PUBKEY": str(login), "SHEREGESH_WORK": d, "OUT": d}
            if deploy is not None:
                f = Path(d) / "deploy.pub"
                f.write_text(deploy if deploy != "SAME" else login.read_text())
                env["DEPLOY_PUBKEY"] = str(f)
            else:
                env.pop("DEPLOY_PUBKEY", None)
            return subprocess.run(["bash", str(ROOT / "os/build.sh"), "sheregesh-lab-dev-pi4"], env=env,
                                  capture_output=True, text=True, timeout=60)

    def test_missing_or_login_key_refused(self):
        r = self.run_build(None)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("обязателен", r.stderr)
        r = self.run_build("SAME")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("совпадает с ключом входа", r.stderr)
