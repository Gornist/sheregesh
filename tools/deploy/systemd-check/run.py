#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Испытание DEV deploy в контейнере с настоящими systemd + polkit + D-Bus (S3.1-DEPLOY, L3, не плата).

  python3 tools/deploy/systemd-check/run.py --work DIR [--docker-host devbox-lan] [--log FILE]

Готовит на Mac: пакет sheregesh (.deb) текущего коммита, тестовые ключи (создаются на прогон, в репозиторий не
попадают), пакеты выпусков good1/good2/bad/crash/foreign и подписи; передаёт их на Linux-хост с Docker
(по умолчанию devbox-lan), собирает образ Debian trixie, запускает systemd в контейнере (--privileged) и
сценарий check.sh. Контейнер и образ удаляются после прогона. Никаких действий на платах Pi.
"""
import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT / "tools/deploy"), str(ROOT / "tools/image")]
import build_deb  # noqa: E402
import shdeploy  # noqa: E402
from sheregesh.deploy import bundle, sig  # noqa: E402

NAME = "shrg-deploy-check"


def keypair(d, name):
    k = d / name
    if not k.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"systemd-check-{name}", "-f", str(k)],
                       check=True)
    return k


def bundles(ctx, owner, foreign):
    files = shdeploy.collect_files()
    commit = shdeploy.git("rev-parse", "HEAD")
    when = int(shdeploy.git("log", "-1", "--format=%ct"))
    base = f"{build_deb.default_version().split('+')[0]}+g{commit[:12]}"
    target = "software/remote/sheregesh_remote/__main__.py"      # обе службы в контейнере исполняют sheregesh_remote
    main = files[target][0]
    variants = {
        "good1": (base, files),
        "good2": (base + ".2", {**files, "software/sheregesh/__init__.py":
                                (files["software/sheregesh/__init__.py"][0] + b"# good2\n", 0o644)}),
        "bad": (base + ".bad", {**files, target: (b"raise SystemExit(3)\n", 0o644)}),
        "crash": (base + ".crash", {**files, target:
                                    (b"import os, threading\nthreading.Timer(8, lambda: os._exit(1)).start()\n" + main,
                                     0o644)}),
    }
    out = ctx / "bundles"
    out.mkdir()
    sums = {}
    for name, (version, fs) in variants.items():
        data = bundle.build_bundle(fs, version, commit, when)
        (out / f"{name}.tar.xz").write_bytes(data)
        (out / f"{name}.tar.xz.sig").write_bytes(sig.sign(data, owner, sig.NS_APP))
        (out / f"{name}.id").write_text(bundle.release_id(version))
        sums[name] = hashlib.sha256(data).hexdigest()
    shutil.copy(out / "good1.tar.xz", out / "foreign.tar.xz")
    (out / "foreign.tar.xz.sig").write_bytes(sig.sign((out / "good1.tar.xz").read_bytes(), foreign, sig.NS_APP))
    (out / "foreign.id").write_text("foreign")
    return sums


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--docker-host", default="devbox-lan")
    ap.add_argument("--log", type=Path)
    ap.add_argument("--base", default="debian:trixie", help="базовый образ Debian trixie (если Docker Hub недоступен)")
    a = ap.parse_args(argv)
    ctx = a.work / "ctx"
    if ctx.exists():
        shutil.rmtree(ctx)
    ctx.mkdir(parents=True)
    keys = a.work / "keys"
    keys.mkdir(exist_ok=True)
    owner, foreign = keypair(keys, "owner"), keypair(keys, "foreign")
    deb = build_deb.build(ctx, build_deb.default_version())
    (ctx / "allowed_signers").write_text(sig.allowed_signers_line(owner.with_suffix(".pub").read_text()))
    for f in ("Dockerfile", "check.sh", "console-container.conf", "emu-container.conf"):
        shutil.copy(HERE / f, ctx / f)
    shutil.copy(ROOT / "os/layer/sheregesh-dev-access.d/50-sheregesh-dev.rules", ctx)
    sums = bundles(ctx, owner, foreign)
    print(f"пакет {deb.name} sha256={hashlib.sha256(deb.read_bytes()).hexdigest()}")
    for n, h in sums.items():
        print(f"выпуск {n}: sha256={h}")
    remote = f"/tmp/{NAME}"
    subprocess.run(["ssh", a.docker_host, f"rm -rf {remote} && mkdir -p {remote}"], check=True)
    subprocess.run(["scp", "-q", "-r", f"{ctx}/.", f"{a.docker_host}:{remote}/"], check=True)
    script = (f"set -e; cd {remote}; nice -n 19 docker build -q --build-arg BASE={a.base} -t {NAME} . >/dev/null; "
              f"docker rm -f {NAME} >/dev/null 2>&1 || true; "
              f"docker run -d --privileged --cgroupns=private --name {NAME} {NAME} >/dev/null; sleep 5; "
              f"set +e; docker exec {NAME} /opt/check/check.sh; rc=$?; "
              f"docker exec {NAME} journalctl --no-pager -u 'sheregesh-deploy@*' -o cat | tail -40; "
              f"docker rm -f {NAME} >/dev/null; docker rmi {NAME} >/dev/null; cd /; rm -rf {remote}; exit $rc")
    r = subprocess.run(["ssh", a.docker_host, script], capture_output=True, text=True)
    out = r.stdout + (("\n" + r.stderr) if r.stderr.strip() else "")
    print(out)
    if a.log:
        a.log.write_text(out)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
