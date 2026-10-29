# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Сборка Debian-пакета sheregesh (arch: all) для образа Pi 4 (ADR-0002: собственное ПО — .deb, без pip в образе).

  python3 tools/image/build_deb.py --out DIR [--version VER]

Только стандартная библиотека; dpkg-deb не нужен. Сборка детерминирована: время всех файлов —
SOURCE_DATE_EPOCH (по умолчанию время последнего коммита), владелец root:root, порядок файлов отсортирован.
Переводы .qm собираются lrelease (pyside6-lrelease, lrelease или /usr/lib/qt6/bin/lrelease) и входят в пакет:
на устройстве корневая ФС только для чтения.
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = "sheregesh"
PREFIX = "usr/lib/sheregesh"
SOURCES = ["software/sheregesh", "software/emulators/sheregesh_emulators", "software/console/sheregesh_console",
           "software/remote/sheregesh_remote", "tools/probe/s2probe.py", "tools/dev/soak.py"]
PYTHONPATH = ":".join(f"/{PREFIX}/{p}" for p in ("software", "software/emulators", "software/console", "software/remote"))
DEPENDS = [
    "python3 (>= 3.11)", "python3-yaml", "python3-jsonschema",
    "python3-pyside6.qtcore", "python3-pyside6.qtgui", "python3-pyside6.qtqml", "python3-pyside6.qtquick",
    "python3-pyside6.qtquickcontrols2",
    "qml6-module-qtquick", "qml6-module-qtquick-controls", "qml6-module-qtquick-layouts",
    "qml6-module-qtquick-templates", "qml6-module-qtquick-window", "qml6-module-qtqml-workerscript",
    "qt6-wayland", "fonts-dejavu-core",
    "gstreamer1.0-tools", "gstreamer1.0-plugins-base", "gstreamer1.0-plugins-good", "gstreamer1.0-x",
    "gstreamer1.0-plugins-bad",   # h264parse (videoparsersbad) — связка v4l2 H.264 на Pi 4; unixfdsink/unixfdsrc
    # Кадр видео пульта мимо Python (ADR-0015): декодер (отдельный процесс) → dmabuf по unixfd → glupload →
    # qml6glsink (элемент QML); конвейер приёма в процессе пульта строится через GObject Introspection
    "gstreamer1.0-gl", "gstreamer1.0-qt6", "python3-gi", "gir1.2-gstreamer-1.0", "gir1.2-gst-plugins-base-1.0",
    "qml6-module-qtquick-shapes",  # значки интерфейса Minimal (MiniIcon.qml)
    "util-linux",
    "cage", "seatd", "openssl",
    "nftables",   # ADR-0016 §3.3: таблица inet sheregesh_video и sheregesh-video-allow (python3-gi и gir уже выше)
    "chrony",     # ADR-0017: время пульт → блок без интернета (sheregesh-time); заменяет systemd-timesyncd
]
# Службы приложения запускаются через launcher (S3.1-DEPLOY): проверенный выпуск DEV deploy из
# /var/lib/sheregesh-deploy или заводской код /usr/lib/sheregesh. Системные команды — всегда из /usr.
LAUNCHER = f"/{PREFIX}/software/sheregesh/deploy/launcher.py"
LAUNCHED = {
    "usr/bin/sheregesh-console": "sheregesh_console",
    "usr/bin/sheregesh-remote-emu": "sheregesh_remote",
    "usr/bin/sheregesh-video-tx": "sheregesh.videosvc.tx",
    "usr/bin/sheregesh-video-rx": "sheregesh.videosvc.rx",
}
WRAPPERS = {   # команда → модуль (-m) или скрипт из /usr/lib/sheregesh (пробы S2 и длительный прогон)
    "usr/bin/sheregesh-probe": "tools/probe/s2probe.py",
    "usr/bin/sheregesh-soak": "tools/dev/soak.py",
    "usr/bin/sheregesh-link": "sheregesh.link.pki",       # ключи и сертификаты устройства (S3.1)
    "usr/bin/sheregesh-health": "sheregesh.health",       # диагностическая проверка здоровья (S3.1)
    "usr/bin/sheregesh-notify-relay": "sheregesh.notify_relay",   # sd_notify пульта из сеанса входа (S3.1)
    "usr/bin/sheregesh-deployd": "sheregesh.deploy.installer",    # установщик выпусков, root (S3.1-DEPLOY)
    "usr/bin/sheregesh-ota-agent": "sheregesh.update.ota",        # агент обновления A/B, root (S3.1-DEPLOY)
    "usr/bin/sheregesh-ota-client": "sheregesh.update.ota_client",  # клиент агента для sheregesh-ui
}
# Помощники root: межсетевой экран видео (ADR-0016, юнит sheregesh-video-allow@) и источники времени chrony
# (ADR-0017, юниты sheregesh-time-role и sheregesh-time-peer@). Помощник лежит в /usr/lib/sheregesh, команда в /usr/bin —
# обёртка python3 -I, как у прочих системных команд.
HELPERS = {"sheregesh-video-allow": "юнит sheregesh-video-allow@", "sheregesh-time": "юниты sheregesh-time-*"}
HELPER_SRC = Path(__file__).resolve().parent / "bin"
HELPER_DST = f"{PREFIX}/tools/image/bin"
NFT_DIR = Path(__file__).resolve().parent / "nft"
NFT_DST = "usr/share/sheregesh/nft"
UNITS = Path(__file__).resolve().parent / "units"
TMPFILES = Path(__file__).resolve().parent / "tmpfiles"      # /usr/lib/tmpfiles.d (ADR-0017: каталог drift chrony)
POSTINST = """#!/bin/sh
set -e
# Байт-код заранее: на устройстве корневая ФС только для чтения (ADR-0006).
python3 -m compileall -q -j 0 /usr/lib/sheregesh || true
"""
PRERM = """#!/bin/sh
set -e
find /usr/lib/sheregesh -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
"""


