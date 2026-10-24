# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Хуки RAUC и адаптер health (прототип варианта C ADR-0009) на тестовой плате: pre-install (цель RAUC =
неактивный слот image-rota), post-install (VarSeeder, отпечатки), tryboot (попытки UPD-004), on-boot,
health по confirm.gates → mark-good / mark-bad + откат."""
import json

import pytest
from rauc_helpers import (FakeRauc, agent_state, autoboot, boot_state, config, env_for, hooks, write_slot)
from test_ota import Board

from sheregesh.update import rauc_backend, rauc_hooks
from sheregesh.update.rauc_hooks import HookError


def rauc_install(board, version="1.1.0"):
    """Полная установка, как её проведёт RAUC: pre-install → set-state bad → запись → set-primary → post-install."""
    h = hooks(board, env=env_for(board, version=version))
    h.pre_install()
    b = rauc_backend.TrybootBackend(config(board))
    b.set_state("B", "bad")
    write_slot(board, version=version)
    b.set_primary("B")
    h.post_install()
    return h


def test_pre_install_accepts_inactive_target_and_claims(tmp_path):
    board = Board(tmp_path)
    assert "цель B" in hooks(board).pre_install()
    claim = json.loads((board.persistent / "update-manager.json").read_text())
    assert claim["manager"] == "rauc" and claim["action"] == "install"


@pytest.mark.parametrize("breakage", ["active_target", "wrong_device", "no_target"])
def test_pre_install_refuses_target_disagreeing_with_image_rota(tmp_path, breakage):
    board = Board(tmp_path)
    env = env_for(board)
    if breakage == "active_target":
        env = env_for(board, target="a")
    elif breakage == "wrong_device":
        env["RAUC_SLOT_DEVICE_1"] = str(board.dev / "mmcblk0p4")
    else:
        env["RAUC_TARGET_SLOTS"] = ""
    with pytest.raises(HookError):
        hooks(board, env=env).pre_install()


def test_pre_install_refused_on_trial_boot(tmp_path):
    board = Board(tmp_path)
    rauc_install(board)
    hooks(board).tryboot()
    board.firmware_boot(tryboot_flag=True)
    with pytest.raises(HookError, match="только с основного слота"):
        hooks(board, env=env_for(board, target="a")).pre_install()


def test_post_install_seeds_var_snapshots_expectations_releases_claim(tmp_path):
    board = Board(tmp_path)
    (board.var / "lib/sheregesh").mkdir(parents=True)
    (board.var / "lib/sheregesh/config.json").write_text('{"rev": 7}')
    rauc_install(board)
    rec = json.loads((board.persistent / "sheregesh-rauc/install.json").read_text())
    assert rec["target"] == "B" and rec["version"] == "1.1.0" and rec["rauc_slot"] == "rootfs.1"
    assert rec["expect"]["machine_id"] == "0123456789abcdef0123456789abcdef"
    var_b = board.persistent / "slots/system_b/var"
    assert (var_b / "lib/dpkg/status").read_text() == "image 1.1.0\n"          # /var нового образа
    assert (var_b / "lib/sheregesh/config.json").read_text() == '{"rev": 7}'   # перенесена конфигурация
    assert (board.persistent / "slots/system_a/var/lib/dpkg/status").read_text() == "old image 1.0.0\n"
    assert not (board.persistent / "update-manager.json").exists()


def test_tryboot_counts_attempts_carries_fresh_config_and_reboots_once(tmp_path):
    board = Board(tmp_path)
    (board.var / "lib/sheregesh").mkdir(parents=True)
    (board.var / "lib/sheregesh/config.json").write_text('{"rev": 7}')
    rauc_install(board)
    (board.var / "lib/sheregesh/config.json").write_text('{"rev": 8}')       # изменение после установки
    rauc = FakeRauc(board)
    assert "попытка 1/2" in hooks(board, rauc=rauc).tryboot()
    assert rauc.commands == ["reboot '0 tryboot'"] and autoboot(board) == (2, 3)
    assert (board.persistent / "slots/system_b/var/lib/sheregesh/config.json").read_text() == '{"rev": 8}'
    board.firmware_boot(tryboot_flag=True)
    board.firmware_boot(tryboot_flag=False)                                 # зависла → питание → A
    assert "попытка 2/2" in hooks(board).tryboot()
    board.firmware_boot(tryboot_flag=True)
    board.firmware_boot(tryboot_flag=False)
    with pytest.raises(HookError, match="исчерпаны"):
        hooks(board).tryboot()
    rauc = FakeRauc(board)
    assert "mark-bad other" in hooks(board, rauc=rauc).on_boot()            # решение записывает RAUC
    assert rauc.commands == ["rauc status mark-bad other"]
    assert boot_state(board)["trial"] is None and boot_state(board)["slots"]["B"] == "bad"
    with pytest.raises(HookError, match="не назначена"):
        hooks(board).tryboot()


def test_health_pass_marks_good_and_commits(tmp_path):
    board = Board(tmp_path)
    rauc_install(board)
    hooks(board).tryboot()
    board.firmware_boot(tryboot_flag=True)
    rauc = FakeRauc(board)
    res = hooks(board, rauc=rauc).health()
    assert res["result"] == "COMMITTED", res["failed"]
    assert rauc.commands == ["rauc status mark-good booted"] and autoboot(board) == (3, 2)
    ids = {c["id"] for c in res["checks"]}
    assert "unit.update-manager(rauc.service)" in ids and "health" in ids and "same.machine_id" in ids
    board.firmware_boot(tryboot_flag=False)
    assert rauc_backend.TrybootBackend(config(board)).booted() == ("B", False)


@pytest.mark.parametrize("fault", ["health", "unit_restarts", "version"])
def test_health_fail_marks_bad_and_rolls_back(tmp_path, fault):
    board = Board(tmp_path)
    rauc_install(board, version="1.1.0")
    hooks(board).tryboot()
    board.firmware_boot(tryboot_flag=True)
    if fault == "health":
        board.health = "FAIL"
    elif fault == "unit_restarts":
        board.units["sheregesh-console.service"] = {"NRestarts": "3"}
    else:
        (board.persistent / "sheregesh-rauc/install.json").write_text(json.dumps(
            {**json.loads((board.persistent / "sheregesh-rauc/install.json").read_text()), "version": "9.9.9"}))
    rauc = FakeRauc(board)
    res = hooks(board, rauc=rauc).health()
    assert res["result"] == "ROLLED_BACK" and res["failed"]
    assert rauc.commands == ["rauc status mark-bad booted", "reboot"]
    assert autoboot(board) == (2, 3) and boot_state(board)["trial"] is None
    board.firmware_boot(tryboot_flag=False)                     # обычная перезагрузка → прежний основной A
    assert rauc_backend.TrybootBackend(config(board)).booted() == ("A", False)


def test_health_without_install_record_never_commits(tmp_path):
    board = Board(tmp_path)
    rauc_install(board)
    hooks(board).tryboot()
    board.firmware_boot(tryboot_flag=True)
    (board.persistent / "sheregesh-rauc/install.json").unlink()
    res = hooks(board).health()
    assert res["result"] == "ROLLED_BACK" and res["failed"] == ["install-record"] and autoboot(board) == (2, 3)


def test_health_skips_outside_trial_and_refuses_under_agent_update(tmp_path):
    board = Board(tmp_path)
    rauc = FakeRauc(board)
    assert hooks(board, rauc=rauc).health()["result"] == "SKIP" and rauc.commands == []
    rauc_install(board)
    hooks(board).tryboot()
    board.firmware_boot(tryboot_flag=True)
    agent_state(board, "HEALTH_PENDING")
    rauc = FakeRauc(board)
    assert hooks(board, rauc=rauc).health()["result"] == "REFUSED" and rauc.commands == []


def test_on_boot_finishes_commit_interrupted_between_autoboot_and_state(tmp_path):
    board = Board(tmp_path)
    rauc_install(board)
    hooks(board).tryboot()
    board.firmware_boot(tryboot_flag=True)
    b = rauc_backend.TrybootBackend(config(board))
    b._render("B", "A")                                         # autoboot.txt записан, состояние — нет (сбой)
    board.firmware_boot(tryboot_flag=False)
    assert "подтверждение завершено" in hooks(board).on_boot()
    assert boot_state(board)["trial"] is None and boot_state(board)["slots"]["B"] == "good"


def test_raucprobe_maps_manager_unit():
    seen = []

    class P(rauc_hooks.RaucProbe):
        def sh(self, *cmd):
            seen.append(cmd[-1])
            return 0, "ActiveState=active"
    P().unit("sheregesh-ota.socket")
    P().unit("ssh")
    assert seen == ["rauc.service", "ssh"]


def test_main_usage_and_refusal_codes(tmp_path, monkeypatch, capsys):
    from rauc_helpers import write_conf
    board = Board(tmp_path)
    monkeypatch.setenv(rauc_backend.CONF_ENV, str(write_conf(board, tmp_path / "rauc.json")))
    assert rauc_hooks.main([]) == 2
    assert rauc_hooks.main(["tryboot"]) == 1                    # нечего пробовать
    assert rauc_hooks.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["backend"]["committed"] == "A"
