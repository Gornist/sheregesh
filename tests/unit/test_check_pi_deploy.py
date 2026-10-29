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


SHOW = 'InvocationID=abc123\nMainPID=42\nExecMainStartTimestamp=@500'


def test_runtime_releases_use_fresh_launcher_logs_for_every_role_service(monkeypatch):
    calls = []
    def ssh(host, cmd, timeout=120, data=None):
        calls.append(cmd)
        if cmd.startswith('systemctl'):
            return 0, SHOW, ''
        module = 'sheregesh.videosvc.rx' if '_SYSTEMD_INVOCATION_ID' in cmd else 'sheregesh_console'
        return 0, f'sheregesh-launcher: {module}: выпуск 0.1.0+gnew', ''
    monkeypatch.setattr(cp, 'ssh', ssh)
    releases = cp.runtime_releases('host', {'sheregesh-video-rx': 'READY', 'sheregesh-console': 'READY'}, 123)
    assert set(releases) == {'sheregesh-video-rx', 'sheregesh-console'}
    assert all(r['release'] == '0.1.0+gnew' for r in releases.values())
    journals = [cmd for cmd in calls if cmd.startswith('journalctl')]
    # Video service: exact invocation, no -u (its OR terms would swallow the invocation match).
    assert any('--since @123' in c and '_SYSTEMD_INVOCATION_ID=abc123' in c and ' -u ' not in c for c in journals)
    # Console lives in session-N.scope (PAMName): bound by this invocation's start time, not by the service's ID.
    assert any('--since @500' in c and 'SYSLOG_IDENTIFIER=sheregesh-notify-relay' in c
               and '_SYSTEMD_INVOCATION_ID' not in c for c in journals)
    assert all(r['invocation']['MainPID'] == '42' for r in releases.values())


def test_runtime_releases_reject_malformed_start_timestamp(monkeypatch):
    calls = []
    def ssh(host, cmd, **kwargs):
        calls.append(cmd)
        return 0, 'InvocationID=abc123\nMainPID=42\nExecMainStartTimestamp=@1; reboot', ''
    monkeypatch.setattr(cp, 'ssh', ssh)
    result = cp.runtime_releases('host', {'sheregesh-console': 'READY'}, 123)
    assert result['sheregesh-console']['release'] == 'unknown'
    assert not any(cmd.startswith('journalctl') for cmd in calls)


def test_runtime_releases_missing_log_is_unknown_not_current_symlink(monkeypatch):
    monkeypatch.setattr(cp, 'ssh', lambda *a, **kw: (0, '', ''))
    releases = cp.runtime_releases('host', {'sheregesh-video-tx': 'READY'}, 123)
    assert releases['sheregesh-video-tx']['release'] == 'unknown'


def test_role_inventory_does_not_trust_old_installers_unit_list(monkeypatch):
    monkeypatch.setattr(cp, 'ssh', lambda *a, **kw: (0, 'LoadState=loaded\nConditionResult=yes\nActiveState=active', ''))
    units, skipped = cp.role_service_units('host', 'pi1')
    assert units == {'sheregesh-console': 'READY', 'sheregesh-video-rx': 'READY'}
    assert not skipped


def test_role_inventory_marks_missing_video_as_unverified_coverage(monkeypatch):
    monkeypatch.setattr(cp, 'ssh', lambda *a, **kw: (0, 'LoadState=not-found', ''))
    units, skipped = cp.role_service_units('host', 'pi2')
    assert units == {'sheregesh-remote-net': 'READY'}
    assert 'sheregesh-video-tx' in skipped


def test_runtime_releases_rejects_restarted_invocation(monkeypatch):
    shows = iter([SHOW, 'InvocationID=abc456\nMainPID=43\nExecMainStartTimestamp=@501'])
    def ssh(host, cmd, **kwargs):
        if cmd.startswith('systemctl'):
            return 0, next(shows), ''
        return 0, 'sheregesh-launcher: sheregesh.videosvc.tx: выпуск new', ''
    monkeypatch.setattr(cp, 'ssh', ssh)
    result = cp.runtime_releases('host', {'sheregesh-video-tx': 'READY'}, 123)
    assert result['sheregesh-video-tx']['release'] == 'unknown'