def source_date_epoch() -> int:
    if os.environ.get("SOURCE_DATE_EPOCH"):
        return int(os.environ["SOURCE_DATE_EPOCH"])
    r = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%ct"], capture_output=True, text=True)
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else 0


def default_version() -> str:
    """0.1.0.dev0 из pyproject.toml → 0.1.0~dev0+g<коммит> (в Debian «~» сортируется раньше релиза)."""
    ver = next(line.split('"')[1] for line in (ROOT / "pyproject.toml").read_text().splitlines()
               if line.startswith("version"))
    ver = ver.replace(".dev", "~dev")
    r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    return f"{ver}+g{r.stdout.strip()}" if r.returncode == 0 else ver


def build_info() -> dict:
    """Версия системной части для OTA (монотонна по времени коммита): «0.1.<SOURCE_DATE_EPOCH>»."""
    ver = next(line.split('"')[1] for line in (ROOT / "pyproject.toml").read_text().splitlines()
               if line.startswith("version"))
    major, minor = ver.split(".")[:2]
    r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    epoch = source_date_epoch()
    return {"commit": r.stdout.strip() if r.returncode == 0 else "unknown", "source_date_epoch": epoch,
            "ota_version": f"{int(major)}.{int(minor)}.{epoch}", "app_compatible": "sheregesh-lab-pi4-app-1"}


def lrelease():
    for tool in ("pyside6-lrelease", "lrelease", "/usr/lib/qt6/bin/lrelease"):
        path = shutil.which(tool) or (tool if os.access(tool, os.X_OK) else None)
        if path:
            return path
    venv = Path(sys.executable).with_name("pyside6-lrelease")
    return str(venv) if venv.exists() else None


def tracked_files():
    """Только файлы под git: в пакет не попадают __pycache__, локальные .qm и прочее неотслеживаемое."""
    r = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", *SOURCES], capture_output=True, check=True)
    return sorted(p for p in r.stdout.decode().split("\0") if p)


