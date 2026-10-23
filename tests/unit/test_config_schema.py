# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""TEST-CFG-001: схема принимает ≥ 5 корректных наборов и отклоняет ≥ 20 некорректных с путём к полю."""
import pytest

from sheregesh.config import Schema, console_defaults

SCHEMA = Schema.load()


def with_file(name, text):
    f = console_defaults()
    f[name] = text
    return f


def replace(name, old, new):
    f = console_defaults()
    assert old in f[name]
    f[name] = f[name].replace(old, new)
    return f


VALID = [
    console_defaults(),
    replace("system.yaml", "language: ru", "language: en"),
    replace("system.yaml", "theme: dark", "theme: light"),
    replace("system.yaml", "ui_scale: 100", "ui_scale: 150"),
    replace("system.yaml", "screen_timeout_s: 300", "screen_timeout_s: 0"),
    replace("diagnostics.yaml", "temp_warning_c: 70", "temp_warning_c: 65.5"),
    replace("link.yaml", "192.168.77.2", "192.168.77.254"),
]

INVALID = [  # (набор, файл, путь, класс)
    (with_file("system.yaml", "language: [ru\n"), "system.yaml", "", "yaml"),
    (with_file("system.yaml", ""), "system.yaml", "", "type"),
    (with_file("system.yaml", "- a\n- b\n"), "system.yaml", "", "type"),
    (replace("system.yaml", "language: ru", "language: de"), "system.yaml", "language", "enum"),
    (replace("system.yaml", "theme: dark", "theme: blue"), "system.yaml", "theme", "enum"),
    (replace("system.yaml", "ui_scale: 100", "ui_scale: 110"), "system.yaml", "ui_scale", "enum"),
    (replace("system.yaml", "screen_timeout_s: 300", "screen_timeout_s: -1"), "system.yaml", "screen_timeout_s", "range"),
    (replace("system.yaml", "screen_timeout_s: 300", "screen_timeout_s: '300'"), "system.yaml", "screen_timeout_s", "type"),
    (replace("system.yaml", "device_name: Шерегеш-пульт", "device_name: ''"), "system.yaml", "device_name", "range"),
    (replace("system.yaml", "device_name: Шерегеш-пульт", "device_name: 'a;rm -rf'"), "system.yaml", "device_name", "pattern"),
    (replace("system.yaml", "language: ru\n", ""), "system.yaml", "language", "required"),
    (replace("system.yaml", "theme: dark", "theme: dark\nextra: 1"), "system.yaml", "extra", "extra"),
    (replace("diagnostics.yaml", "temp_warning_c: 70", "temp_warning_c: 95"), "diagnostics.yaml", "temp_warning_c", "range"),
    (replace("diagnostics.yaml", "temp_critical_c: 80", "temp_critical_c: hot"), "diagnostics.yaml", "temp_critical_c", "type"),
    (replace("diagnostics.yaml", "temp_warning_c: 70", "temp_warning_c: 80"), "diagnostics.yaml", "temp_warning_c", "semantic"),
    (replace("diagnostics.yaml", "battery_critical_pct: 10", "battery_critical_pct: 25"), "diagnostics.yaml", "battery_critical_pct", "semantic"),
    (replace("diagnostics.yaml", "battery_warning_pct: 20", "battery_warning_pct: 20.5"), "diagnostics.yaml", "battery_warning_pct", "type"),
    (replace("link.yaml", "192.168.77.2", "8.8.8.8"), "link.yaml", "peer_address", "pattern"),
    (replace("link.yaml", "192.168.77.2", "192.168.77.255"), "link.yaml", "peer_address", "pattern"),
    (replace("link.yaml", "port: 7700", "port: 80"), "link.yaml", "port", "range"),
    (replace("link.yaml", "heartbeat_timeout_s: 3", "heartbeat_timeout_s: 10"), "link.yaml", "heartbeat_timeout_s", "range"),
    ({k: v for k, v in console_defaults().items() if k != "link.yaml"}, "link.yaml", "", "missing-file"),
    ({**console_defaults(), "secrets.yaml": "token: x\n"}, "secrets.yaml", "", "unknown-file"),
    (with_file("system.yaml", "language: !!python/object/apply:os.system ['true']\n"), "system.yaml", "", "yaml"),
]


@pytest.mark.test_id("TEST-CFG-001")
@pytest.mark.parametrize("files", VALID)
def test_valid_sets_accepted(files):
    assert SCHEMA.validate(files) == []


@pytest.mark.test_id("TEST-CFG-001")
@pytest.mark.parametrize("files,file,path,kind", INVALID)
def test_invalid_sets_rejected_with_field_path(files, file, path, kind):
    issues = SCHEMA.validate(files)
    assert issues, "некорректный набор принят"
    assert any(i.file == file and i.path == path and i.kind == kind for i in issues), [str(i) for i in issues]


def test_sample_counts_meet_criterion():
    assert len(VALID) >= 5 and len(INVALID) >= 20


def test_services_for_changed_files():
    assert SCHEMA.services_for(["diagnostics.yaml", "system.yaml"]) == ["monitor", "ui"]
