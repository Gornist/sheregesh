# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S3.2-OTA: двенадцать свойств модели A/B и критерии подтверждения нового слота — на файлах-разделах, без блочных
устройств Raspberry Pi, без root и без перезагрузок (тестовая плата — test_ota.Board)."""
import io
import json
import os

import pytest
from test_ota import PARTS, SERIAL, Board  # noqa: F401 — та же «плата», что в test_ota

from sheregesh.update import ota, slots
from sheregesh.update.model import UpdateError
from deploy_helpers import keypair, needs_ssh_keygen

pytestmark = needs_ssh_keygen


def autoboot(b):
    return slots.autoboot_parts((b.bootfs / "autoboot.txt").read_text())


# 1, 10 — активный слот не перезаписывается, в том числе при повторной установке
def test_reinstall_replaces_only_inactive_slot(tmp_path):
    b = Board(tmp_path)
    before = b.part_hashes()
    assert b.request("install", b.payload("1.1.0"))[0]["result"] == "OK"
    res, _ = b.request("install", b.payload("1.2.0"))
    assert res["result"] == "OK" and res["state"]["version_target"] == "1.2.0" and res["state"]["target"] == "B"
    after = b.part_hashes()
    assert all(before[n] == after[n] for n in (1, 2, 4, 6)) and autoboot(b) == (2, 3)
    assert (b.dev / "mmcblk0p5").read_bytes().startswith(b"SYSTEM-v1.2.0")


# 2, 3 — неактивный слот однозначен; расхождение источников — отказ
@pytest.mark.parametrize("breakage", ["missing_link", "other_disk", "bootconfig_label"])
def test_more_inconsistencies_refuse(tmp_path, breakage):
    b = Board(tmp_path)
    if breakage == "missing_link":
        (b.dev / "disk/by-slot/other/boot").unlink()
    elif breakage == "other_disk":
        d = b.t / "sys/devices/mmcblk1/mmcblk1p5"
        d.mkdir(parents=True)
        (d / "uevent").write_text("PARTN=5\nPARTNAME=system_b\n")
        (b.t / "sys/class/block/mmcblk0p5").unlink()
        os.symlink(d, b.t / "sys/class/block/mmcblk0p5")
    else:
        (b.t / "sys/devices/mmcblk0/mmcblk0p1/uevent").write_text("PARTN=1\nPARTNAME=boot\n")
    before = b.part_hashes()
    with pytest.raises(UpdateError):
        slots.detect(b.paths())
    with pytest.raises(UpdateError):
        b.request("install", b.payload("1.1.0"))                       # агент отказывает до чтения потока
    assert b.part_hashes() == before


class CountingStream(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.consumed = 0

    def read(self, n=-1):
        chunk = super().read(n)
        self.consumed += len(chunk)
        return chunk


# 4 — неверная подпись: отказ до обработки образов (образы даже не читаются)
@pytest.mark.parametrize("who", ["foreign", "login_like"])
def test_bad_signature_rejected_before_images_are_read(tmp_path, who):
    b = Board(tmp_path)
    data = b.payload("1.1.0", key=keypair(tmp_path, who))
    s = CountingStream(data)
    out = []
    res = b.agent().handle({"action": "install"}, s, out.append)
    assert res["result"] == "REJECTED" and "подпис" in res["reason"]
    header = len(ota.MAGIC)
    rest = data[header:]
    n1, _, rest = rest.partition(b"\n")
    rest = rest[int(n1):]
    n2, _, _ = rest.partition(b"\n")
    signed_part = header + len(n1) + 1 + int(n1) + len(n2) + 1 + int(n2)
    assert s.consumed == signed_part < len(data)                       # ни байта образов


# 5 — несовместимый образ (другая разметка)
def test_other_layout_rejected(tmp_path, monkeypatch):
    b = Board(tmp_path)
    monkeypatch.setattr(ota, "LAYOUT", "other-layout")
    data = b.payload("1.1.0")
    monkeypatch.setattr(ota, "LAYOUT", "image-rota-gpt-ab-1")
    before = b.part_hashes()
    res, _ = b.request("install", data)
    assert res["result"] == "REJECTED" and "разметка" in res["reason"] and b.part_hashes() == before


# 7 — несовпадение SHA-256 при повторном чтении после записи
def test_readback_mismatch_fails_and_blocks_tryboot(tmp_path, monkeypatch):
    b = Board(tmp_path)
    b.enable()
    monkeypatch.setattr(slots, "readback", lambda dev, size: "0" * 64)    # носитель вернул не то, что записано
    res, _ = b.request("install", b.payload("1.1.0"))
    assert res["result"] == "FAILED" and res["state"]["state"] == "FAILED"
    assert b.request("tryboot")[0]["result"] == "REJECTED" and autoboot(b) == (2, 3) and b.commands == []


# 8, 9 — прерывание записи (агент убит) и перезапуск агента
def test_interrupted_install_never_becomes_bootable(tmp_path):
    b = Board(tmp_path)
    b.enable()
    assert b.request("install", b.payload("1.1.0"))[0]["result"] == "OK"
    st = json.loads((b.persistent / "sheregesh-ota/state.json").read_text())
    st.update(state="INSTALLING")                                       # агент умер посреди записи
    (b.persistent / "sheregesh-ota/state.json").write_text(json.dumps(st))
    res, _ = b.request("tryboot")                                       # новый процесс агента
    assert res["result"] == "REJECTED" and autoboot(b) == (2, 3) and b.commands == []
    assert b.request("status")[0]["state"]["state"] == "FAILED"
    st.update(state="INSTALLING")
    (b.persistent / "sheregesh-ota/state.json").write_text(json.dumps(st))
    assert b.agent().on_boot()["state"] == "INSTALLING → FAILED"        # и при перезагрузке


def test_state_survives_agent_restart_between_steps(tmp_path):
    b = Board(tmp_path)
    b.enable()
    assert b.request("install", b.payload("1.1.0"))[0]["result"] == "OK"
    assert b.request("status")[0]["state"]["state"] == "INSTALLED"      # другой экземпляр агента
    assert b.request("tryboot")[0]["result"] == "OK"
    b.firmware_boot(tryboot_flag=True)
    assert b.agent().on_boot()["state"] == "TRYBOOT → HEALTH_PENDING"
    assert b.request("status")[0]["consistent"]


# 11, 12 — ключи и конфигурация не меняются; откат оставляет прежнюю систему загружаемой
def test_rollback_keeps_previous_system_and_shared_data(tmp_path):
    b = Board(tmp_path)
    b.enable()
    a_parts, pers = {n: b.part_hashes()[n] for n in (2, 4)}, b.persistent_hashes()
    assert b.request("install", b.payload("1.1.0"))[0]["result"] == "OK"
    for _ in range(2):                                                  # две неудачные попытки (UPD-004)
        assert b.request("tryboot")[0]["result"] == "OK"
        b.firmware_boot(tryboot_flag=True)
        b.agent().on_boot()
        b.health = "FAIL"
        b.request("confirm")
    b.firmware_boot(tryboot_flag=False)
    b.agent().on_boot()
    assert b.slot == "a" and autoboot(b) == (2, 3)
    assert {n: b.part_hashes()[n] for n in (2, 4)} == a_parts and b.persistent_hashes() == pers
    assert b.request("status")[0]["state"]["state"] in ("ROLLED_BACK", "IDLE")


# Перенос конфигурации: свежая копия перед пробной загрузкой, старая не перезаписывает новую
def test_config_carried_fresh_at_tryboot_and_not_copied_back(tmp_path):
    b = Board(tmp_path)
    b.enable()
    cfg = b.var / "lib/sheregesh/config/active"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("r7\n")
    assert b.request("install", b.payload("1.1.0"))[0]["result"] == "OK"
    cfg.write_text("r8\n")                                              # изменение после установки, до tryboot
    assert b.request("tryboot")[0]["result"] == "OK"
    new_cfg = b.persistent / "slots/system_b/var/lib/sheregesh/config/active"
    assert new_cfg.read_text() == "r8\n"                                # не устаревшая r7
    b.firmware_boot(tryboot_flag=True)
    b.agent().on_boot()
    new_cfg.write_text("r9-trial\n")                                    # изменения на пробном слоте
    assert b.request("abort")[0]["result"] == "OK"
    b.firmware_boot(tryboot_flag=False)
    assert cfg.read_text() == "r8\n"                                    # прежний слот видит свою конфигурацию


# Критерии подтверждения
def console_board(tmp_path):
    b = Board(tmp_path)
    (b.persistent / "home/sheregesh-ui/.config/sheregesh").mkdir(parents=True)
    (b.persistent / "home/sheregesh-ui/.config/sheregesh/console.role").write_text("urn\n")
    b.status_files = {"/run/sheregesh-ui/link-status.json": {"phase": "connected", "tls": "TLSv1.3"},
                      "/run/sheregesh-ui/ui-state.json": {"video": {"mode": "remote-tls", "state": "playing",
                                                                    "decoded_fps_5s": 30.0}}}
    b.enable()
    assert b.request("install", b.payload("1.1.0"))[0]["result"] == "OK"
    assert b.request("tryboot")[0]["result"] == "OK"
    b.firmware_boot(tryboot_flag=True)
    b.agent().on_boot()
    return b


@pytest.mark.parametrize("breakage, gate", [
    ("restarts", "unit.sheregesh-console"), ("no_mtls", "mtls"), ("no_video", "video"), ("uptime", "uptime"),
    ("role_lost", "same.role"), ("machine_id", "same.machine_id"), ("host_key", "same.ssh_host_key"),
    ("ssh_down", "unit.ssh"), ("failed_unit", "failed-units"), ("health", "health")])
def test_confirm_refused_when_any_mandatory_gate_fails(tmp_path, breakage, gate):
    b = console_board(tmp_path)
    if breakage == "restarts":
        b.units["sheregesh-console.service"] = {"NRestarts": "2"}
    elif breakage == "no_mtls":
        b.status_files["/run/sheregesh-ui/link-status.json"] = {"phase": "lost"}
    elif breakage == "no_video":
        b.status_files["/run/sheregesh-ui/ui-state.json"] = {"video": {"mode": "off", "state": "off"}}
    elif breakage == "uptime":
        b.uptime = 20.0
    elif breakage == "role_lost":
        (b.persistent / "home/sheregesh-ui/.config/sheregesh/console.role").unlink()
    elif breakage == "machine_id":
        (b.persistent / "common/etc/machine-id").write_text("ffffffffffffffffffffffffffffffff\n")
    elif breakage == "host_key":
        (b.persistent / "shared/etc/ssh/ssh_host_ed25519_key.pub").write_text("NEW\n")
    elif breakage == "ssh_down":
        b.units["ssh"] = {"ActiveState": "inactive"}
    elif breakage == "failed_unit":
        b.units["something.service"] = {"ActiveState": "failed"}
    else:
        b.health = "FAIL"
    res, _ = b.request("confirm")
    failed = [c["id"] for c in res["checks"] if c["status"] != "PASS" and c["mandatory"]]
    assert res["result"] == "FAILED" and gate in failed, (failed, res["reason"])
    assert res["state"]["state"] != "COMMITTED" and autoboot(b) == (2, 3)      # основной слот не сменился


def test_confirm_commits_console_when_all_gates_pass(tmp_path):
    b = console_board(tmp_path)
    res, _ = b.request("confirm")
    assert res["result"] == "OK", [c for c in res["checks"] if c["status"] != "PASS"]
    assert autoboot(b) == (3, 2) and res["state"]["state"] == "COMMITTED"
    assert {c["id"] for c in res["checks"]} >= {"slot", "version", "persistent", "same.role", "same.device_crt",
                                                "unit.sheregesh-console", "health.network", "mtls", "video"}


def test_remote_role_gates(tmp_path):
    b = Board(tmp_path)
    (b.persistent / "home/sheregesh-ui/.config/sheregesh").mkdir(parents=True)
    (b.persistent / "home/sheregesh-ui/.config/sheregesh/remote.role").write_text("urn\n")
    b.status_files = {"/run/sheregesh-remote/link-status.json": {
        "connected": True, "tls": True, "peer": {"addr": "192.168.1.92"},
        "video": {"port": 7444, "transport": "tls1.3-mutual", "active": True}}}
    b.enable()
    assert b.request("install", b.payload("1.1.0"))[0]["result"] == "OK"
    assert b.request("tryboot")[0]["result"] == "OK"
    b.firmware_boot(tryboot_flag=True)
    b.agent().on_boot()
    b.units["sheregesh-remote-net.service"] = {"NRestarts": "1"}       # перезапуск службы блока на новом слоте
    res, _ = b.request("confirm")
    assert res["result"] == "FAILED" and "unit.sheregesh-remote-net" in res["reason"]
    assert "unit.sheregesh-console" not in {c["id"] for c in res["checks"]}   # у блока пульт не обязателен
