# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Хранилище ревизий: атомарность, подтверждение, откат, восстановление, снимки, слоты, резервные копии.

Тесты с прерыванием записи перебирают КАЖДУЮ изменяющую операцию ФС (L2 на tmpfs/ext4 хоста).
Отключение питания на эталоне — TEST-CFG-002/005 уровня L4, здесь не выполняется.
"""
import shutil
import tarfile
from pathlib import Path

import pytest

from sheregesh.config import (BackupError, ConfigError, ConfigStore, ConfirmationRequired, FsOps,
                              NoValidConfiguration, Schema, console_defaults)

SCHEMA = Schema.load()
OK = lambda rev, services: True  # noqa: E731
FAIL = lambda rev, services: False  # noqa: E731


class PowerLoss(BaseException):
    """BaseException: «питание пропало» — код не должен её перехватывать как обычную ошибку."""


class CrashFs(FsOps):
    """Считает изменяющие операции; на операции № crash_at «пропадает питание».

    torn=True: при write_file на этой операции на диск успевает попасть половина данных.
    """

    def __init__(self, crash_at=None, torn=False):
        self.n, self.crash_at, self.torn = 0, crash_at, torn

    def _tick(self):
        self.n += 1
        return self.n == self.crash_at

    def write_file(self, path, data):
        if self._tick():
            if self.torn:
                Path(path).write_bytes(data[: len(data) // 2])
            raise PowerLoss()
        super().write_file(path, data)

    def fsync_dir(self, path):
        if self._tick():
            raise PowerLoss()
        super().fsync_dir(path)

    def rename(self, src, dst):
        if self._tick():
            raise PowerLoss()
        super().rename(src, dst)

    def mkdir(self, path):
        if self._tick():
            raise PowerLoss()
        super().mkdir(path)

    def rmtree(self, path):
        if self._tick():
            raise PowerLoss()
        super().rmtree(path)

    def unlink(self, path):
        if self._tick():
            raise PowerLoss()
        super().unlink(path)


def changed(**overrides):
    f = console_defaults()
    for key, value in overrides.items():
        name, field = key.split("__")
        lines = [ln for ln in f[f"{name}.yaml"].splitlines() if not ln.startswith(f"{field}:")]
        f[f"{name}.yaml"] = "\n".join(lines + [f"{field}: {value}"]) + "\n"
    return f


@pytest.fixture
def store(tmp_path):
    s = ConfigStore(tmp_path / "config", SCHEMA)
    s.initialize(console_defaults())
    s.recover()
    return s


def reopen(store, **kw):
    s = ConfigStore(store.root, SCHEMA, **kw)
    report = s.recover()
    return s, report


# ---------------- применение, подтверждение, откат ----------------
@pytest.mark.test_id("TEST-CFG-004")
def test_apply_confirms_after_health_check(store):
    calls = []
    res = store.apply(changed(system__language="en"), lambda r, s: calls.append(s) or True)
    assert res.ok and res.stage == "confirmed"
    assert calls == [["ui"]]
    st = store.state()
    assert st["confirmed"] == st["current"] == res.revision and st["pending"] is None
    assert store.active().data["system.yaml"]["language"] == "en"


@pytest.mark.test_id("TEST-CFG-004")
@pytest.mark.parametrize("check", [FAIL, lambda r, s: 1 / 0], ids=["refused", "exception"])
def test_failed_health_check_rolls_back_and_quarantines(store, check):
    before = store.active()
    res = store.apply(changed(system__language="en"), check)
    assert not res.ok and res.stage == "health-check"
    assert store.active().id == before.id and store.active().files == before.files
    assert (store.quarantine / res.revision).is_dir()
    assert not (store.revisions / res.revision).exists()


@pytest.mark.test_id("TEST-CFG-004")
def test_health_check_timeout_rolls_back(store):
    import threading
    release = threading.Event()
    res = store.apply(changed(system__theme="light"), lambda r, s: release.wait(5), timeout_s=0.2)
    release.set()
    assert not res.ok and "0.2" in res.message
    assert store.active().data["system.yaml"]["theme"] == "dark"


@pytest.mark.test_id("TEST-CFG-002")
def test_invalid_configuration_never_becomes_active(store):
    before = store.state()
    res = store.apply(changed(diagnostics__temp_warning_c=95), OK)
    assert not res.ok and res.stage == "validation" and res.issues
    assert store.state() == before
    assert len(store.history()) == 1


@pytest.mark.test_id("TEST-CFG-003")
def test_multi_file_change_is_one_revision(store):
    res = store.apply(changed(system__language="en", diagnostics__temp_warning_c=60, link__port=7701), OK)
    assert res.ok
    data = store.active().data
    assert (data["system.yaml"]["language"], data["diagnostics.yaml"]["temp_warning_c"],
            data["link.yaml"]["port"]) == ("en", 60, 7701)
    assert len(store.history()) == 2


def test_manual_rollback_returns_previous_snapshot(store):
    store.apply(changed(system__language="en"), OK)
    res = store.rollback(OK)
    assert res.ok
    assert store.active().data["system.yaml"]["language"] == "ru"


def test_apply_refused_while_pending(store):
    st = store.state()
    st["pending"] = "r000099"
    store._write_state(st)
    with pytest.raises(ConfigError):
        store.apply(changed(system__language="en"), OK)


# ---------------- прерывание записи ----------------
def _count_ops(store_dir, action):
    """Сколько изменяющих операций выполняет действие без сбоя."""
    tmp = store_dir.parent / "count"
    shutil.copytree(store_dir, tmp)
    fs = CrashFs()
    action(ConfigStore(tmp, SCHEMA, fs=fs))
    shutil.rmtree(tmp)
    return fs.n


def _crash_matrix(store, action, new_value_check, torn=False):
    """Для каждой операции k: сбой на k → новый старт → проверка инвариантов."""
    total = _count_ops(store.root, action)
    assert total > 10
    old = store.active()
    outcomes = {"old": 0, "new": 0}
    for k in range(1, total + 1):
        d = store.root.parent / f"crash-{k}"
        shutil.copytree(store.root, d)
        with pytest.raises(PowerLoss):
            action(ConfigStore(d, SCHEMA, fs=CrashFs(crash_at=k, torn=torn)))
        s = ConfigStore(d, SCHEMA)
        s.recover()
        st, rev = s.state(), s.active()
        assert st["pending"] is None, f"k={k}: pending остался"
        assert st["current"] == st["confirmed"], f"k={k}"
        if rev.files == old.files:
            outcomes["old"] += 1
        else:
            assert new_value_check(rev), f"k={k}: активен смешанный набор {rev.files}"
            assert (s.markers / rev.id).exists(), f"k={k}: активна неподтверждённая ревизия"
            outcomes["new"] += 1
        s.apply(changed(system__screen_timeout_s=42), OK)  # после восстановления хранилище работоспособно
        shutil.rmtree(d)
    return total, outcomes


@pytest.mark.test_id("TEST-CFG-002")
@pytest.mark.test_id("TEST-CFG-005")
@pytest.mark.parametrize("torn", [False, True], ids=["clean-stop", "torn-write"])
def test_power_loss_at_every_step_of_apply(store, torn):
    files = changed(system__language="en", diagnostics__temp_warning_c=60)
    total, outcomes = _crash_matrix(
        store, lambda s: s.apply(files, OK),
        lambda rev: rev.files == files, torn=torn)
    assert outcomes["old"] > 0 and outcomes["new"] > 0, outcomes
    assert sum(outcomes.values()) == total


@pytest.mark.test_id("TEST-CFG-005")
def test_power_loss_during_health_check_does_not_activate_pending(store):
    """Питание пропало во время проверки служб: после старта активна прежняя подтверждённая ревизия."""
    before = store.active()

    def lose_power(rev, services):
        raise PowerLoss()

    with pytest.raises(PowerLoss):
        # PowerLoss — BaseException, поэтому проходит сквозь обработку ошибок проверки, как реальный сбой.
        store.apply(changed(system__language="en"), lose_power)
    pending = store.state()["pending"]
    assert pending
    s, report = reopen(store)
    assert s.active().files == before.files
    assert (s.quarantine / pending).is_dir()
    assert any(code == "CFG-PENDING-DISCARDED" for code, _ in report.actions)


@pytest.mark.test_id("TEST-CFG-002")
def test_power_loss_at_every_step_of_restore(store, tmp_path):
    store.apply(changed(system__language="en"), OK)
    backup = store.backup(tmp_path)
    store.apply(changed(system__language="ru", system__theme="light"), OK)
    target = store.inspect_backup(backup)[1]
    _crash_matrix(store, lambda s: s.restore(backup, OK, confirmed=True), lambda rev: rev.files == target)


# ---------------- повреждённые и неполные файлы ----------------
def test_corrupted_state_uses_previous_copy(store):
    store.apply(changed(system__language="en"), OK)
    store.state_path.write_text('{"format": 1, "curr')
    s, report = reopen(store)
    assert any(c == "CFG-STATE-PREV" for c, _ in report.actions)
    assert s.state()["pending"] is None


def test_lost_state_rebuilt_from_confirmation_markers(store):
    res = store.apply(changed(system__language="en"), OK)
    store.state_path.write_bytes(b"\x00" * 10)
    store.prev_path.unlink()
    s, report = reopen(store)
    assert any(c == "CFG-STATE-REBUILT" for c, _ in report.actions)
    assert s.active().id == res.revision


def test_corrupted_confirmed_revision_falls_back_to_snapshot(store):
    first = store.active()
    res = store.apply(changed(system__language="en"), OK)
    (store.revisions / res.revision / "system.yaml").write_text("language: en\ntheme: dark\n")  # искажён
    s, report = reopen(store)
    assert any(c == "CFG-CONFIRMED-CORRUPT" for c, _ in report.actions)
    assert s.active().id == first.id


@pytest.mark.parametrize("damage", ["truncate-manifest", "delete-file", "extra-file"])
def test_revision_integrity_detected(store, damage):
    first = store.active()
    res = store.apply(changed(link__port=7702), OK)
    d = store.revisions / res.revision
    if damage == "truncate-manifest":
        (d / "MANIFEST.json").write_text("{")
    elif damage == "delete-file":
        (d / "link.yaml").unlink()
    else:
        (d / "notes.yaml").write_text("x: 1\n")
    s, _ = reopen(store)
    assert s.active().id == first.id


def test_no_valid_configuration_raises_for_safe_mode(store):
    for p in store.revisions.iterdir():
        for f in p.iterdir():
            f.write_text("damaged")
    with pytest.raises(NoValidConfiguration):
        reopen(store)


def test_leftover_temporary_revision_removed(store):
    tmp = store.revisions / ".tmp-r000007"
    tmp.mkdir()
    (tmp / "system.yaml").write_text("half")
    _, report = reopen(store)
    assert not tmp.exists()
    assert any(c == "CFG-TMP-REMOVED" for c, _ in report.actions)


# ---------------- снимки и слоты ----------------
@pytest.mark.test_id("TEST-CFG-009")
def test_three_latest_snapshots_kept_and_valid(store):
    for i in range(4):
        assert store.apply(changed(system__screen_timeout_s=10 + i), OK).ok
    snaps = store.state()["snapshots"]
    assert len(snaps) == 3
    assert all(store._verify(s) for s in snaps)
    assert snaps == [h["id"] for h in reversed(store.history()[1:4])]


@pytest.mark.test_id("TEST-CFG-006")
def test_slot_rollback_activates_bound_revision(tmp_path):
    root = tmp_path / "config"
    a = ConfigStore(root, SCHEMA, slot="A")
    a.initialize(console_defaults())
    a.recover()
    rev_n = a.apply(changed(system__language="en"), OK).revision
    b, _ = reopen(a, slot="B")                      # «обновление ОС» на слот B
    assert b.state()["slots"]["B"] == rev_n
    rev_n1 = b.apply(changed(system__language="en", system__theme="light"), OK).revision
    a2, report = reopen(a, slot="A")                # «откат ОС» на слот A
    assert a2.active().id == rev_n
    assert (a2.revisions / rev_n1).is_dir(), "более новая ревизия должна сохраниться"
    assert any(c == "CFG-SLOT-BINDING" for c, _ in report.actions)


# ---------------- резервные копии ----------------
@pytest.mark.test_id("TEST-CFG-008")
def test_backup_roundtrip_requires_confirmation(store, tmp_path):
    store.apply(changed(system__language="en"), OK)
    path = store.backup(tmp_path)
    saved = store.active().files
    store.apply(changed(system__theme="light"), OK)
    with pytest.raises(ConfirmationRequired):
        store.restore(path, OK)
    assert store.active().data["system.yaml"]["theme"] == "light"
    assert store.restore(path, OK, confirmed=True).ok
    assert store.active().files == saved


@pytest.mark.test_id("TEST-CFG-008")
@pytest.mark.parametrize("damage", ["bitflip", "truncated", "wrong-device", "newer-schema", "extra-member"])
def test_backup_damage_or_incompatibility_rejected(store, tmp_path, damage):
    import io
    import json
    path = store.backup(tmp_path)
    data = path.read_bytes()
    if damage == "bitflip":
        i = data.index(b"language: ru") + 10
        data = data[:i] + b"x" + data[i + 1:]
        path.write_bytes(data)
    elif damage == "truncated":
        path.write_bytes(data[: len(data) // 3])
    else:
        with tarfile.open(path) as tar:
            members = [(m, tar.extractfile(m).read()) for m in tar.getmembers()]
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w") as tar:
            for m, raw in members:
                if m.name == "BACKUP.json":
                    info = json.loads(raw)
                    if damage == "wrong-device":
                        info["device"] = "remote"
                    if damage == "newer-schema":
                        info["schema_version"] = 2
                    raw = json.dumps(info).encode()
                    m.size = len(raw)
                tar.addfile(m, io.BytesIO(raw))
            if damage == "extra-member":
                extra = tarfile.TarInfo("config/../../etc/passwd")
                extra.size = 1
                tar.addfile(extra, io.BytesIO(b"x"))
        path.write_bytes(out.getvalue())
    before = store.state()
    with pytest.raises(BackupError):
        store.restore(path, OK, confirmed=True)
    assert store.state() == before


@pytest.mark.test_id("TEST-CFG-007")
def test_backup_contains_no_secrets(store, tmp_path):
    secrets = store.root.parent / "secrets"
    secrets.mkdir()
    canary = "SHRG-CANARY-BACKUP-7f3a9c1e5d"
    (secrets / "dev-key").write_text(canary)
    path = store.backup(tmp_path)
    assert canary.encode() not in path.read_bytes()
    with tarfile.open(path) as tar:
        names = tar.getnames()
    assert names == ["BACKUP.json"] + [f"config/{n}" for n in sorted(console_defaults())]
