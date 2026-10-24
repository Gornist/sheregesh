# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Бэкенд RAUC `bootloader=custom` поверх tryboot (прототип варианта C ADR-0009) на файловом autoboot.txt:
операции RAUC get/set-primary, get/set-state, get-current; однократность tryboot; инварианты «основной слот
меняется только на пробной загрузке этого слота», «основной нельзя пометить плохим»."""
import pytest
from rauc_helpers import agent_state, autoboot, boot_state, config, write_conf
from test_ota import Board

from sheregesh.update import rauc_backend, slots
from sheregesh.update.rauc_backend import BackendError, TrybootBackend


def backend(board):
    return TrybootBackend(config(board))


def install_like_rauc(board, slot="B"):
    """Последовательность вызовов бэкенда при `rauc install`: цель плохая до записи, после записи — set-primary."""
    b = backend(board)
    b.set_state(slot, "bad")
    b.set_primary(slot)
    return b


def test_initial_state_reads_autoboot_and_device_tree(tmp_path):
    board = Board(tmp_path)
    b = backend(board)
    assert (b.get_primary(), b.get_current(), b.get_state("A"), b.get_state("B")) == ("A", "A", "good", "bad")
    assert b.status()["tryboot"] is False


def test_set_primary_arms_one_time_trial_without_touching_all(tmp_path):
    board = Board(tmp_path)
    before = (board.bootfs / "autoboot.txt").read_text()
    b = install_like_rauc(board)
    assert autoboot(board) == (2, 3)                       # [all]=2 (A) по-прежнему, [tryboot]=3
    assert (board.bootfs / "autoboot.txt").read_text() == before == slots.render_autoboot(2, 3)
    assert b.get_primary() == "A" and b.get_state("B") == "bad"
    assert boot_state(board)["trial"] == {"slot": "B", "attempts": 0}


def test_mark_good_commits_only_on_trial_boot_of_that_slot(tmp_path):
    board = Board(tmp_path)
    b = install_like_rauc(board)
    with pytest.raises(BackendError, match="только слот текущей пробной загрузки"):
        b.set_state("B", "good")                           # B не загружался — подтверждать нечего
    assert autoboot(board) == (2, 3)
    board.firmware_boot(tryboot_flag=True)                 # reboot "0 tryboot"
    b = backend(board)
    assert b.booted() == ("B", True) and b.get_primary() == "A"
    b.set_state("B", "good")
    assert autoboot(board) == (3, 2) and b.get_primary() == "B" and b.get_state("B") == "good"
    assert b.get_state("A") == "good"                      # прежний основной — запасной, «good»
    assert boot_state(board)["trial"] is None
    board.firmware_boot(tryboot_flag=False)                # обычная перезагрузка → новый основной
    assert backend(board).booted() == ("B", False)


def test_power_loss_or_plain_reboot_on_trial_returns_committed_slot(tmp_path):
    board = Board(tmp_path)
    install_like_rauc(board)
    board.firmware_boot(tryboot_flag=True)
    board.firmware_boot(tryboot_flag=False)                # сброс без подтверждения: флаг tryboot потерян
    b = backend(board)
    assert b.booted() == ("A", False) and autoboot(board) == (2, 3)
    with pytest.raises(BackendError):
        b.set_state("B", "good")                           # с основного слота подтвердить пробный нельзя


def test_mark_bad_on_trial_cancels_trial_and_keeps_committed(tmp_path):
    board = Board(tmp_path)
    install_like_rauc(board)
    board.firmware_boot(tryboot_flag=True)
    b = backend(board)
    b.set_state("B", "bad")
    assert boot_state(board)["trial"] is None and b.get_state("B") == "bad" and autoboot(board) == (2, 3)
    board.firmware_boot(tryboot_flag=False)
    assert backend(board).booted() == ("A", False)


def test_committed_slot_cannot_be_marked_bad(tmp_path):
    board = Board(tmp_path)
    b = backend(board)
    with pytest.raises(BackendError, match="основной слот A нельзя"):
        b.set_state("A", "bad")
    assert autoboot(board) == (2, 3) and b.get_state("A") == "good"


def test_set_primary_refused_from_trial_boot(tmp_path):
    board = Board(tmp_path)
    install_like_rauc(board)
    board.firmware_boot(tryboot_flag=True)
    with pytest.raises(BackendError, match="только с основного слота"):
        backend(board).set_primary("B")
    backend(board).set_primary("A")                        # отмена пробы (= основной) разрешена всегда
    assert boot_state(board)["trial"] is None


def test_unknown_slot_and_state_rejected(tmp_path):
    board = Board(tmp_path)
    b = backend(board)
    for call in (lambda: b.get_state("C"), lambda: b.set_primary("rootfs.1"), lambda: b.set_state("B", "maybe")):
        with pytest.raises(BackendError):
            call()


def test_broken_autoboot_is_an_error_not_a_guess(tmp_path):
    board = Board(tmp_path)
    (board.bootfs / "autoboot.txt").write_text("[all]\nboot_partition=2\n")
    with pytest.raises(BackendError, match="tryboot_a_b"):
        backend(board).get_primary()
    (board.bootfs / "autoboot.txt").write_text(slots.render_autoboot(5, 3))
    with pytest.raises(BackendError, match="не слот A/B"):
        backend(board).get_primary()


def test_guard_blocks_slot_changes_while_agent_update_unfinished(tmp_path):
    board = Board(tmp_path)
    agent_state(board, "INSTALLED")
    b = backend(board)
    b.set_state("B", "bad")                                # безопасно — разрешено
    with pytest.raises(BackendError, match="sheregesh-ota"):
        b.set_primary("B")
    assert boot_state(board)["trial"] is None and autoboot(board) == (2, 3)


def test_cli_matches_rauc_custom_backend_protocol(tmp_path, monkeypatch, capsys):
    board = Board(tmp_path)
    monkeypatch.setenv(rauc_backend.CONF_ENV, str(write_conf(board, tmp_path / "rauc.json")))
    assert rauc_backend.main(["get-primary"]) == 0 and capsys.readouterr().out == "A\n"
    assert rauc_backend.main(["get-current"]) == 0 and capsys.readouterr().out == "A\n"
    assert rauc_backend.main(["get-state", "B"]) == 0 and capsys.readouterr().out == "bad\n"
    assert rauc_backend.main(["set-state", "B", "bad"]) == 0
    assert rauc_backend.main(["set-primary", "B"]) == 0
    assert rauc_backend.main(["set-state", "B", "good"]) == 1          # не пробная загрузка — ненулевой код
    assert rauc_backend.main(["get-state", "X"]) == 1
    assert rauc_backend.main(["frobnicate"]) == 2
    capsys.readouterr()
    assert autoboot(board) == (2, 3)


def test_config_load_from_json(tmp_path, monkeypatch):
    board = Board(tmp_path)
    c = rauc_backend.Config.load(write_conf(board, tmp_path / "rauc.json"))
    assert c.paths.bootfs == str(board.bootfs) and c.state_dir == board.persistent / "sheregesh-rauc"
    monkeypatch.delenv(rauc_backend.CONF_ENV, raising=False)
    assert rauc_backend.Config.load().state_dir.as_posix() == "/persistent/sheregesh-rauc"
