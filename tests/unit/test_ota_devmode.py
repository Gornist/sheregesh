# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Разовый режим разработчика (REQ-UPD-005/006, TEST-UPD-005/006): собственный образ без подписи проекта — только
после взвода службой устройства, ровно один, затем режим выключен; пробная загрузка, подтверждение и откат — тем же
A/B, без подписанного разрешения переключения. Тестовая плата — test_ota.Board (файлы-разделы, без root)."""
import io

import pytest
from test_ota import Board  # noqa: F401

from sheregesh.update import ota, slots
from deploy_helpers import keypair, needs_ssh_keygen

pytestmark = needs_ssh_keygen
CONSOLE = "sheregesh-console.service"


def own(b, tmp_path, version="1.1.0"):
    """Образ, подписанный не ключом проекта (собственная сборка пользователя)."""
    return b.payload(version, key=keypair(tmp_path, f"own-{version}"))


def arm(b, caller=CONSOLE):
    return b.agent().handle({"action": "dev-arm"}, io.BytesIO(b""), [].append, caller=caller)


def test_own_image_rejected_without_dev_mode(tmp_path):
    b = Board(tmp_path)
    res, _ = b.request("install", own(b, tmp_path))
    assert res["result"] == "REJECTED" and "подпис" in res["reason"]


@pytest.mark.parametrize("caller", ["", "ssh.service", "user@1000.service", "sheregesh-health.service"])
def test_dev_mode_arms_only_from_device_services(tmp_path, caller):
    b = Board(tmp_path)
    res = arm(b, caller)
    assert res["result"] == "REFUSED" and b.request("status")[0]["dev_mode"] is None


def test_exactly_one_own_image_then_mode_off(tmp_path):
    b = Board(tmp_path)
    assert arm(b)["result"] == "OK"
    st = b.request("status")[0]["dev_mode"]
    assert st["by"] == CONSOLE and st["expires"] - st["armed"] == ota.DEV_GRANT_S
    res, out = b.request("install", own(b, tmp_path))
    assert res["result"] == "OK" and res["state"]["dev_image"] is True
    assert any("режим разработчика" in m.get("msg", "") for m in out)
    assert b.request("status")[0]["dev_mode"] is None                        # снят первой установкой
    res, _ = b.request("install", own(b, tmp_path, "1.2.0"))
    assert res["result"] == "REJECTED"                                       # второй собственный — отказ


def test_failed_install_also_consumes_dev_mode(tmp_path, monkeypatch):
    b = Board(tmp_path)
    arm(b)
    monkeypatch.setattr(slots, "readback", lambda dev, size: "0" * 64)
    assert b.request("install", own(b, tmp_path))[0]["result"] == "FAILED"
    monkeypatch.undo()
    assert b.request("status")[0]["dev_mode"] is None
    assert b.request("install", own(b, tmp_path, "1.2.0"))[0]["result"] == "REJECTED"


def test_expired_dev_mode_does_not_allow(tmp_path):
    b = Board(tmp_path)
    arm(b)
    g = b.agent().dev_grant()
    g.clock = lambda: 10 ** 12
    assert g.current() is None


def test_disarm(tmp_path):
    b = Board(tmp_path)
    arm(b)
    assert b.request("dev-disarm")[0]["result"] == "OK"
    assert b.request("install", own(b, tmp_path))[0]["result"] == "REJECTED"


def test_signed_image_with_dev_mode_is_not_marked_dev(tmp_path):
    b = Board(tmp_path)
    arm(b)
    res, _ = b.request("install", b.payload("1.1.0"))
    assert res["result"] == "OK" and res["state"]["dev_image"] is False


def test_own_image_trial_and_commit_without_switch_permission(tmp_path):
    """UPD-006: тот же A/B — пробная загрузка и подтверждение собственного образа без подписанного enable."""
    b = Board(tmp_path)
    arm(b)
    assert b.request("install", own(b, tmp_path))[0]["result"] == "OK"
    assert b.request("tryboot")[0]["result"] == "OK"
    b.firmware_boot(tryboot_flag=True)
    b.agent().on_boot()
    res = b.request("confirm")[0]
    assert res["result"] == "OK" and res["state"]["state"] == "COMMITTED"
    assert b.request("tryboot")[0]["result"] == "REFUSED"                    # дальше — снова только с разрешением


def test_own_image_failing_health_rolls_back_like_signed(tmp_path):
    """UPD-006: ошибочный собственный образ откатывается к прежнему тем же механизмом, что в TEST-UPD-004."""
    b = Board(tmp_path)
    a_parts = {n: b.part_hashes()[n] for n in (2, 4)}
    arm(b)
    assert b.request("install", own(b, tmp_path))[0]["result"] == "OK"
    for _ in range(2):
        assert b.request("tryboot")[0]["result"] == "OK"
        b.firmware_boot(tryboot_flag=True)
        b.agent().on_boot()
        b.health = "FAIL"
        b.request("confirm")
    b.firmware_boot(tryboot_flag=False)
    b.agent().on_boot()
    assert b.slot == "a" and {n: b.part_hashes()[n] for n in (2, 4)} == a_parts
    assert b.request("status")[0]["state"]["state"] in ("ROLLED_BACK", "IDLE")


def test_caller_unit_from_cgroup(tmp_path, monkeypatch):
    """Служба — последний компонент *.service пути cgroup v2 процесса-клиента; нет SO_PEERCRED/файла — «»."""
    import struct

    class Sock:
        def getsockopt(self, *a):
            return struct.pack("3i", 4242, 0, 0)
    real = ota.Path

    def fake_path(p):
        if str(p) == "/proc/4242/cgroup":
            f = tmp_path / "cgroup"
            f.write_text("0::/system.slice/sheregesh-console.service\n")
            return f
        return real(p)
    monkeypatch.setattr(ota.socket, "SO_PEERCRED", 17, raising=False)
    monkeypatch.setattr(ota, "Path", fake_path)
    assert ota.caller_unit(Sock()) == CONSOLE

    class NoCred:
        def getsockopt(self, *a):
            raise OSError("нет SO_PEERCRED")
    assert ota.caller_unit(NoCred()) == ""
