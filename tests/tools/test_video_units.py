# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Юниты видеослужб (ADR-0016, трек C): разбор как ini, обязательные ключи, зависимость rx от таблицы nftables,
попадание в .deb (юниты, nft, помощник, лаунчеры, nftables в DEPENDS) и проверки verify_image.video_checks.
Загрузка юнитов systemd и nft на реальной системе здесь не проверяется — только состав файлов."""
import configparser
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/image"))
import build_deb  # noqa: E402
import verify_image as vi  # noqa: E402

UNITS = ROOT / "tools/image/units"


def load(name):
    """Юнит как ini (ключи могут повторяться — ConditionPathExists, ExecStart)."""
    cp = configparser.RawConfigParser(strict=False, delimiters=("=",), comment_prefixes=("#",))
    cp.optionxform = str
    text = (UNITS / name).read_text()
    # strict=False оставляет последнее значение повторяющегося ключа; для проверки полного набора берём и сырой текст
    cp.read_string(text)
    return cp, text


class VideoServiceUnitTest(unittest.TestCase):
    REQUIRED = {"User": "sheregesh-ui", "Type": "notify", "NotifyAccess": "main", "WatchdogSec": "15",
                "Restart": "on-failure", "RuntimeDirectory": "sheregesh-video", "RuntimeDirectoryMode": "0700",
                "LimitCORE": "0", "NoNewPrivileges": "yes", "ProtectSystem": "strict", "IPAddressDeny": "any",
                "RestrictAddressFamilies": "AF_INET AF_INET6 AF_UNIX"}

    def test_required_keys(self):
        net = load("sheregesh-remote-net.service")[0]
        for name, exe in (("sheregesh-video-tx.service", "/usr/bin/sheregesh-video-tx"),
                          ("sheregesh-video-rx.service", "/usr/bin/sheregesh-video-rx")):
            cp, text = load(name)
            for key, want in self.REQUIRED.items():
                self.assertEqual(cp["Service"].get(key), want, f"{name} {key}")
            self.assertEqual(cp["Service"]["ExecStart"], exe)
            self.assertEqual(cp["Unit"]["StartLimitBurst"], net["Unit"]["StartLimitBurst"])
            self.assertEqual(cp["Unit"]["StartLimitIntervalSec"], net["Unit"]["StartLimitIntervalSec"])
            # та же сетевая песочница, что у sheregesh-remote-net
            self.assertEqual(cp["Service"]["IPAddressAllow"], net["Service"]["IPAddressAllow"])
            self.assertEqual(cp["Service"]["IPAddressDeny"], net["Service"]["IPAddressDeny"])
            for cert in ("device.key", "device.crt", "ca.crt"):
                self.assertIn(f"ConditionPathExists=/home/sheregesh-ui/.local/share/sheregesh/link/{cert}", text)
            self.assertNotIn("key_b64", text)

    def test_roles(self):
        tx = load("sheregesh-video-tx.service")[1]
        rx = load("sheregesh-video-rx.service")[1]
        self.assertIn("ConditionPathExists=/home/sheregesh-ui/.config/sheregesh/remote.role", tx)
        self.assertIn("ConditionPathExists=!/home/sheregesh-ui/.config/sheregesh/remote.role", rx)

    def test_rx_requires_nft_table(self):
        rx = load("sheregesh-video-rx.service")[0]["Unit"]
        self.assertEqual(rx["BindsTo"], "sheregesh-video-nft.service")
        self.assertIn("sheregesh-video-nft.service", rx["After"].split())
        tx = load("sheregesh-video-tx.service")[0]["Unit"]
        self.assertEqual(tx["BindsTo"], "sheregesh-video-nft-remote.service")
        self.assertIn("sheregesh-video-nft-remote.service", tx["After"].split())

    def test_nft_units(self):
        for name, nft, cond in (("sheregesh-video-nft.service", "console", "ConditionPathExists=!"),
                                ("sheregesh-video-nft-remote.service", "remote", "ConditionPathExists=/")):
            cp, text = load(name)
            self.assertEqual(cp["Service"]["Type"], "oneshot")
            self.assertEqual(cp["Service"]["RemainAfterExit"], "yes")
            self.assertEqual(cp["Service"]["User"], "root")
            self.assertEqual(cp["Service"]["ExecStart"], f"/usr/sbin/nft -f /usr/share/sheregesh/nft/sheregesh-video-{nft}.nft")
            self.assertIn("nftables.service", cp["Unit"]["After"].split())
            self.assertEqual(cp["Unit"]["PartOf"], "nftables.service")   # flush ruleset → таблица загружается заново
            self.assertIn(cond, text)
            self.assertNotIn("ExecStart=-", text)                    # ошибка загрузки не маскируется
            self.assertTrue((ROOT / f"tools/image/nft/sheregesh-video-{nft}.nft").exists())

    def test_allow_template(self):
        cp, text = load("sheregesh-video-allow@.service")
        self.assertEqual(cp["Service"]["Type"], "oneshot")
        self.assertEqual(cp["Service"]["User"], "root")
        self.assertEqual(cp["Service"]["ExecStart"], "/usr/bin/sheregesh-video-allow %i")
        self.assertEqual(cp["Service"]["RestrictAddressFamilies"], "AF_NETLINK AF_UNIX")
        self.assertIn("NoNewPrivileges=yes", text)
        self.assertIn("ProtectSystem=strict", text)
        self.assertNotIn("sh -c", text)                               # без shell


class VideoPackageTest(unittest.TestCase):
    def tree(self):
        real_run = subprocess.run

        def fake_run(cmd, *a, **kw):
            if cmd and cmd[0] == "true":
                Path(cmd[-1]).write_bytes(b"qm")
                return subprocess.CompletedProcess(cmd, 0, b"", b"")
            return real_run(cmd, *a, **kw)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(build_deb, "lrelease", return_value="true"), \
                mock.patch.object(build_deb.subprocess, "run", side_effect=fake_run):
            stage = Path(tmp)
            build_deb.build_tree(stage)
            return {"/" + p.relative_to(stage).as_posix(): p.read_text(errors="replace") for p in stage.rglob("*")
                    if p.is_file() and not p.name.endswith((".qm", ".ts"))}, \
                {"/" + p.relative_to(stage).as_posix(): p.stat().st_mode & 0o111 for p in stage.rglob("*") if p.is_file()}

    def test_deb_contents(self):
        files, modes = self.tree()
        for u in vi.VIDEO_UNITS:
            self.assertIn(f"/usr/lib/systemd/system/{u}", files)
        for n in vi.VIDEO_NFT:
            self.assertIn(f"/usr/share/sheregesh/nft/{n}", files)
        self.assertIn("-m sheregesh.videosvc.tx", files["/usr/bin/sheregesh-video-tx"])
        self.assertIn("-m sheregesh.videosvc.rx", files["/usr/bin/sheregesh-video-rx"])
        self.assertIn("python3 -s ", files["/usr/bin/sheregesh-video-tx"])
        self.assertIn("python3 -I /usr/lib/sheregesh/tools/image/bin/sheregesh-video-allow", files["/usr/bin/sheregesh-video-allow"])
        for path in ("/usr/bin/sheregesh-video-allow", "/usr/lib/sheregesh/tools/image/bin/sheregesh-video-allow",
                     "/usr/bin/sheregesh-video-tx", "/usr/bin/sheregesh-video-rx"):
            self.assertTrue(modes[path], path)
        self.assertEqual(build_deb.DEPENDS.count("nftables"), 1)
        self.assertEqual(build_deb.DEPENDS.count("python3-gi"), 1)

    def test_layer(self):
        lab = (ROOT / "os/layer/sheregesh-lab.yaml").read_text()
        self.assertIn("52-sheregesh-video-allow.rules", lab)
        self.assertIn("sheregesh-video-nft sheregesh-video-nft-remote sheregesh-video-rx sheregesh-video-tx", lab)
        self.assertIn("- polkitd", lab)
        self.assertTrue((ROOT / "os/layer/sheregesh-lab.d/52-sheregesh-video-allow.rules").exists())

    def image(self, listed=("nftables",)):
        files, _ = self.tree()
        files["/etc/polkit-1/rules.d/52-sheregesh-video-allow.rules"] = \
            (ROOT / "os/layer/sheregesh-lab.d/52-sheregesh-video-allow.rules").read_text()
        for u in vi.VIDEO_UNITS[:4]:
            files[f"/etc/systemd/system/multi-user.target.wants/{u}"] = "x"
        tsv = "".join(f"{p}\t1\tabc\n" for p in listed)
        return files, tsv

    def run_checks(self, files, tsv):
        class Root:
            cat = staticmethod(lambda p: files.get(p, ""))
            exists = staticmethod(lambda p: p in files)
            ls = staticmethod(lambda p: [f for f in files if f.startswith(p.rstrip("/") + "/")])
        rep = vi.Report()
        with contextlib.redirect_stdout(io.StringIO()):
            vi.video_checks(rep, Root, tsv)
        return {c["id"]: c["status"] for c in rep.checks}

    def test_verify_passes_and_catches_regressions(self):
        files, tsv = self.image()
        r = self.run_checks(files, tsv)
        self.assertEqual({k: v for k, v in r.items() if v != "PASS"}, {})
        self.assertEqual(len(r), 9)
        _, tsv2 = self.image(listed=("python3",))
        self.assertEqual(self.run_checks(files, tsv2)["video.nftables"], "FAIL")
        broken = dict(files)
        broken["/usr/lib/systemd/system/sheregesh-video-rx.service"] = \
            broken["/usr/lib/systemd/system/sheregesh-video-rx.service"].replace("BindsTo=sheregesh-video-nft.service\n", "")
        del broken["/usr/share/sheregesh/nft/sheregesh-video-remote.nft"]
        del broken["/etc/polkit-1/rules.d/52-sheregesh-video-allow.rules"]
        del broken["/usr/bin/sheregesh-video-allow"]
        del broken["/etc/systemd/system/multi-user.target.wants/sheregesh-video-rx.service"]
        r = self.run_checks(broken, tsv)
        for cid in ("video.rx_nft", "video.nft", "video.polkit", "video.allow", "video.enabled"):
            self.assertEqual(r[cid], "FAIL", cid)


if __name__ == "__main__":
    unittest.main()
