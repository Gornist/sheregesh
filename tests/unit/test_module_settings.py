# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S6 (ADR-0012, раздел 8): настройки внешнего модуля в ConfigStore (схема remote-1) — заводские, восстановление,
сброс повреждённого хранилища, работа в памяти, сохранение командой configure."""
import os
import stat

import pytest

from sheregesh.config import ConfigStore, Schema, remote_defaults
from sheregesh.module.lab import InlineEmulatorBackend
from sheregesh.module.service import ModuleService
from sheregesh.module.settings import ModuleSettings, SettingsStore
from sheregesh.module.states import State, Timings
from sheregesh_emulators.devices import SimClock
from sheregesh_emulators.jrmodule import JrModuleEmulator

SCHEMA = Schema.load("remote-1")


def store(root, logs=None):
    return SettingsStore(root, log=(logs if logs is not None else []).append, clock=lambda: 1793000000.0)


def ok_validate(_s):
    return None


def test_schema_remote_1_and_defaults():
    assert SCHEMA.device == "remote" and SCHEMA.version == 1
    assert SCHEMA.validate(remote_defaults()) == []
    assert ModuleSettings.from_data(SCHEMA.parse(remote_defaults())[0]["module.yaml"]) == ModuleSettings()
    assert ModuleSettings().to_yaml() == remote_defaults()["module.yaml"]
    for bad in ("backend: hardware", "backend: uart", "enabled: 1", "profile: Bad Id", "output: active"):
        k = bad.split(":")[0]
        text = "".join(line + "\n" for line in remote_defaults()["module.yaml"].splitlines() if not line.startswith(k))
        issues = SCHEMA.validate({"module.yaml": text + bad + "\n"})
        assert issues, bad


def test_no_directory_gives_factory(tmp_path):
    s, info = store(tmp_path / "remote-config").load()
    assert s == ModuleSettings() and info == {"source": "factory", "revision": "r000001", "notice": ""}
    s, info = store(tmp_path / "remote-config").load()               # повторный запуск — из хранилища
    assert info == {"source": "store", "revision": "r000001", "notice": ""}


def test_save_and_reload(tmp_path):
    st = store(tmp_path / "rc")
    st.load()
    res, info = st.save(ModuleSettings(enabled=False, auto_init=False), ok_validate)
    assert res == "ok" and info == {"source": "store", "revision": "r000002", "notice": ""}
    s, info = store(tmp_path / "rc").load()
    assert s == ModuleSettings(enabled=False, auto_init=False) and info["revision"] == "r000002"
    assert info["source"] == "store"


def test_invalid_profile_is_not_saved(tmp_path):
    st = store(tmp_path / "rc")
    st.load()
    res, info = st.save(ModuleSettings(profile="no-such-module"), lambda s: f"unknown profile {s.profile!r}")
    assert res == "invalid" and "no-such-module" in info["text"]
    res, info = st.save(ModuleSettings(backend="hardware"), ok_validate)        # схема remote-1 не допускает
    assert res == "invalid"
    assert store(tmp_path / "rc").load()[1]["revision"] == "r000001"


def test_corrupt_state_json_gives_recovered(tmp_path):
    root = tmp_path / "rc"
    st = store(root)
    st.load()
    st.save(ModuleSettings(auto_recover=False), ok_validate)
    (root / "state.json").write_text("{broken")
    logs = []
    s, info = store(root, logs).load()
    # state.prev.json — состояние до подтверждения (ADR-0004): ревизия r000002 ещё pending и уходит в карантин
    assert s == ModuleSettings() and info["revision"] == "r000001"
    assert info["source"] == "recovered" and "CFG-STATE-CORRUPT" in info["notice"] and "CFG-STATE-PREV" in info["notice"]
    assert any("settings recovered" in x for x in logs)


def test_all_revisions_corrupt_gives_reset_and_corrupt_copy(tmp_path):
    root = tmp_path / "remote-config"
    st = store(root)
    st.load()
    for rev in (root / "revisions").iterdir():
        (rev / "module.yaml").write_text("enabled: maybe\n")
    (root / "state.json").unlink()
    (root / "state.prev.json").unlink(missing_ok=True)
    s, info = store(root).load()
    assert s == ModuleSettings() and info["source"] == "reset" and info["notice"] == "CFG-RESET"
    corrupt = [p.name for p in tmp_path.iterdir() if p.name.startswith("remote-config.corrupt-")]
    assert corrupt == ["remote-config.corrupt-20261026T073320"]
    assert ConfigStore(root, SCHEMA).active().data["module.yaml"]["enabled"] is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root пишет в каталог только для чтения")
def test_read_only_directory_gives_memory_and_not_saved(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        st = store(ro / "remote-config")
        s, info = st.load()
        assert s == ModuleSettings() and info["source"] == "memory"
        assert st.save(ModuleSettings(enabled=False), ok_validate)[0] == "not-saved"
        clock = SimClock()
        be = InlineEmulatorBackend(JrModuleEmulator(clock), clock=clock, timings=Timings())
        svc = ModuleService(ro / "remote-config", clock=clock, log=lambda s: None, backend_factory=lambda s: be)
        svc.start()
        svc.command({"req": 7, "op": "configure", "args": {"enabled": False}})
        assert svc.status()["cmd"] == {"req": 7, "op": "configure", "result": "not-saved",
                                       "error": "settings storage is not available; value not applied"}
        assert svc.manager.settings.enabled is True and svc.status()["settings"]["source"] == "memory"
    finally:
        ro.chmod(stat.S_IRWXU)


def test_no_root_is_memory():
    s, info = store(None).load()
    assert s == ModuleSettings() and info["source"] == "memory"


# ---------- configure через ModuleService ----------
class Svc:
    def __init__(self, root):
        self.clock = SimClock()
        self.be = InlineEmulatorBackend(JrModuleEmulator(self.clock), clock=self.clock, timings=Timings())
        self.logs = []
        self.svc = ModuleService(root, clock=self.clock, log=self.logs.append, backend_factory=lambda s: self.be)
        self.svc.start()

    def run(self, seconds):
        for _ in range(int(seconds / 0.1)):
            self.clock.advance(0.1)
            self.svc.tick()

    def finish(self):
        """Дождаться исполнителя (сохранение идёт в своём потоке) и забрать итог тактом."""
        if self.svc._pending is not None:
            self.svc._pending[0].result(timeout=10)
        self.run(0.1)


def test_configure_pending_then_ok_and_applied(tmp_path):
    t = Svc(tmp_path / "rc")
    t.run(3)
    assert t.svc.manager.state == State.READY
    t.svc.command({"req": 1, "op": "configure", "args": {"enabled": False}})
    assert t.svc.status()["cmd"]["result"] == "pending"
    t.svc.command({"req": 2, "op": "configure", "args": {"enabled": True}})
    assert t.svc.status()["cmd"] == {"req": 2, "op": "configure", "result": "busy",
                                     "error": "configure already in progress"}
    t.finish()
    st = t.svc.status()
    assert st["cmd"] == {"req": 1, "op": "configure", "result": "ok", "error": ""}
    assert st["state"] == "DISABLED" and st["settings"]["enabled"] is False
    assert st["settings"]["revision"] == "r000002" and st["settings"]["source"] == "store"
    assert any("settings revision r000002 saved" in x for x in t.logs)
    # перезапуск службы: те же настройки, DISABLED, бэкенд не создаётся
    t2 = Svc(tmp_path / "rc")
    assert t2.svc.manager.state == State.DISABLED and t2.svc.status()["settings"]["revision"] == "r000002"
    assert t2.be.starts == 0
    t2.svc.command({"req": 3, "op": "configure", "args": {"enabled": True}})
    t2.finish()
    t2.run(3)
    assert t2.svc.manager.state == State.READY and t2.svc.status()["settings"]["revision"] == "r000003"


def test_configure_invalid_and_unchanged(tmp_path):
    t = Svc(tmp_path / "rc")
    t.svc.command({"req": 1, "op": "configure", "args": {"profile": "no-such-module"}})
    assert t.svc.status()["cmd"]["result"] == "invalid"
    t.svc.command({"req": 2, "op": "configure", "args": {"enabled": True}})          # уже так: без новой ревизии
    assert t.svc.status()["cmd"]["result"] == "ok" and t.svc.status()["settings"]["revision"] == "r000001"


def test_configure_result_after_new_session_is_only_logged(tmp_path):
    t = Svc(tmp_path / "rc")
    t.svc.command({"req": 1, "op": "configure", "args": {"auto_init": False}})
    t.svc.new_session()
    t.finish()
    assert "cmd" not in t.svc.status() and t.svc.manager.settings.auto_init is False
