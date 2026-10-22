#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Проверки репозитория без внешних зависимостей.

  scan.py secrets [PATH...]  — шаблоны секретов в отслеживаемых файлах (REQ-UPD-009, REL-011)
  scan.py scope              — признаки компонентов вне объёма 1.0 в software/ и tools/ (REQ-SYS-003)

Код возврата 1 при находках. Это базовая проверка CI; полное сканирование истории перед
публикацией (REL-011) выполняется отдельно.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SECRET_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("preset-password", re.compile(r"(?i)\b[A-Z_]*PASSWORD\s*=\s*['\"][^'\"]+['\"]")),
    ("canary", re.compile(r"SHRG-CANARY-[A-Z]+-[A-Za-z0-9]{8,}")),
]
# Канареечные строки допустимы только в тестовых данных.
CANARY_ALLOWED = ("tests/",)

# Признаки из docs/security/out-of-scope-components.md. Документация может их упоминать, код — нет.
SCOPE_PATTERNS = re.compile(
    r"(?i)\b(crsf|expresslrs|elrs|sbus|crossfire|rx5808|vrx|srb[-_]head|srb_core|bridge-fw|sheregesh-radio)\b")
SCOPE_DIRS = ("software/", "tools/", "tests/")
SCOPE_SELF = {"tools/security/scan.py", "tests/tools/test_scan.py"}


def tracked_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True).stdout
    return [p for p in out.decode().split("\0") if p]


def read_text(rel):
    data = (ROOT / rel).read_bytes()
    if b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace")


def scan_secrets(files):
    findings = []
    for rel in files:
        text = read_text(rel)
        if text is None:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for name, pat in SECRET_PATTERNS:
                if pat.search(line):
                    if name == "canary" and rel.startswith(CANARY_ALLOWED):
                        continue
                    if rel == "tools/security/scan.py":
                        continue
                    findings.append(f"{rel}:{n}: {name}")
    return findings


def scan_scope(files):
    findings = []
    for rel in files:
        if not rel.startswith(SCOPE_DIRS) or rel in SCOPE_SELF:
            continue
        text = read_text(rel)
        if text is None:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            m = SCOPE_PATTERNS.search(line)
            if m:
                findings.append(f"{rel}:{n}: {m.group(0)}")
    return findings


def main(argv):
    if not argv or argv[0] not in ("secrets", "scope"):
        print(__doc__)
        return 2
    files = argv[1:] or tracked_files()
    findings = scan_secrets(files) if argv[0] == "secrets" else scan_scope(files)
    for f in findings:
        print(f)
    print(f"{argv[0]}: {len(findings)} находок в {len(files)} файлах")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
