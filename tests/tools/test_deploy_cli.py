# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""shdeploy (S3.1-DEPLOY): сборка пакета из коммита, действия на плате только фиксированными командами, сверка
серийного номера, сборка потока OTA из образа image-rota. Платы заменены FakeRemote; сети и SSH нет."""
import contextlib
import hashlib
import io
import json
import lzma
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/deploy"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import shdeploy  # noqa: E402
from test_image_tools import write_gpt  # noqa: E402

from sheregesh.deploy import bundle, sig  # noqa: E402
from sheregesh.update import ota  # noqa: E402

HAVE_KEYGEN = bool(shutil.which("ssh-keygen"))


def keypair(d, name="owner"):
    k = Path(d) / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(k)], check=True)
    signers = Path(d) / f"{name}.signers"
    signers.write_text(sig.allowed_signers_line(k.with_suffix(".pub").read_text()))
    return k, signers


class FakeRemote:
    serial = shdeploy.BOARDS["pi1"]["serial"]
    log = []
    history = []
    result = "OK"

    def __init__(self, host):
        self.host = host

    def run(self, cmd, data=None, timeout=900, check=False):
        FakeRemote.log.append((self.host, cmd, data))
        if "serial-number" in cmd:
            return 0, FakeRemote.serial + "\n", ""
        if cmd.startswith("systemctl start sheregesh-deploy@"):
            last = {"action": "install", "result": FakeRemote.result, "reason": "ok"}
            FakeRemote.history.append(last)
            return 0, json.dumps({"current": "x", "last": last, "history": FakeRemote.history}), ""
        if cmd.startswith("cat /var/lib/sheregesh-deploy/state.json"):
            return 0, json.dumps({"history": FakeRemote.history}), ""
        return 0, "", ""


@contextlib.contextmanager
def fake_lrelease():
    with tempfile.TemporaryDirectory() as d:
        tool = Path(d) / "lrelease"
        tool.write_text('#!/bin/sh\necho qm > "$4"\n')
        tool.chmod(0o755)
        with mock.patch.object(shdeploy.build_deb, "lrelease", return_value=str(tool)):
            yield


@unittest.skipUnless(HAVE_KEYGEN, "нет ssh-keygen")
class DeployCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        FakeRemote.log, FakeRemote.history, FakeRemote.result = [], [], "OK"
        FakeRemote.serial = shdeploy.BOARDS["pi1"]["serial"]
        self.patches = [mock.patch.object(shdeploy, "CACHE", self.d / "boards.json"),
                        mock.patch.dict(os.environ, {"SHEREGESH_PI1_HOST": "", "SHEREGESH_PI2_HOST": ""})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_release_from_commit_matches_deb_sources(self):
        with fake_lrelease():
            data, version, commit, _ = shdeploy.build_release()
            again = shdeploy.build_release()[0]
        self.assertEqual(data, again)                                  # детерминированно
        manifest, files = bundle.parse_bundle(data)
        tracked = set(shdeploy.build_deb.tracked_files())
        self.assertEqual(set(files) - tracked, {"software/console/sheregesh_console/i18n/sheregesh_ru.qm"})
        self.assertEqual(tracked - set(files), set())
        self.assertEqual(manifest["commit"], commit)
        self.assertTrue(version.endswith("+g" + commit[:12]))

    def test_deploy_uploads_signed_bundle_and_starts_only_fixed_unit(self):
        key, signers = keypair(self.d)
        with fake_lrelease(), mock.patch.object(shdeploy, "Remote", FakeRemote), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = shdeploy.main(["deploy", "pi1", "--host", "10.0.0.5", "--key", str(key)])
        self.assertEqual(rc, 0, out.getvalue())
        cmds = [c for _, c, _ in FakeRemote.log]
        uploads = {c.split("/")[-1]: data for _, c, data in FakeRemote.log if data}
        self.assertEqual(sorted(uploads), ["bundle.tar.xz", "bundle.tar.xz.sig"])
        sig.verify(uploads["bundle.tar.xz"], uploads["bundle.tar.xz.sig"], signers, sig.NS_APP)
        self.assertTrue(any(c.startswith("systemctl start sheregesh-deploy@install.service") for c in cmds))
        for c in cmds:                                                  # никаких sudo/произвольных команд
            self.assertNotIn("sudo", c)
            self.assertRegex(c, r"^(tr -d|mkdir -p ~/\.cache/sheregesh-deploy/incoming|cat > ~/\.cache/sheregesh-deploy/"
                                r"incoming/|cat /var/lib/sheregesh-deploy/state\.json|"
                                r"systemctl start sheregesh-deploy@install\.service)")
        self.assertIn("выпуск: x", out.getvalue())

    def test_failed_install_is_reported_from_state_not_unit_status(self):
        FakeRemote.result = "ROLLED_BACK"                              # юнит «успешен» (SuccessExitStatus), итог — нет
        with mock.patch.object(shdeploy, "Remote", FakeRemote), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(shdeploy.main(["rollback", "pi1", "--host", "h"]), 1)

    def test_rollback_and_factory_do_not_upload(self):
        for action in ("rollback", "factory"):
            FakeRemote.log = []
            with mock.patch.object(shdeploy, "Remote", FakeRemote), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(shdeploy.main([action, "pi1", "--host", "h"]), 0)
            self.assertFalse([d for _, _, d in FakeRemote.log if d])
            self.assertTrue(any(f"sheregesh-deploy@{action}.service" in c for _, c, _ in FakeRemote.log))

    def test_wrong_board_serial_is_refused(self):
        FakeRemote.serial = shdeploy.BOARDS["pi2"]["serial"]           # по адресу pi1 отвечает pi2
        with mock.patch.object(shdeploy, "Remote", FakeRemote), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(shdeploy.CliError):
                shdeploy.resolve("pi1", "10.0.0.7")
        self.assertFalse((self.d / "boards.json").exists())
        FakeRemote.serial = shdeploy.BOARDS["pi1"]["serial"]
        with mock.patch.object(shdeploy, "Remote", FakeRemote):
            self.assertEqual(shdeploy.resolve("pi1", None, finder=lambda mac, net: "10.0.0.9").host, "10.0.0.9")
        self.assertEqual(json.loads((self.d / "boards.json").read_text())["pi1"], "10.0.0.9")

    def test_ota_build_from_image_rota_layout(self):
        import verify_image as vi
        MiB = 1 << 20
        small = [("bootconfig", "fat", 1, "BOOTCONFIG"), ("boot_a", "fat", 2, "BOOT"), ("boot_b", "fat", 2, "BOOT"),
                 ("system_a", "ext4", 3, "SYSTEM"), ("system_b", "ext4", 3, "SYSTEM"), ("persistent", "ext4", 1, "PERSISTENT")]
        parts, off = [], MiB
        for name, fs, size, label in small:
            parts.append((name, vi.FAT_TYPE if fs == "fat" else vi.LINUX_TYPE, off, size * MiB, fs, label))
            off += size * MiB
        img = self.d / "t.img"
        write_gpt(img, parts, off + MiB)
        with open(img, "r+b") as f:                                     # одинаковое содержимое слотов A и B
            for name, _, o, size, _, _ in parts:
                if name in ("boot_a", "boot_b"):
                    f.seek(o + 4096)
                    f.write(b"BOOT-CONTENT" * 100)
                if name == "system_a":
                    f.seek(o + 8192)
                    f.write(b"SYSTEM-CONTENT" * 100)
        key, signers = keypair(self.d)
        out, m = shdeploy.build_ota(img, "sheregesh-lab-dev-pi4", "0.1.1790000000", "d" * 40, self.d / "ota", key)
        with open(out, "rb") as f:
            pl = ota.Payload(f, signers, "sheregesh-lab-pi4-dev-1")
            self.assertEqual(pl.manifest["version"], "0.1.1790000000")
            for i, (name, _, o, size, _, _) in zip(pl.manifest["images"], [parts[1], parts[3]]):
                raw = lzma.decompress(f.read(i["compressed_size"]))
                with open(img, "rb") as src:
                    src.seek(o)
                    self.assertEqual(raw, src.read(size))
                self.assertEqual(hashlib.sha256(raw).hexdigest(), i["sha256"])
        with open(out, "rb") as f:                                      # строгий образ dev-плата не примет
            with self.assertRaises(ota.PayloadError):
                ota.Payload(f, signers, "sheregesh-lab-pi4-1")


class OtaInstallSha(unittest.TestCase):
    def test_install_refuses_payload_with_other_sha_before_sending(self):
        with tempfile.TemporaryDirectory() as d:
            pl = Path(d) / "p.shota"
            pl.write_bytes(b"SHRGOTA1\n")
            remote = mock.Mock()
            remote.stream.return_value = (0, {"result": "OK"})
            argv = ["ota-install", "pi2", "--payload", str(pl)]
            with mock.patch.object(shdeploy, "resolve", return_value=remote), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(shdeploy.main(argv + ["--sha256", "0" * 64]), 2)
                remote.stream.assert_not_called()
                good = hashlib.sha256(pl.read_bytes()).hexdigest()
                self.assertEqual(shdeploy.main(argv + ["--sha256", good.upper()]), 0)
                remote.stream.assert_called_once()


if __name__ == "__main__":
    unittest.main()
