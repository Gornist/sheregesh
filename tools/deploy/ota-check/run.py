#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Испытание системного обновления на файловом образе диска (S3.2-OTA, L3). С Mac владельца; без плат Pi.

  python3 tools/deploy/ota-check/run.py --base IMG.img.zst --payload P.shota [--docker-host devbox-lan] [--log F]

Готовит на Mac: allowed_signers (открытый ключ ~/.ssh/sheregesh-deploy.pub), подпись манифеста посторонним ключом,
разрешение переключения для ТЕСТОВОГО серийного номера (к платам неприменимо) — подписано ключом владельца на Mac,
закрытый ключ никуда не передаётся. Передаёт образ, поток и код агента на Linux-хост с Docker (каталог на /srv/build),
запускает check.py в привилегированном контейнере (loop-устройства), забирает result.json и удаляет рабочий каталог.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "software"))
from sheregesh.deploy import sig  # noqa: E402

SERIAL = "TESTSERIAL00ota"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", type=Path, required=True, help="образ плат .img.zst (до обновления)")
    ap.add_argument("--payload", type=Path, required=True)
    ap.add_argument("--key", type=Path, default=Path("~/.ssh/sheregesh-deploy").expanduser())
    ap.add_argument("--docker-host", default="devbox-lan")
    ap.add_argument("--remote", default="/srv/build/sheregesh-ota-check")
    ap.add_argument("--image", default="ghcr.io/armbian/docker-armbian-build:armbian-debian-trixie-latest")
    ap.add_argument("--log", type=Path)
    ap.add_argument("--result", type=Path)
    a = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        ctx = Path(tmp)
        (ctx / "allowed_signers").write_text(sig.allowed_signers_line(a.key.with_suffix(".pub").read_text()))
        with open(a.payload, "rb") as f:
            f.readline()
            manifest = f.read(int(f.readline()))
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(ctx / "foreign")], check=True)
        (ctx / "foreign.sig").write_bytes(sig.sign(manifest, ctx / "foreign", sig.NS_OTA))
        token = json.dumps({"kind": "sheregesh-ota-enable", "serial": SERIAL, "switching": True,
                            "board": "file-image-test"}).encode()
        (ctx / "enable.json").write_bytes(token)
        (ctx / "enable.json.sig").write_bytes(sig.sign(token, a.key, sig.NS_OTA_ENABLE))
        shutil.copy(HERE / "check.py", ctx / "check.py")
        shutil.copytree(ROOT / "software/sheregesh", ctx / "software/sheregesh",
                        ignore=shutil.ignore_patterns("__pycache__"))
        for f in ("foreign", "foreign.pub"):
            (ctx / f).unlink()                          # посторонний ключ не нужен на хосте
        h = a.docker_host
        subprocess.run(["ssh", h, f"rm -rf {a.remote} && mkdir -p {a.remote}"], check=True)
        subprocess.run(["scp", "-q", "-r", f"{ctx}/.", f"{h}:{a.remote}/"], check=True)
        subprocess.run(["scp", "-q", str(a.base), f"{h}:{a.remote}/base.img.zst"], check=True)
        subprocess.run(["scp", "-q", str(a.payload), f"{h}:{a.remote}/payload.shota"], check=True)
    script = (f"cd {a.remote} && nice -n 19 docker run --rm --privileged -v /dev:/dev -v {a.remote}:/work "
              f"{a.image} python3 -I /work/check.py; rc=$?; cat {a.remote}/result.json > /tmp/sheregesh-ota-result.json "
              f"2>/dev/null; sudo -n rm -rf {a.remote} 2>/dev/null || docker run --rm -v /srv/build:/b {a.image} "
              f"rm -rf /b/{Path(a.remote).name}; exit $rc")
    r = subprocess.run(["ssh", h, script], capture_output=True, text=True)
    out = r.stdout + (("\n" + r.stderr) if r.stderr.strip() else "")
    print(out)
    res = subprocess.run(["ssh", h, "cat /tmp/sheregesh-ota-result.json; rm -f /tmp/sheregesh-ota-result.json"],
                         capture_output=True, text=True).stdout
    meta = {"payload": a.payload.name, "payload_sha256": hashlib.sha256(a.payload.read_bytes()).hexdigest(),
            "base": a.base.name}
    if a.log:
        a.log.write_text(json.dumps(meta, ensure_ascii=False) + "\n" + out)
    if a.result and res.strip():
        a.result.write_text(json.dumps({**meta, **json.loads(res)}, ensure_ascii=False, indent=1))
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
