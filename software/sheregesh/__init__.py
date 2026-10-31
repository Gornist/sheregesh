# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Общий пакет «Шерегеш» 1.0 для пульта и удалённого блока."""

import re

__version__ = "0.1.0.dev0"            # версия пакета Python (PEP 440); снаружи — SemVer (semver)
PROTOCOL_VERSION = "1.5"

_PEP440 = re.compile(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+))?(?:\.post(\d+))?(?:\.dev(\d+))?(?:\+([0-9A-Za-z.]+))?")
_PRE = {"a": "alpha", "b": "beta", "rc": "rc"}


def semver(version=__version__):
    """Версия для людей и релиза (REQ-REL-010, SYS-007): PEP 440 → SemVer 2.0. 0.1.0.dev0 → 0.1.0-dev.0,
    0.1.0rc1 → 0.1.0-rc.1, локальная часть «+g…» — метаданные сборки. Неразборчивая строка — ValueError."""
    m = _PEP440.fullmatch(str(version))
    if m is None:
        raise ValueError(f"не версия PEP 440 этого проекта: {version!r}")
    core, pre, pre_n, post, dev, local = m.groups()
    tags = ([_PRE[pre], pre_n] if pre else []) + (["post", post] if post else []) + (["dev", dev] if dev else [])
    return core + ("-" + ".".join(tags) if tags else "") + ("+" + local if local else "")


def semver_or_raw(version):
    """Для отображения версии второй стороны: SemVer, а неразборчивую строку — как есть."""
    try:
        return semver(version)
    except ValueError:
        return str(version or "")


__semver__ = semver(__version__)
