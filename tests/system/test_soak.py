# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Длительная работа программного стенда (L3): память, зависания, ошибки QML, сироты, восстановление.

Длительность — SHEREGESH_SOAK_S (по умолчанию 60 с; локально запускайте tools/dev/sheregesh-emu --soak 900).
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/dev"))


@pytest.mark.test_id("TEST-SW-001")
def test_soak_two_processes_with_faults(qapp, tmp_path):
    import soak
    seconds = float(os.environ.get("SHEREGESH_SOAK_S", "60"))
    r = soak.run(seconds, ROOT / "reports" / "soak")
    assert r["verdict"]["no_qml_errors"], r["qml_problems"]
    assert r["verdict"]["responsive"], f"пауза событийного цикла {r['loop_worst_ms']} мс"
    assert r["verdict"]["no_orphans"], f"остались процессы GStreamer: {r['leftover_gst']}"
    assert r["verdict"]["recovered"], r["final"]
    assert r["verdict"]["memory"], (r["console_rss_growth_mb"], r["agent_rss_growth_mb"])
    assert any(s["peer"] == "lost" for s in r["samples"]) or seconds < 30, "неисправности должны были проявиться"
