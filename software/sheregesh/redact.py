# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Фильтр секретов для выгружаемых журналов пульта и удалённого блока (REQ-SW-005; шаблоны — docs/security/secrets.md)."""
import re

REDACT = re.compile(r"SHRG-CANARY-[A-Z]+-[A-Za-z0-9]+|-----BEGIN [A-Z ]*PRIVATE KEY-----"
                    r"|(?i:(password|passwd|token|secret)\s*[=:]\s*\S+)")


def redact(text: str) -> str:
    return REDACT.sub("[REDACTED]", text)
