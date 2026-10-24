# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Инструмент аппаратной приёмки DEV deploy (tools/probe/check_pi_deploy.py): без плат."""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "probe"))
import check_pi_deploy as cp  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def git(*a):
    return subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True, check=True).stdout.strip()


def test_variant_is_detached_commit_changing_one_file():
    head, branch, status = git("rev-parse", "HEAD"), git("rev-parse", "--abbrev-ref", "HEAD"), git("status", "--porcelain")
    rev = cp._variant("t", "software/console/sheregesh_console/__main__.py", lambda s: s + "# TEST ONLY\n")
    assert git("rev-parse", f"{rev}^") == head
    assert git("diff", "--name-only", head, rev).splitlines() == ["software/console/sheregesh_console/__main__.py"]
    assert git("show", f"{rev}:software/console/sheregesh_console/__main__.py").endswith("# TEST ONLY")
    # рабочее дерево, индекс и ветка не тронуты; ссылок на коммит нет
    assert git("rev-parse", "HEAD") == head and git("rev-parse", "--abbrev-ref", "HEAD") == branch
    assert git("status", "--porcelain") == status
    assert rev not in git("for-each-ref", "--format=%(objectname)")


def test_polkit_negatives_cover_privileged_paths():
    cmds = " ".join(cp.NEGATIVE.values())
    for needle in ("sheregesh-deploy@shell", "stop sheregesh-deploy@install", "restart sheregesh-deploy@install",
                   "systemd-run --unit=sheregesh-deploy@install", "set-environment", "daemon-reload", "stop ssh"):
        assert needle in cmds


def test_scenario_failure_is_error_and_skip_allowed(monkeypatch, tmp_path):
    monkeypatch.setattr(cp, "ssh", lambda h, c, timeout=120, data=None: (0, "x", ""))

    def boom(a):
        raise TimeoutError("ssh")
    monkeypatch.setattr(cp, "SCENARIOS", {"a": boom, "b": cp.sc_corrupt, "c": lambda a: {"verdict": "??"}})
    rc = cp.main(["--board", "pi1", "--host", "h", "--out", str(tmp_path), "--only", "a,b,c"])
    s = json.loads((tmp_path / "summary.json").read_text())
    assert rc == 1 and s["results"] == {"a": "ERROR", "b": "SKIP", "c": "ERROR"}


def test_ssh_probe_records_failures(monkeypatch):
    """Каждый отказ нового соединения SSH во время сценария фиксируется (иначе сценарий не PASS)."""
    import time
    seq = iter([(0, "active", ""), (255, "", "Connection refused"), (0, "inactive", "")])
    monkeypatch.setattr(cp, "ssh", lambda h, c, timeout=120, data=None: next(seq, (0, "active", "")))
    with cp.SshProbe("h") as p:
        deadline = time.monotonic() + 10
        while p.ok + len(p.fail) < 3 and time.monotonic() < deadline:
            time.sleep(0.1)
    r = p.result()
    assert r["ssh_probes_ok"] >= 1 and [f["rc"] for f in r["ssh_probes_failed"]] == [255, 0]
