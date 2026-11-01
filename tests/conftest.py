# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Общие настройки тестов и сбор доказательств по TEST-ID (ADR-0008).

Каждый тест с маркером test_id попадает в reports/evidence.json с результатом и уровнем L2/L3.
Это доказательство уровня CI. Статус в реестре требований этот файл НЕ меняет: продуктовые Must
требуют L4 (стенд), см. TEST-SPEC.
"""
import json
import os
import platform
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

_results = {}


@pytest.fixture(autouse=True)
def _no_stand_lookup(request, monkeypatch):
    """Тесты не ищут платы стенда: «auto» в --console/--remote разрешается в имя платы без SSH и сканирования сети
    (иначе на машине со стендом тест ходит на настоящую плату, а в CI падает «плата не найдена»).
    Тесты tests/tools (сам поиск, отказ shdeploy) — без подмены."""
    if "/tests/tools/" in str(request.node.fspath):      # тесты инструментов проверяют сам поиск и отказ (shdeploy)
        return
    import sys
    tools = str(ROOT / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from stand import boards
    monkeypatch.setattr(boards, "resolve", lambda board, host=None, **kw: board if host in (None, boards.AUTO) else host)


def _git_rev():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    marks = [m.args[0] for m in item.iter_markers("test_id")]
    if not marks or (rep.when != "call" and not rep.failed):
        return
    level = "L3" if "/system/" in str(item.fspath) or "/gui/" in str(item.fspath) else "L2"
    for tid in marks:
        entry = _results.setdefault(tid, {"level": level, "passed": 0, "failed": 0, "tests": []})
        entry["passed" if rep.passed else "failed"] += 1
        entry["tests"].append({"name": item.nodeid, "outcome": rep.outcome})
        if level == "L3":
            entry["level"] = "L3"


def pytest_sessionfinish(session, exitstatus):
    if not _results:
        return
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    doc = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "git": _git_rev(),
           "hw": f"emulated/{platform.machine()}", "python": platform.python_version(),
           "note": "Доказательства уровня CI (L2/L3). Не заменяют испытания L4 по TEST-SPEC.",
           "tests": dict(sorted(_results.items()))}
    (out / "evidence.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
