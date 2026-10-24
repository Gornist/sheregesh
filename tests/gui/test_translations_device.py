# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Переводы на устройстве (S3.1-DEPLOY, найдено на pi1 57e8fe4): установщик пишет файлы выпуска по алфавиту, .ts
оказывается «новее» .qm; без lrelease на плате пульт падал с RuntimeError и выпуск откатывался."""
import os
import shutil

import pytest

import sheregesh_console.app as app


def test_stale_qm_without_lrelease_is_used(tmp_path, monkeypatch, capsys):
    for f in app.I18N.glob("sheregesh_*"):
        shutil.copy(f, tmp_path)
    ts, qm = tmp_path / "sheregesh_ru.ts", tmp_path / "sheregesh_ru.qm"
    os.utime(qm, (1, 1))
    os.utime(ts, (2, 2))                                   # .ts «новее» только по времени записи
    monkeypatch.setattr(app, "I18N", tmp_path)
    monkeypatch.setattr(app, "qt_tool", lambda name: [])   # как на плате: lrelease нет
    assert app.compile_translations(tmp_path) == [qm]
    assert "готовый .qm" in capsys.readouterr().err


def test_missing_qm_without_lrelease_still_fails(tmp_path, monkeypatch):
    shutil.copy(app.I18N / "sheregesh_ru.ts", tmp_path)
    monkeypatch.setattr(app, "I18N", tmp_path)
    monkeypatch.setattr(app, "qt_tool", lambda name: [])
    with pytest.raises(RuntimeError, match="lrelease"):
        app.compile_translations(tmp_path)
