# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Совместимость версий протокола SHRG-LAB (ADR-0007, REQ-SW-003, REQ-SYS-008; правило — CHG-012).

Правило:
  1. MAJOR обеих сторон одинаков и поддерживается матрицей;
  2. MINOR второй стороны не ниже min_peer_minor из матрицы для этого MAJOR;
  3. версия ПО второй стороны не входит в software.rejected, HW-версия — в hardware.accepted_prefixes;
  4. сессия работает на версии MAJOR.min(MINOR обеих сторон); сообщения и поля новее этой версии
     не исполняются — получатель их только учитывает (журнал, счётчик).
"""
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_VER = re.compile(r"^(\d+)\.(\d+)$")
MATRIX_PATH = Path(__file__).with_name("compat-matrix.yaml")


def parse(version: str) -> tuple[int, int]:
    m = _VER.match(version or "")
    if not m:
        raise ValueError(f"неверная версия протокола {version!r}")
    return int(m.group(1)), int(m.group(2))


def load_matrix(path=MATRIX_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Negotiation:
    compatible: bool
    session: str | None     # согласованная версия сессии
    reason: str             # ok | bad-version | major | minor | software | hardware


def negotiate(local: str, peer: str, peer_software=None, peer_hardware=None, matrix=None) -> Negotiation:
    matrix = matrix if matrix is not None else load_matrix()
    try:
        lmaj, lmin = parse(local)
        pmaj, pmin = parse(peer)
    except (ValueError, TypeError):
        return Negotiation(False, None, "bad-version")
    supported = {int(k): v for k, v in matrix["protocol"]["supported"].items()}
    if lmaj != pmaj or pmaj not in supported:
        return Negotiation(False, None, "major")
    if pmin < supported[pmaj].get("min_peer_minor", 0):
        return Negotiation(False, None, "minor")
    if peer_software is not None and peer_software in (matrix.get("software", {}).get("rejected") or []):
        return Negotiation(False, None, "software")
    prefixes = (matrix.get("hardware") or {}).get("accepted_prefixes")
    if peer_hardware is not None and prefixes and not any(peer_hardware.startswith(p) for p in prefixes):
        return Negotiation(False, None, "hardware")
    return Negotiation(True, f"{lmaj}.{min(lmin, pmin)}", "ok")


def compatible(local: str, peer: str, matrix=None) -> bool:
    """Совместимость только по версиям протокола (без проверки ПО и HW)."""
    return negotiate(local, peer, matrix=matrix).compatible
