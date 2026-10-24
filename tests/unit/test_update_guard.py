# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Исключение двух менеджеров A/B (прототип варианта C ADR-0009): при конфигурации RAUC dev-агент отказывает в
install/tryboot/confirm; при незавершённом обновлении агента RAUC отказывает в установке, назначении пробы и
mark-good. Без конфигурации RAUC обёртка агента ведёт себя как сам агент."""
import json
import os

import pytest
from rauc_helpers import agent_state, autoboot, config, hooks, rauc_configured
from test_ota import Board

from sheregesh.update import guard, rauc_backend
from sheregesh.update.rauc_hooks import HookError


def gpaths(board):
    (board.t / "boot_id").write_text("boot-1\n")
    return guard.GuardPaths.under(board.persistent, board.t / "etc/rauc/system.conf", board.t / "boot_id")


def guarded(board):
    a = board.agent()
    return guard.guarded_agent_class()(a.cfg, guard=guard.Guard(gpaths(board)), run=a.run, seeder=a.seeder)


def request(agent, action, stream=b"", **extra):
    import io
    out = []
    return agent.handle({"action": action, **extra}, io.BytesIO(stream), out.append)


def test_agent_refuses_install_tryboot_confirm_when_rauc_configured(tmp_path):
    board = Board(tmp_path)
    rauc_configured(board)
    before = board.part_hashes()
    payload = board.payload()
    for action, stream in (("install", payload), ("tryboot", b""), ("confirm", b"")):
        res = request(guarded(board), action, stream)
        assert res["result"] == "REFUSED" and "RAUC" in res["reason"], (action, res)
    assert board.part_hashes() == before and autoboot(board) == (2, 3)
    assert not (board.persistent / "sheregesh-ota/state.json").exists()        # агент даже не начинал
    assert request(guarded(board), "status")["result"] == "OK"                 # чтение и abort — можно
    assert request(guarded(board), "abort")["result"] == "OK"


def test_guarded_agent_without_rauc_behaves_like_agent(tmp_path):
    board = Board(tmp_path)
    res = request(guarded(board), "install", board.payload())
    assert res["result"] == "OK" and res["state"]["state"] == "INSTALLED"
    plain = Board(tmp_path / "plain")
    assert request(plain.agent(), "install", plain.payload())["state"]["state"] == "INSTALLED"
    assert not (board.persistent / "update-manager.json").exists()             # заявка снята после действия
    # обратное направление: незавершённое обновление агента — RAUC отказывает
    with pytest.raises(HookError, match="sheregesh-ota"):
        hooks(board).pre_install()


def test_agent_refuses_while_rauc_trial_pending_even_without_rauc_conf(tmp_path):
    board = Board(tmp_path)
    b = rauc_backend.TrybootBackend(config(board))
    b.set_state("B", "bad")
    b.set_primary("B")
    res = request(guarded(board), "install", board.payload())
    assert res["result"] == "REFUSED" and "пробная загрузка" in res["reason"]


@pytest.mark.parametrize("state", ["VERIFIED", "INSTALLING", "INSTALLED", "TRYBOOT", "HEALTH_PENDING"])
def test_rauc_refuses_while_agent_update_unfinished(tmp_path, state):
    """Состояние агента читается напрямую: агент образа 57e8fe4 об охраннике не знает."""
    board = Board(tmp_path)
    rauc_configured(board)
    agent_state(board, state)
    with pytest.raises(HookError, match=state):
        hooks(board).pre_install()
    with pytest.raises(rauc_backend.BackendError, match="sheregesh-ota"):
        rauc_backend.TrybootBackend(config(board)).set_primary("B")
    assert not (board.persistent / "update-manager.json").exists()


@pytest.mark.parametrize("state", ["IDLE", "COMMITTED", "ROLLED_BACK", "FAILED"])
def test_rauc_allowed_after_agent_finished(tmp_path, state):
    board = Board(tmp_path)
    rauc_configured(board)
    agent_state(board, state)
    assert "цель B" in hooks(board).pre_install()


def test_live_claim_blocks_other_manager_and_stale_claim_does_not(tmp_path):
    board = Board(tmp_path)
    g = guard.Guard(gpaths(board))
    ok, _ = g.claim(guard.AGENT, "install", pid=os.getpid())       # агент работает (процесс жив)
    assert ok
    ok, why = g.claim(guard.RAUC, "install")
    assert not ok and "sheregesh-ota" in why
    assert g.claim(guard.AGENT, "install")[0]                         # тот же менеджер — можно
    claim = json.loads((board.persistent / "update-manager.json").read_text())
    claim.update(pid=os.getpid(), boot_id="boot-0")                   # заявка прошлой загрузки
    (board.persistent / "update-manager.json").write_text(json.dumps(claim))
    assert g.check(guard.RAUC, "install")[0]
    g.release(guard.RAUC)                                             # чужую заявку не снимает
    assert (board.persistent / "update-manager.json").exists()
    g.release(guard.AGENT)
    assert not (board.persistent / "update-manager.json").exists()


def test_non_mutating_agent_actions_always_allowed(tmp_path):
    board = Board(tmp_path)
    rauc_configured(board)
    g = guard.Guard(gpaths(board))
    for action in ("status", "abort", "enable", "disable"):
        assert g.check(guard.AGENT, action)[0]
    assert not g.check("other", "install")[0]


def test_guard_cli(tmp_path, monkeypatch, capsys):
    board = Board(tmp_path)
    monkeypatch.setenv("SHEREGESH_RAUC_SYSTEM_CONF", str(board.t / "etc/rauc/system.conf"))
    assert guard.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["rauc_configured"] is False
    assert guard.main(["bogus"]) == 2
