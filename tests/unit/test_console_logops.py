# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Журнал приложения пульта (logops.py): ревизия, фильтры, перенос событий монитора, выгрузка без секретов."""
from types import SimpleNamespace

from sheregesh_console import logops


def ev(i, code, severity="info", **params):
    return SimpleNamespace(id=i, code=code, severity=severity, params=params)


def test_rows_filters_and_revision():
    log = logops.AppLog(limit=3)
    assert not log.take_dirty() and log.revision == 0
    for level in ("info", "warning", "error", "info"):
        log.add(level, "app", level)
    assert log.take_dirty() and not log.take_dirty()
    assert log.revision == 4 and len(log) == 3            # предел записей, ревизия — сквозная
    assert [r["id"] for r in log.rows()] == [4, 3, 2]
    assert [r["level"] for r in log.rows(1)] == ["error", "warning"]
    assert [r["level"] for r in log.rows(2)] == ["error"]
    assert len(log.rows(99)) == 3


def test_mirror_events_once():
    log = logops.AppLog()
    events = [ev(1, "A", "warning", x=1), ev(2, "B")]
    log.mirror_events(events)
    log.mirror_events(events + [ev(3, "C", "error")])
    assert [(r["level"], r["text"]) for r in reversed(log.rows())] == [("warning", "A x=1"), ("info", "B"),
                                                                       ("error", "C")]


def test_export_redacts_and_writes_checksum(tmp_path):
    log = logops.AppLog()
    log.add("info", "app", "token=abc SHRG-CANARY-TEST-x1 ok")
    name, data, count = log.export_payload()
    assert count == 1 and b"abc" not in data and b"SHRG-CANARY" not in data and b"[REDACTED]" in data
    logops.write_export(tmp_path / "usb", name, data)
    assert (tmp_path / "usb" / name).read_bytes() == data
    assert (tmp_path / "usb" / f"{name}.sha256").read_text().endswith(f"  {name}\n")
    assert not list((tmp_path / "usb").glob(".tmp-*"))
