# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Probe критериев подтверждения (S3.2): pi2, 2026-10-24 — агент OTA (root) вызывал sheregesh-health без
--link-config, «~» раскрывался в /root, и здоровая плата получала health FAIL; слот B не подтверждался."""
import subprocess
from pathlib import Path

from sheregesh import health
from sheregesh.update import confirm


def _probe(calls, out='{"verdict": "PASS", "checks": []}'):
    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    return confirm.Probe(Path("/"), run)


def test_role_health_uses_stand_user_link_config_not_root_home():
    calls = []
    assert _probe(calls).health("remote")["verdict"] == "PASS"
    cmd = calls[-1]
    assert cmd[:2] == ["/usr/bin/sheregesh-health", "--json"]
    assert cmd[cmd.index("--role") + 1] == "remote"
    assert cmd[cmd.index("--link-config") + 1] == "/home/sheregesh-ui/.config/sheregesh/link.json"
    assert not any("~" in c for c in cmd)


def test_standalone_health_has_no_network_profile():
    calls = []
    _probe(calls).health("standalone")
    assert "--link-config" not in calls[-1] and "--role" not in calls[-1]


def test_link_config_path_matches_role_and_fingerprint_location():
    """Профиль, по которому судит health, — тот же файл, отпечаток которого снимает expect_from_board."""
    calls = []
    _probe(calls).health("console")
    cmd = calls[-1]
    assert Path(cmd[cmd.index("--link-config") + 1]) == confirm.CONFIG / "link.json"


def test_health_cli_accepts_the_arguments():
    ap_flags = Path(health.__file__).read_text()
    assert '"--role"' in ap_flags and '"--link-config"' in ap_flags


def test_broken_health_output_is_error_not_pass():
    calls = []
    assert _probe(calls, out="not json").health("remote")["verdict"] == "ERROR"
