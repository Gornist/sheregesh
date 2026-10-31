# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Паспорт устройства sheregesh.info (TEST-SYS-007): плата, образ, выпуск — из локальных файлов, без сети."""
import json
import re

import pytest

from sheregesh import PROTOCOL_VERSION, __semver__, __version__, info, semver, semver_or_raw

SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?")  # semver.org


def board_dir(tmp_path, model="Raspberry Pi 4 Model B Rev 1.5", serial="10000000568152de"):
    dt = tmp_path / "dt"
    dt.mkdir()
    (dt / "model").write_bytes(model.encode() + b"\0")
    (dt / "serial-number").write_bytes(serial.encode() + b"\0")
    return dt


def test_on_board_reports_revision_model_serial_image_and_release(tmp_path):
    dt = board_dir(tmp_path)
    build = tmp_path / "build.json"
    build.write_text(json.dumps({"commit": "c0c509c0b7162bdb7a60fc701fb374d9c13d8c8b", "ota_version": "0.1.1793394197"}))
    rel = tmp_path / "releases" / "0.1.0~dev0+g3b4ffc747491"
    rel.mkdir(parents=True)
    (tmp_path / "current").symlink_to(rel)
    d = info.collect("remote", dt=dt, build_json=build, deploy_current=tmp_path / "current")
    assert d == {"role": "remote", "software": __semver__, "package": __version__, "protocol": PROTOCOL_VERSION,
                 "image": {"commit": "c0c509c0b716", "ota_version": "0.1.1793394197"},
                 "release": "0.1.0~dev0+g3b4ffc747491", "hardware": "HW-1.0 Rev A (Raspberry Pi 4 Model B Rev 1.5)",
                 "serial": "10000000568152de", "device_id": "remote-10000000568152de"}
    assert d["hardware"].startswith("HW-1.0")             # префикс матрицы совместимости


def test_off_board_keeps_emulator_identity(tmp_path):
    d = info.collect("console", dt=tmp_path / "none", build_json=tmp_path / "none.json",
                     deploy_current=tmp_path / "none")
    assert d["hardware"] == "HW-1.0 Rev A (emulator)" and d["device_id"] == "console-emu-01"
    assert d["serial"] == "" and d["image"] == {"commit": "", "ota_version": ""} and d["release"] == ""


def test_cli_text_and_json(capsys):
    assert info.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["protocol"] == PROTOCOL_VERSION
    assert info.main([]) == 0
    assert "Протокол" in capsys.readouterr().out


@pytest.mark.parametrize("pep,want", [
    ("0.1.0.dev0", "0.1.0-dev.0"), ("0.1.0", "0.1.0"), ("1.0.0rc1", "1.0.0-rc.1"), ("1.2.3a2.dev4", "1.2.3-alpha.2.dev.4"),
    ("1.2.3.post1", "1.2.3-post.1"), ("0.1.0.dev0+gabc1234", "0.1.0-dev.0+gabc1234"),
    ("0.1.0.dev0+g43ea9a9.dirty", "0.1.0-dev.0+g43ea9a9.dirty")])
def test_semver_from_pep440(pep, want):
    """REQ-REL-010: версия снаружи — SemVer 2.0; одна функция для GUI, sheregesh-info, тега и архива."""
    assert semver(pep) == want and SEMVER.fullmatch(want)


@pytest.mark.parametrize("bad", ["", "1.0", "1.0.0.0", "v1.0.0", "1.0.0-beta"])
def test_semver_rejects_foreign_strings(bad):
    with pytest.raises(ValueError):
        semver(bad)
    assert semver_or_raw(bad) == bad
