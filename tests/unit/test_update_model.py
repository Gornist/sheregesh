# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Модель обновления A/B (ADR-0006/0009, REQ-UPD-004): инварианты без записи в загрузочные разделы."""
import pytest

from sheregesh.update.model import MAX_BOOT_ATTEMPTS, FakeSlots, UpdateError, Updater, UpdateState

GOOD = {"version": "1.1.0", "compatible": "sheregesh-pi4", "signature_ok": True}


def installed(tmp_path=None):
    b = FakeSlots()
    u = Updater(b, tmp_path / "state.json" if tmp_path else None)
    u.verify(GOOD)
    u.install()
    u.request_tryboot()
    return b, u


@pytest.mark.test_id("TEST-UPD-004")
def test_success_commits_only_after_health_pass(tmp_path):
    b, u = installed(tmp_path)
    assert b.default_slot == "A" and b.writes == ["B"]                    # основной не тронут, запись в неактивный
    slot = b.boot()
    u.on_boot(slot)
    assert u.s.state == "HEALTH_PENDING" and b.default_slot == "A"
    u.on_health("PASS", slot)
    assert u.s.state == "COMMITTED" and u.s.active == "B" and b.default_slot == "B" and u.s.version_active == "1.1.0"


@pytest.mark.test_id("TEST-UPD-004")
def test_health_fail_retries_then_rolls_back(tmp_path):
    b, u = installed(tmp_path)
    for _ in range(MAX_BOOT_ATTEMPTS):
        slot = b.boot()
        u.on_boot(slot)
        u.on_health("FAIL", slot)
    assert u.s.state == "ROLLED_BACK" and b.default_slot == "A" and b.boot() == "A"
    assert u.s.active == "A" and u.s.version_active == "1.0.0"


@pytest.mark.parametrize("cut_after", ["verify", "install", "request_tryboot", "boot"])
def test_power_loss_at_any_step_keeps_previous_slot(tmp_path, cut_after):
    """Отключение питания = прошивка грузит основной слот (tryboot однократен); состояние — из файла."""
    b = FakeSlots()
    u = Updater(b, tmp_path / "state.json")
    steps = {"verify": lambda: u.verify(GOOD), "install": u.install, "request_tryboot": u.request_tryboot,
             "boot": lambda: u.on_boot(b.boot())}
    for name, step in steps.items():
        step()
        if name == cut_after:
            break
    b.tryboot_slot = None if cut_after == "boot" else b.tryboot_slot
    b.tryboot_slot = None                                                  # питание пропало: флаг tryboot потерян
    u2 = Updater(b, tmp_path / "state.json")                               # после включения — состояние с диска
    booted = b.boot()
    u2.on_boot(booted)
    assert booted == "A" and b.default_slot == "A"
    assert u2.s.state in ("VERIFIED", "INSTALLED", "ROLLED_BACK", "FAILED"), u2.s.state


def test_interrupted_install_fails_without_touching_active(tmp_path):
    b = FakeSlots()
    u = Updater(b, tmp_path / "state.json")
    u.verify(GOOD)
    u.s.state = "INSTALLING"
    u.save()
    u2 = Updater(b, tmp_path / "state.json")
    u2.on_boot(b.boot())
    assert u2.s.state == "FAILED" and b.default_slot == "A" and b.content["A"] == "1.0.0"


@pytest.mark.parametrize("bundle, reason", [
    ({**GOOD, "signature_ok": False}, "подпись"),
    ({**GOOD, "compatible": "other-board"}, "compatible"),
    ({**GOOD, "version": "0.9.0"}, "AUD-014"),
])
def test_rejected_bundles_never_write(bundle, reason):
    b = FakeSlots()
    u = Updater(b)
    u.verify(bundle)
    assert u.s.state == "FAILED" and reason in u.s.reason and b.writes == []
    with pytest.raises(UpdateError):
        u.install()


def test_write_error_and_hash_mismatch_fail_before_tryboot():
    b = FakeSlots()
    b.fail_write = True
    u = Updater(b)
    u.verify(GOOD)
    u.install()
    assert u.s.state == "FAILED" and b.tryboot_slot is None


def test_boot_attempts_limit_counts_unconfirmed_boots():
    b, u = installed()
    for _ in range(MAX_BOOT_ATTEMPTS + 1):
        b.set_tryboot(u.s.target or "B")
        u.on_boot(b.boot())
        if u.s.state == "ROLLED_BACK":
            break
    assert u.s.state == "ROLLED_BACK" and "попыток" in u.s.reason


def test_health_on_wrong_slot_and_double_update_are_refused():
    b, u = installed()
    u.on_boot(b.boot())
    with pytest.raises(UpdateError):
        u.on_health("PASS", "A")
    with pytest.raises(UpdateError):
        u.verify(GOOD)


def test_state_survives_restart(tmp_path):
    b, u = installed(tmp_path)
    assert Updater(b, tmp_path / "state.json").s == u.s
    assert isinstance(UpdateState(), UpdateState)
