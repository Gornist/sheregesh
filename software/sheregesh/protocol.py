# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Правило совместимости версий протокола SHRG-LAB (ADR-0007, REQ-SW-003)."""
import re

_VER = re.compile(r"^(\d+)\.(\d+)$")


def parse(version: str) -> tuple[int, int]:
    m = _VER.match(version or "")
    if not m:
        raise ValueError(f"неверная версия протокола {version!r}")
    return int(m.group(1)), int(m.group(2))


def compatible(local: str, peer: str) -> bool:
    """Одинаковый MAJOR — совместимо; разный или неразборчивый — нет."""
    try:
        return parse(local)[0] == parse(peer)[0]
    except ValueError:
        return False
