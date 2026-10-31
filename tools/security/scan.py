#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Проверки репозитория без внешних зависимостей.

  scan.py secrets [PATH...]  — шаблоны секретов в отслеживаемых файлах (REQ-REL-011)
  scan.py history [REF]      — те же шаблоны во всех версиях всех файлов, достижимых из REF (по умолчанию v2):
                               перед открытием репозитория и перед публичным выпуском (REQ-REL-011)

Код возврата 1 при находках. Находка истории с записанным решением (docs/security/history-findings.md: строка с
blob и шаблоном) не считается неразобранной.
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


DECISIONS = ROOT / "docs/security/history-findings.md"


def history_blobs(ref):
    """(blob, путь) каждой версии каждого файла, достижимой из ref — один раз на blob."""
    out = subprocess.run(["git", "rev-list", "--objects", ref], cwd=ROOT, check=True, capture_output=True,
                         text=True).stdout
    seen, res = set(), []
    for line in out.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[0] not in seen:
            seen.add(parts[0])
            res.append((parts[0], parts[1]))
    return res


def scan_history(ref):
    blobs = history_blobs(ref)
    proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    findings = []
    for sha, path in blobs:
        proc.stdin.write(sha.encode() + b"\n")
        proc.stdin.flush()
        header = proc.stdout.readline().split()
        size = int(header[2]) if len(header) == 3 else 0
        data = proc.stdout.read(size)
        proc.stdout.read(1)
        if len(header) != 3 or header[1] != b"blob" or b"\0" in data[:4096] or path == "tools/security/scan.py":
            continue
        text = data.decode("utf-8", errors="replace")
        for name, pat in SECRET_PATTERNS:
            if pat.search(text) and not (name == "canary" and path.startswith(CANARY_ALLOWED)):
                findings.append((sha, path, name))
    proc.stdin.close()
    proc.wait()
    decided = DECISIONS.read_text(encoding="utf-8") if DECISIONS.exists() else ""
    open_ = [f for f in findings if f"{f[0][:12]} {f[2]}" not in decided]
    return blobs, findings, open_


def main(argv):
    if argv and argv[0] == "history":
        ref = argv[1] if len(argv) > 1 else "v2"
        blobs, findings, open_ = scan_history(ref)
        for sha, path, name in findings:
            print(f"{sha[:12]} {name} {path}" + ("" if (sha, path, name) in open_ else "  [решение записано]"))
        print(f"history {ref}: {len(blobs)} объектов, {len(findings)} находок, неразобранных {len(open_)}")
        return 1 if open_ else 0
    if not argv or argv[0] != "secrets":
        print(__doc__)
        return 2
    files = argv[1:] or tracked_files()
    findings = scan_secrets(files)
    for f in findings:
        print(f)
    print(f"{argv[0]}: {len(findings)} находок в {len(files)} файлах")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
