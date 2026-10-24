#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Испытание RAUC на файловом образе диска image-rota (прототип варианта C ADR-0009, L3). С Mac; без плат Pi.

  python3 tools/deploy/rauc-check/run.py [--docker-host devbox-lan] [--log F] [--result F]

Копирует код (software/sheregesh, tools/rauc, check.py) на Linux-хост с Docker в уникальный каталог
/srv/build/rauc-ab-<id>, запускает check.py в привилегированном контейнере debian:trixie (loop-устройства,
dm-verity для бандлов verity), где пакеты rauc/rauc-service ставятся только внутри контейнера, забирает вывод и
result.json и удаляет рабочий каталог. Ключи владельца не используются: тестовые CA и подписанты создаются в
контейнере (tools/rauc/testpki.py) и исчезают вместе с каталогом.
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PKGS = ("rauc rauc-service dbus squashfs-tools dosfstools mtools e2fsprogs fdisk rsync openssl python3 dmsetup "
        "ca-certificates")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--docker-host", default="devbox-lan")
    ap.add_argument("--image", default="debian:trixie")
    ap.add_argument("--log", type=Path)
    ap.add_argument("--result", type=Path)
    ap.add_argument("--format", default="verity", choices=("verity", "plain"))
    ap.add_argument("--keep", action="store_true", help="не удалять рабочий каталог (отладка)")
    a = ap.parse_args(argv)
    remote = f"/srv/build/rauc-ab-{uuid.uuid4().hex[:10]}"
    h = a.docker_host
    with tempfile.TemporaryDirectory() as tmp:
        ctx = Path(tmp)
        shutil.copytree(ROOT / "software/sheregesh", ctx / "software/sheregesh",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "tools/rauc", ctx / "tools/rauc", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(HERE / "check.py", ctx / "check.py")
        subprocess.run(["ssh", h, f"mkdir -p {remote}"], check=True)
        subprocess.run(["scp", "-q", "-r", f"{ctx}/.", f"{h}:{remote}/"], check=True)
    inner = (f"set -e; export DEBIAN_FRONTEND=noninteractive; apt-get update -qq >/dev/null; "
             f"apt-get install -y -qq --no-install-recommends {PKGS} >/dev/null 2>&1; "
             # D-Bus-активация запустила бы rauc service с /etc/rauc/system.conf — нужен только наш экземпляр
             f"rm -f /usr/share/dbus-1/system-services/de.pengutronix.rauc.service; "
             f"mkdir -p /run/dbus; dbus-daemon --system --fork; "
             f"echo \"контейнер: $(. /etc/os-release; echo $PRETTY_NAME), $(rauc --version), ядро $(uname -r)\"; "
             f"python3 -I -u /work/check.py")
    script = (f"cd {remote} && nice -n 19 docker run --rm --privileged -v /dev:/dev -v {remote}:/work "
              f"-e RAUC_BUNDLE_FORMAT={a.format} {a.image} "
              f"bash -c '{inner}'; rc=$?; cat {remote}/result.json > /tmp/{Path(remote).name}.json 2>/dev/null; ")
    if not a.keep:
        script += (f"docker run --rm -v /srv/build:/b {a.image} rm -rf /b/{Path(remote).name}; "
                   f"test -e {remote} && echo 'каталог НЕ удалён' || echo 'рабочий каталог {remote} удалён'; ")
    script += "exit $rc"
    r = subprocess.run(["ssh", h, script], capture_output=True, text=True)
    out = r.stdout + (("\n[stderr]\n" + r.stderr) if r.stderr.strip() else "")
    print(out)
    res = subprocess.run(["ssh", h, f"cat /tmp/{Path(remote).name}.json; rm -f /tmp/{Path(remote).name}.json"],
                         capture_output=True, text=True).stdout
    head = (f"# tools/deploy/rauc-check/run.py --image {a.image} --format {a.format} → {h}:{remote} "
            f"(docker, privileged), rc={r.returncode}\n")
    if a.log:
        a.log.write_text(head + out)
    if a.result and res.strip():
        a.result.write_text(json.dumps(json.loads(res), ensure_ascii=False, indent=1) + "\n")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
