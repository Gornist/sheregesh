# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""state.json между версиями агента OTA на файлах-разделах (спринт OTA CLOSURE, OTA-01/02/03/07/08/10).

Файл состояния общий для слотов (/persistent/sheregesh-ota): install выполняет агент активного слота, после tryboot
состояние ведёт агент нового слота, после отката — снова агент прежнего. Здесь каждый слот тестовой платы (Board из
test_ota.py) исполняет НАСТОЯЩИЙ код своего образа: 57e8fe4 (слот A pi1/pi2), a7f1b74 (пробный B pi2) — из git
archive под именем пакета sheregesh_<rev> (импорты в update/deploy относительные), v2 — рабочее дерево.
"""
import dataclasses
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from test_ota import Board, FakeProbe, TestSeeder

from sheregesh.update import model, slots
from sheregesh.update import ota as ota_v2

ROOT = Path(__file__).resolve().parents[2]
OLD, PI2_B = "a886e05", "15e4549"   # до чистки истории 2026-10-30 — 57e8fe4 (коммит только с сырыми данными удалён; код = предок, идентичен); a7f1b74 → образ слота A pi1/pi2; образ пробного B pi2 (2026-10-25)
ROLLBACK_STATES = ("TRYBOOT", "HEALTH_PENDING", "ROLLED_BACK", "COMMITTED", "FAILED")


def _load(rev, base: Path):
    name = f"sheregesh_{rev}"
    if name not in sys.modules:
        try:
            raw = subprocess.run(["git", "-C", str(ROOT), "archive", rev, "software/sheregesh/__init__.py",
                                  "software/sheregesh/update", "software/sheregesh/deploy"],
                                 capture_output=True, check=True).stdout
        except (subprocess.CalledProcessError, FileNotFoundError):
            # в CI история обязательна (fetch-depth: 0): пропуск здесь скрыл бы проверку совместимости агентов
            (pytest.fail if os.environ.get("CI") else pytest.skip)(f"нет git-истории {rev}")
        with tarfile.open(fileobj=io.BytesIO(raw)) as t:
            t.extractall(base / rev, filter="data")
        (base / "pkgs").mkdir(exist_ok=True)
        shutil.move(base / rev / "software/sheregesh", base / "pkgs" / name)
        if str(base / "pkgs") not in sys.path:
            sys.path.insert(0, str(base / "pkgs"))
    return importlib.import_module(f"{name}.update.ota")


@pytest.fixture(scope="module")
def code(tmp_path_factory):
    base = tmp_path_factory.mktemp("agents")
    return {OLD: _load(OLD, base), PI2_B: _load(PI2_B, base), "v2": ota_v2}


def _model(ota_mod):
    return sys.modules[ota_mod.__package__ + ".model"]


class XBoard(Board):
    """Плата, у которой агент — код образа загруженного слота."""

    def __init__(self, tmp, code_a, code_b):
        super().__init__(tmp)
        self.code = {"a": code_a, "b": code_b}

    def agent(self):
        mod = self.code[self.slot]
        slots_mod = sys.modules[mod.__package__ + ".slots"]
        kw = dict(paths=slots_mod.Paths(**dataclasses.asdict(self.paths())), state_dir=self.state.parent,
                  signers=self.signers, build_json=self.t / "build.json", serial=self.t / "serial",
                  dev_mode=self.etc / "DEV-MODE", health_cmd=("health",), reboot_cmd=("reboot",),
                  tryboot_cmd=("reboot", "0 tryboot"), root=self.root, probe=FakeProbe(self))
        names = {f.name for f in dataclasses.fields(mod.AgentConfig)}
        return mod.Agent(mod.AgentConfig(**{k: v for k, v in kw.items() if k in names}), run=self.run,
                         seeder=TestSeeder(self))

    @property
    def state(self):
        return self.persistent / "sheregesh-ota/state.json"

    def raw(self):
        return json.loads(self.state.read_text())

    def install(self, version):
        res, _ = self.request("install", self.payload(version))
        assert res["result"] == "OK", res
        return res

    def trial(self):
        """tryboot → прошивка грузит неактивный слот однократно → учёт загрузки агентом нового слота."""
        assert self.request("tryboot")[0]["result"] == "OK"
        self.firmware_boot(tryboot_flag=True)
        return self.agent().on_boot()

    def normal_boot(self):
        self.firmware_boot(tryboot_flag=False)
        return self.agent().on_boot()


def pi2_now(tmp_path, code):
    """Повтор пути pi2 настоящим кодом: 57e8fe4 на A ставит B, tryboot, a7f1b74 на B, confirm FAIL (дефект health)
    → TRYBOOT, попытка 1, "expect": {} (снимок docs/acceptance/s3.2-ota/step5/confirm.json)."""
    b = XBoard(tmp_path, code[OLD], code[PI2_B])
    b.enable()
    b.install("1.1.0")
    assert b.trial()["state"] == "TRYBOOT → HEALTH_PENDING"
    b.health = "FAIL"
    assert b.request("confirm")[0]["result"] == "FAILED"
    b.health = "PASS"
    return b


# ---------------------------------------------------------------- состояние pi2 сейчас


def test_pi2_state_now_matches_board_snapshot(tmp_path, code):
    b = pi2_now(tmp_path, code)
    s = b.raw()
    assert (s["state"], s["active"], s["target"], s["boot_attempts"], s["expect"]) == ("TRYBOOT", "A", "B", 1, {})
    assert s["reason"] == "health FAIL, повтор" and b.slot == "b"


def test_pi2_reboot_to_a_breaks_agent_57e8fe4(tmp_path, code):
    """(а) любая перезагрузка pi2 сейчас → A → агент 57e8fe4 падает на "expect" (on-boot, status, install)."""
    b = pi2_now(tmp_path, code)
    b.firmware_boot(tryboot_flag=False)
    assert b.slot == "a"
    with pytest.raises(TypeError, match="expect"):
        code[OLD].on_boot_main(b.agent())              # TypeError не перехватывается → юнит failed
    with pytest.raises(TypeError, match="expect"):
        b.request("status")
    with pytest.raises(TypeError, match="expect"):
        b.request("install", b.payload("1.2.0"))


def test_pi2_no_agent_action_on_b_makes_state_safe(tmp_path, code):
    """(б) без root пути нет: каждое сохранение a7f1b74 пишет "expect"; install в TRYBOOT отвергается, abort на
    пробной загрузке — только перезагрузка (в A, где 57e8fe4 упадёт), повторный tryboot и confirm снова пишут expect."""
    b = pi2_now(tmp_path, code)
    for action in ("status", "install", "confirm", "disable", "abort"):
        b.enable()
        b.commands.clear()
        res, _ = b.request(action, b.payload("1.2.0") if action == "install" else b"")
        assert "expect" in b.raw(), action
        if action == "install":
            assert res["result"] == "REJECTED"
        if action == "abort":
            assert b.commands == [["reboot"]]          # прошивка вернёт A — состояние не переписано
    b.enable()
    assert b.trial()["state"] == "TRYBOOT → HEALTH_PENDING"     # вторая и последняя попытка (UPD-004)
    b.health = "FAIL"                                           # confirm a7f1b74 — тот же дефект health
    b.request("confirm")
    s = b.raw()
    assert s["state"] == "ROLLED_BACK" and s["expect"] == {}
    assert b.request("install", b.payload("1.2.0"))[0]["result"] == "REJECTED"   # B не основной: несогласовано
    b.firmware_boot(tryboot_flag=False)
    with pytest.raises(TypeError, match="expect"):
        b.agent().on_boot()


@pytest.mark.parametrize("repair", ["remove_file", "drop_expect"])
def test_pi2_repair_then_ota_with_v2_package(tmp_path, code, repair):
    """Ремонт на карте (p6): state.json убран целиком или удалён только ключ expect → A (57e8fe4) работает, ставит
    пакет v2 в B; агент v2 на B: HEALTH_PENDING, expect пуст (57e8fe4 его не снимает) — confirm по present.*."""
    b = pi2_now(tmp_path, code)
    b.code["b"] = code["v2"]                      # B будет перезаписан пакетом из v2
    if repair == "remove_file":
        b.state.rename(b.state.with_name("state.json.a7f1b74"))
    else:
        s = b.raw()
        del s["expect"]
        b.state.write_text(json.dumps(s, ensure_ascii=False))
    r = b.normal_boot()                           # обычная загрузка: прошивка грузит основной A
    assert b.slot == "a"
    if repair == "remove_file":
        assert r["state"] == "IDLE → IDLE"
        st = b.request("status")[0]["state"]
        assert (st["state"], st["active"], st["version_active"]) == ("IDLE", "A", "1.0.0")   # из build.json
    else:
        assert r["state"] == "TRYBOOT → ROLLED_BACK"
    assert code[OLD].on_boot_main(b.agent()) == 0
    b.install("1.2.0")                            # 57e8fe4 пишет B; expect не снимает
    assert "expect" not in b.raw()
    assert b.trial()["state"] == "TRYBOOT → HEALTH_PENDING"
    assert "expect" not in b.raw()                # v2 не пишет пустой expect: откат на A безопасен
    res, _ = b.request("confirm")
    ids = {c["id"]: c["status"] for c in res["checks"]}
    assert res["result"] == "OK" and res["state"]["state"] == "COMMITTED", res["reason"]
    assert ids["present.machine_id"] == ids["present.ssh_host_key"] == ids["present.authorized_keys"] == "PASS"
    assert not [k for k in ids if k.startswith("same.")]
    assert b.normal_boot()["state"] == "COMMITTED → COMMITTED" and b.slot == "b"


def test_pi2_repaired_empty_expect_does_not_hide_missing_identity(tmp_path, code):
    """Пустой expect не ослабляет confirm v2 до «всегда PASS»: пропавший ключ хоста SSH на новом слоте → FAILED."""
    b = pi2_now(tmp_path, code)
    b.code["b"] = code["v2"]
    b.state.unlink()
    b.normal_boot()
    b.install("1.2.0")
    b.trial()
    (b.persistent / "shared/etc/ssh/ssh_host_ed25519_key.pub").unlink()
    res, _ = b.request("confirm")
    ids = {c["id"]: c["status"] for c in res["checks"]}
    assert res["result"] == "FAILED" and ids["present.ssh_host_key"] == "FAIL"


# ---------------------------------------------------------------- OTA-01/02/03: чтение чужого state.json


def test_ota01_old_agent_creates_and_reads_its_own_state(tmp_path, code):
    b = XBoard(tmp_path, code[OLD], code[OLD])
    assert not b.state.exists()
    assert b.agent().on_boot()["state"] == "IDLE → IDLE"
    s = b.raw()
    assert (s["state"], s["active"], s["version_active"]) == ("IDLE", "A", "1.0.0") and "expect" not in s
    assert b.request("status")[0]["state"]["state"] == "IDLE"
    assert _model(code[OLD]).Updater(None, b.state).s == _model(code[OLD]).UpdateState(**s)


def _written_by(ota_mod, path, state, **extra):
    m = _model(ota_mod)
    fields = {f.name for f in dataclasses.fields(m.UpdateState)}
    kw = dict(state=state, active="A", target="B" if state in ("TRYBOOT", "HEALTH_PENDING", "INSTALLED") else None,
              boot_attempts=1, history=[f"X->{state}"], **extra)
    m.Updater(None, path, m.UpdateState(**{k: v for k, v in kw.items() if k in fields})).save()
    return json.loads(path.read_text())


@pytest.mark.parametrize("state", model.STATES)
@pytest.mark.parametrize("writer,expect", [(OLD, None), (PI2_B, {}), (PI2_B, {"role": "remote", "machine_id": "x"})])
def test_ota02_v2_reads_state_of_old_agents(tmp_path, code, state, writer, expect):
    raw = _written_by(code[writer], tmp_path / "state.json", state, **({} if expect is None else {"expect": expect}))
    assert ("expect" in raw) == (expect is not None)
    s = model.Updater(None, tmp_path / "state.json").s
    assert (s.state, s.active, s.target, s.boot_attempts, s.expect) == (state, "A", raw["target"], 1, expect or {})


@pytest.mark.parametrize("state", ROLLBACK_STATES)
def test_ota03_old_agent_reads_v2_state_without_expect(tmp_path, code, state):
    """v2 после работы с пустым expect (установка агентом 57e8fe4) — 57e8fe4 читает во всех состояниях отката."""
    raw = _written_by(ota_v2, tmp_path / "state.json", state)
    assert "expect" not in raw
    assert _model(code[OLD]).Updater(None, tmp_path / "state.json").s.state == state


def test_ota03_nonempty_expect_never_meets_old_agent(tmp_path, code):
    """Непустой expect пишет только _install агента v2+ на ОСНОВНОМ слоте, а откат ведёт на этот же слот (v2+).
    Слот с 57e8fe4 при этом — цель записи и перезаписывается. Проверка по всему пути: A(57e8fe4)→B(v2)→A(v2)."""
    b = XBoard(tmp_path, code[OLD], code["v2"])
    b.enable()
    b.install("1.1.0")
    b.trial()
    assert b.request("confirm")[0]["state"]["state"] == "COMMITTED"
    assert "expect" not in b.raw()                       # пока на A 57e8fe4 — expect пуст во всех записях
    b.code["a"] = code["v2"]                             # следующий пакет перезапишет A кодом v2
    b.install("1.2.0")
    assert b.raw()["expect"]["machine_id"] and b.slot == "b"
    b.health = "FAIL"
    for _ in range(2):
        b.trial()
        b.request("confirm")
    b.normal_boot()                                       # откат: прошивка грузит основной B (v2)
    assert b.slot == "b" and b.raw()["state"] == "ROLLED_BACK" and b.raw()["expect"]


# ---------------------------------------------------------------- OTA-07/08: откат на прежний слот и новый цикл


def test_ota07_08_rollback_to_old_agent_and_next_ota(tmp_path, code):
    """B (v2) не подтверждён дважды → ROLLED_BACK, возврат в A: 57e8fe4 читает состояние v2, принимает новый пакет,
    v2 на B его подтверждает."""
    b = XBoard(tmp_path, code[OLD], code["v2"])
    b.enable()
    b.install("1.1.0")
    b.health = "FAIL"
    for attempt in (1, 2):
        assert b.trial()["state"].endswith("HEALTH_PENDING")
        assert b.request("confirm")[0]["result"] == "FAILED"
    assert b.raw()["state"] == "ROLLED_BACK" and "expect" not in b.raw()
    assert b.request("abort")[0]["result"] == "OK" and b.commands[-1] == ["reboot"]
    assert b.normal_boot()["state"] == "ROLLED_BACK → ROLLED_BACK" and b.slot == "a"     # 57e8fe4
    st = b.request("status")[0]
    assert st["state"]["state"] == "ROLLED_BACK" and st["consistent"] is True
    b.health = "PASS"
    b.install("1.2.0")                                    # OTA-08: следующий цикл после отката
    assert b.trial()["state"] == "TRYBOOT → HEALTH_PENDING"
    assert b.request("confirm")[0]["state"]["state"] == "COMMITTED"


@pytest.mark.parametrize("cut", ["after_tryboot_request", "during_trial"])
def test_ota07_power_loss_during_v2_trial_returns_to_working_old_agent(tmp_path, code, cut):
    b = XBoard(tmp_path, code[OLD], code["v2"])
    b.enable()
    b.install("1.1.0")
    assert b.request("tryboot")[0]["result"] == "OK"
    if cut == "during_trial":
        b.firmware_boot(tryboot_flag=True)
        b.agent().on_boot()                               # v2 сохранил HEALTH_PENDING
    assert b.normal_boot()["state"].endswith("ROLLED_BACK") and b.slot == "a"
    assert code[OLD].on_boot_main(b.agent()) == 0
    b.install("1.2.0")


# ---------------------------------------------------------------- OTA-10: повторы и прерванная запись


@pytest.mark.parametrize("rev", [OLD, "v2"])
def test_ota10_repeated_on_boot_and_save_are_idempotent(tmp_path, code, rev):
    b = XBoard(tmp_path, code[OLD], code[rev])
    b.enable()
    b.install("1.1.0")
    b.trial()
    b.request("confirm")                                  # COMMITTED на B
    before = b.state.read_bytes()
    for _ in range(2):
        b.agent().on_boot()
    assert b.state.read_bytes() == before
    u = _model(code[rev]).Updater(None, b.state)
    u.save()
    u.save()
    assert b.state.read_bytes() == before


@pytest.mark.parametrize("rev", [OLD, "v2"])
def test_ota10_interrupted_atomic_write_leaves_state_intact(tmp_path, code, rev):
    """Обрыв между записью state.tmp и rename: state.json прежний, мусорный .tmp не читается и перезаписывается."""
    b = XBoard(tmp_path, code[rev], code[rev])
    b.agent().on_boot()
    before = b.state.read_bytes()
    b.state.with_suffix(".tmp").write_text('{"state": "INSTAL')
    assert b.agent().on_boot()["state"] == "IDLE → IDLE" and b.state.read_bytes() == before
    b.enable()
    b.install("1.1.0")
    assert b.raw()["state"] == "INSTALLED"
    assert not b.state.with_suffix(".tmp").exists()        # следующая запись заменила мусор и переименовала


def test_ota10_repeated_on_boot_during_trial_only_moves_towards_rollback(tmp_path, code):
    """Повторный учёт той же пробной загрузки (перезапуск oneshot — только root) считается попыткой: это сдвигает к
    откату, но не к mark-good; основной слот (autoboot [all]) не меняется."""
    b = XBoard(tmp_path, code[OLD], code["v2"])
    b.enable()
    b.install("1.1.0")
    b.trial()
    assert b.agent().on_boot()["state"] == "HEALTH_PENDING → HEALTH_PENDING" and b.raw()["boot_attempts"] == 2
    assert b.agent().on_boot()["state"] == "HEALTH_PENDING → ROLLED_BACK"
    assert slots.autoboot_parts((b.bootfs / "autoboot.txt").read_text())[0] == 2      # основной — A
    assert b.normal_boot()["state"] == "ROLLED_BACK → ROLLED_BACK" and b.slot == "a"
