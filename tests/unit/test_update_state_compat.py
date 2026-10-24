# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Совместимость state.json между версиями агента OTA (S3.2). Файл общий для слотов (/persistent): при откате на
прежний слот его читает агент старого образа. pi2, 2026-10-25: новый агент на B записал "expect": {}, а агент
57e8fe4 читает UpdateState(**json) — TypeError, агент на A не запустился бы."""
import json
import os
import subprocess
import sys
import tarfile
import io
from pathlib import Path

import pytest

from sheregesh.update import model

ROOT = Path(__file__).resolve().parents[2]
OLD = "57e8fe4"                              # образ, установленный на pi1/pi2


def _updater(tmp_path, **state):
    p = tmp_path / "state.json"
    u = model.Updater(backend=None, state_path=p, state=model.UpdateState(**state))
    u.save()
    return p


def test_empty_expect_is_not_written(tmp_path):
    p = _updater(tmp_path, state="TRYBOOT", target="B")
    assert "expect" not in json.loads(p.read_text())


def test_nonempty_expect_round_trips(tmp_path):
    p = _updater(tmp_path, state="INSTALLED", target="B", expect={"role": "remote"})
    assert json.loads(p.read_text())["expect"] == {"role": "remote"}
    assert model.Updater(backend=None, state_path=p).s.expect == {"role": "remote"}


def test_unknown_fields_from_other_agent_version_are_ignored(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"state": "IDLE", "active": "A", "future_field": 1}))
    assert model.Updater(backend=None, state_path=p).s.state == "IDLE"


def _old_model(tmp_path):
    try:
        raw = subprocess.run(["git", "-C", str(ROOT), "archive", OLD, "software/sheregesh/update"],
                             capture_output=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        (pytest.fail if os.environ.get("CI") else pytest.skip)(f"нет git-истории {OLD}")
    with tarfile.open(fileobj=io.BytesIO(raw)) as t:
        t.extractall(tmp_path / "old", filter="data")
    return tmp_path / "old/software"


def test_agent_57e8fe4_reads_state_written_by_new_agent(tmp_path):
    """Состояние pi2 после неудачного confirm на B (поля как в docs/acceptance/s3.2-ota/step5/confirm.json)."""
    old = _old_model(tmp_path)
    p = _updater(tmp_path, state="TRYBOOT", active="A", target="B", version_active="0.1.1792843040",
                 version_target="0.1.1792865618", boot_attempts=1, reason="health FAIL, повтор",
                 history=["INSTALLED->TRYBOOT"])
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); from sheregesh.update import model; "
            "s = model.UpdateState(**json.load(open(sys.argv[2]))); print(s.state)")
    r = subprocess.run([sys.executable, "-I", "-c", code, str(old), str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "TRYBOOT"
