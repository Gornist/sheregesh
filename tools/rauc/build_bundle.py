#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Сборка подписанного бандла RAUC (verity) из артефактов image-rota (прототип варианта C ADR-0009).

  python3 tools/rauc/build_bundle.py --boot boot.vfat --system system.ext4 --version V \
      --cert signing.cert.pem --key signing.key.pem --keyring keyring.pem --out B.raucb \
      [--compatible sheregesh-lab-pi4-dev-1] [--build COMMIT] [--manifest-only DIR]

Нужен `rauc` (Debian trixie: пакет rauc) и mksquashfs (squashfs-tools). На Mac rauc нет: испытание на файловом
образе (tools/deploy/rauc-check/run.py) собирает бандлы в контейнере Debian trixie на devbox. --manifest-only пишет
только каталог содержимого (manifest.raucm + ссылки на образы) — проверяется без rauc.
Образы boot/system переносятся в бандл как есть (жёсткая ссылка или копия); подпись — CMS по сертификату
подписанта, цепочка проверяется по keyring при сборке (`rauc bundle --keyring`).
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

COMPATIBLE = "sheregesh-lab-pi4-dev-1"


def manifest(compatible, version, build=None, description="Sheregesh lab image (dev)", fmt="verity"):
    if not re.fullmatch(r"[0-9A-Za-z.+~-]+", version):
        raise ValueError(f"версия {version!r}: допустимы цифры, буквы и .+~-")
    lines = ["[update]", f"compatible={compatible}", f"version={version}", f"description={description}"]
    if build:
        lines.append(f"build={build}")
    lines += ["", "[bundle]", f"format={fmt}", "", "[image.rootfs]", "filename=system.ext4", "",
              "[image.boot]", "filename=boot.vfat", ""]
    return "\n".join(lines)


def content_dir(dst: Path, boot: Path, system: Path, text: str):
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "manifest.raucm").write_text(text)
    for src, name in ((boot, "boot.vfat"), (system, "system.ext4")):
        try:
            os.link(src, dst / name)
        except OSError:
            shutil.copyfile(src, dst / name)
    return dst


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--boot", type=Path, required=True)
    ap.add_argument("--system", type=Path, required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--compatible", default=COMPATIBLE)
    ap.add_argument("--build")
    ap.add_argument("--cert", type=Path)
    ap.add_argument("--key", type=Path)
    ap.add_argument("--keyring", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--format", default="verity", choices=("verity", "plain"),
                    help="plain — только для испытаний с rauc 1.13 на ядре, где dm-verity несовместим (см. отчёт)")
    ap.add_argument("--check-purpose", default="codesign", help="как [keyring] check-purpose в system.conf")
    ap.add_argument("--manifest-only", type=Path)
    a = ap.parse_args(argv)
    text = manifest(a.compatible, a.version, a.build, fmt=a.format)
    if a.manifest_only:
        print(content_dir(a.manifest_only, a.boot, a.system, text))
        return 0
    if not (a.cert and a.key and a.out):
        ap.error("нужны --cert, --key, --out")
    if not shutil.which("rauc"):
        print("build_bundle: нет rauc (на Mac — tools/deploy/rauc-check/run.py собирает в контейнере)", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(dir=a.out.parent, prefix=".rauc-content-") as tmp:
        content_dir(Path(tmp) / "content", a.boot, a.system, text)
        a.out.unlink(missing_ok=True)
        cmd = ["rauc", "bundle", f"--cert={a.cert}", f"--key={a.key}", str(Path(tmp) / "content"), str(a.out)]
        if a.keyring:
            # Проверка цепочки при сборке — с тем же назначением сертификата, что на плате. `--keyring` один
            # проверяет по назначению smimesign; check-purpose RAUC берёт только из system.conf (rauc 1.13).
            conf = Path(tmp) / "verify.conf"
            conf.write_text(f"[system]\ncompatible={a.compatible}\nbootloader=noop\n\n"
                            f"[keyring]\npath={a.keyring.resolve()}\ncheck-purpose={a.check_purpose}\n")
            cmd[1:1] = [f"--conf={conf}"]
            cmd.insert(4, f"--keyring={a.keyring}")
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            print(r.stdout + r.stderr, file=sys.stderr)
            return r.returncode
    print(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
