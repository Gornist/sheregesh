# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Паспорт устройства (REQ-SYS-007): версия ПО, сборка образа, DEV-выпуск, ревизия железа, серийный номер платы,
версия протокола. Только локальные файлы — работает без сети. Один источник для `sheregesh-info`, экрана «О системе»
пульта и hello канала управления (hardware, device_id), чтобы данные блока на пульте совпадали с его `sheregesh-info`.

  sheregesh-info [--json]        (до нового образа: python3 -m sheregesh.info)
"""
import argparse
import json
import sys
from pathlib import Path

from . import PROTOCOL_VERSION, __semver__, __version__

HW_REVISION = "HW-1.0 Rev A"            # аппаратная спецификация (docs/hardware/SHEREGESH-HARDWARE-1.0.md)
BUILD_JSON = Path("/usr/share/sheregesh/build.json")
DEPLOY_CURRENT = Path("/var/lib/sheregesh-deploy/current")
DT = Path("/proc/device-tree")
ROLE_FILES = {"console": Path("~/.config/sheregesh/console.role"), "remote": Path("~/.config/sheregesh/remote.role")}


def _dt(name, root=DT):
    try:
        return (root / name).read_bytes().rstrip(b"\0").decode("ascii", "replace").strip()
    except OSError:
        return ""


def board(root=DT):
    """Модель и серийный номер платы из device-tree; вне Raspberry Pi — пусто."""
    return {"model": _dt("model", root), "serial": _dt("serial-number", root)}


def hardware(b=None):
    """Строка железа для hello и экрана: ревизия спецификации и фактическая плата (или «emulator» вне платы).
    Префикс HW-1.0 проверяет матрица совместимости (compat-matrix.yaml, hardware.accepted_prefixes)."""
    b = board() if b is None else b
    return f"{HW_REVISION} ({b['model']})" if b["model"] else f"{HW_REVISION} (emulator)"


def device_id(role, b=None):
    """Идентификатор устройства: роль и серийный номер платы; вне платы — прежний идентификатор эмулятора."""
    b = board() if b is None else b
    return f"{role}-{b['serial']}" if b["serial"] else f"{role}-emu-01"


def image(path=BUILD_JSON):
    """Сборка системного образа (build.json образа): коммит и версия OTA; вне образа — пусто."""
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {"commit": "", "ota_version": ""}
    return {"commit": str(d.get("commit") or "")[:12], "ota_version": str(d.get("ota_version") or "")}


def release(current=DEPLOY_CURRENT):
    """Активный DEV-выпуск приложения (поверх кода образа) или «»."""
    try:
        return Path(current).resolve(strict=True).name
    except OSError:
        return ""


def role():
    for name, p in ROLE_FILES.items():
        if p.expanduser().exists():
            return name
    return ""


def collect(role_name=None, *, dt=DT, build_json=BUILD_JSON, deploy_current=DEPLOY_CURRENT):
    b = board(dt)
    r = role() if role_name is None else role_name
    return {"role": r, "software": __semver__, "package": __version__, "protocol": PROTOCOL_VERSION, "image": image(build_json),
            "release": release(deploy_current), "hardware": hardware(b), "serial": b["serial"],
            "device_id": device_id(r or "device", b)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    info = collect()
    if a.json:
        print(json.dumps(info, ensure_ascii=False))
        return 0
    img = info["image"]
    rows = [("Роль", info["role"] or "—"), ("ПО", info["software"]), ("Протокол", info["protocol"]),
            ("Сборка образа", f"{img['ota_version']} · {img['commit']}" if img["commit"] else "—"),
            ("DEV-выпуск", info["release"] or "—"), ("Железо", info["hardware"]),
            ("Серийный номер", info["serial"] or "—"), ("ID устройства", info["device_id"])]
    for k, v in rows:
        print(f"{k:16} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