def build_tree(stage: Path):
    for rel in tracked_files():
        dst = stage / PREFIX / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, dst)
    tool = lrelease()
    if not tool:
        raise SystemExit("не найден lrelease: переводы .qm обязательны в пакете")
    for ts in sorted((stage / PREFIX).rglob("*.ts")):
        subprocess.run([tool, "-silent", str(ts), "-qm", str(ts.with_suffix(".qm"))], check=True, capture_output=True)
    for path, module in WRAPPERS.items():
        f = stage / path
        f.parent.mkdir(parents=True, exist_ok=True)
        target = f"/{PREFIX}/{module}" if module.endswith(".py") else f"-m {module}"
        # -s: без пользовательских site-packages (~/.local/…/*.pth): код команды — только из /usr (S3.1-DEPLOY).
        flags = "-s "
        f.write_text(f"#!/bin/sh\n# {PKG}: запуск {module} из /{PREFIX}\nexport PYTHONPATH={PYTHONPATH}\n"
                     f"exec /usr/bin/python3 {flags}{target} \"$@\"\n")
        f.chmod(0o755)
    for path, module in LAUNCHED.items():
        f = stage / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"#!/bin/sh\n# {PKG}: запуск {module} — выпуск DEV deploy или /{PREFIX} (launcher, S3.1-DEPLOY)\n"
                     f"exec /usr/bin/python3 -I {LAUNCHER} {module} \"$@\"\n")
        f.chmod(0o755)
    for name, used_by in HELPERS.items():
        helper = stage / HELPER_DST / name
        helper.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(HELPER_SRC / name, helper)
        helper.chmod(0o755)
        wrap = stage / "usr/bin" / name
        wrap.write_text(f"#!/bin/sh\n# {PKG}: запуск {name} из /{PREFIX} (root, {used_by})\n"
                        f"exec /usr/bin/python3 -I /{HELPER_DST}/{name} \"$@\"\n")
        wrap.chmod(0o755)
    for nft in sorted(NFT_DIR.glob("*.nft")):
        dst = stage / NFT_DST / nft.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(nft, dst)
    info = stage / "usr/share/sheregesh/build.json"
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text(json.dumps(build_info(), ensure_ascii=False, indent=1) + "\n")
    for conf in sorted(TMPFILES.glob("*.conf")):
        dst = stage / "usr/lib/tmpfiles.d" / conf.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(conf, dst)
    for unit in sorted([*UNITS.glob("*.service"), *UNITS.glob("*.timer"), *UNITS.glob("*.socket")]):
        dst = stage / "usr/lib/systemd/system" / unit.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(unit, dst)
    doc = stage / "usr/share/doc" / PKG / "copyright"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
                   "Upstream-Name: sheregesh\n\nFiles: *\nCopyright: 2026 Sheregesh contributors\n"
                   "License: GPL-3.0-or-later\n On Debian systems see /usr/share/common-licenses/GPL-3.\n")


def tar_bytes(stage: Path, epoch: int) -> bytes:
    """tar.xz с фиксированными временем, владельцем и порядком (каталог идёт раньше своего содержимого)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz", format=tarfile.GNU_FORMAT) as tar:
        for src in sorted(stage.rglob("*"), key=lambda p: p.relative_to(stage).as_posix()):
            ti = tarfile.TarInfo("./" + src.relative_to(stage).as_posix())
            ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = epoch, 0, 0, "root", "root"
            if src.is_dir():
                ti.type, ti.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(ti)
            else:
                data = src.read_bytes()
                ti.size, ti.mode = len(data), 0o755 if os.access(src, os.X_OK) else 0o644
                tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def ar_member(name: str, data: bytes, epoch: int) -> bytes:
    head = f"{name:<16}{epoch:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode()
    assert len(head) == 60
    return head + data + (b"\n" if len(data) % 2 else b"")


def build(out_dir: Path, version: str) -> Path:
    epoch = source_date_epoch()
    with tempfile.TemporaryDirectory() as tmp:
        stage, ctrl = Path(tmp) / "data", Path(tmp) / "control"
        stage.mkdir()
        ctrl.mkdir()
        build_tree(stage)
        files = sorted(p for p in stage.rglob("*") if p.is_file())
        size_kb = sum(p.stat().st_size for p in files) // 1024 + 1
        (ctrl / "control").write_text(
            f"Package: {PKG}\nVersion: {version}\nArchitecture: all\nMaintainer: Sheregesh contributors\n"
            f"Installed-Size: {size_kb}\nDepends: {', '.join(DEPENDS)}\nSection: misc\nPriority: optional\n"
            "Description: Sheregesh 1.0 laboratory console and remote unit emulator\n"
            " Laboratory prototype: synthetic data only, loopback-only link (ADR-0007).\n"
            " Not a means of controlling aircraft or any physical device.\n")
        (ctrl / "md5sums").write_text("".join(f"{hashlib.md5(p.read_bytes()).hexdigest()}  "
                                              f"{p.relative_to(stage)}\n" for p in files))
        for name, text in (("postinst", POSTINST), ("prerm", PRERM)):
            (ctrl / name).write_text(text)
            (ctrl / name).chmod(0o755)
        deb = b"!<arch>\n" + ar_member("debian-binary", b"2.0\n", epoch) + \
            ar_member("control.tar.xz", tar_bytes(ctrl, epoch), epoch) + \
            ar_member("data.tar.xz", tar_bytes(stage, epoch), epoch)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{PKG}_{version}_all.deb"
    out.write_bytes(deb)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--version", default=None)
    a = ap.parse_args(argv)
    deb = build(a.out, a.version or default_version())
    print(f"{deb}  sha256={hashlib.sha256(deb.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
