# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Релизный гейт «OTA CLOSURE» (QA): недостающие проверки критического пути A/B OTA — на файлах-разделах, без плат.

  • восстановление pi2 из текущего состояния (B в пробной загрузке, TRYBOOT, попытка 1, агент a7f1b74):
    какие действия возможны без root и что требуется для возврата к рабочему циклу OTA;
  • OTA-11: неподписанный пакет, манифест, изменённый после подписи, подпись другого назначения, повтор старого
    подписанного пакета после подтверждения нового;
  • OTA-12: SHA-256 распакованного образа проверяется независимо от SHA-256 сжатого; слот, загрузивший образ
    другой версии, чем в подписанном манифесте, не подтверждается.
Код software/ не меняется; используется тестовая плата test_ota.Board.
"""
import hashlib
import io
import json
import lzma
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import test_ota
from deploy_helpers import needs_ssh_keygen
from test_ota import PARTS, Board, image

from sheregesh.deploy import sig
from sheregesh.update import ota, slots

pytestmark = needs_ssh_keygen
ROOT = Path(__file__).resolve().parents[2]
OLD_A, OLD_B = "a886e05", "15e4549"   # до чистки истории 2026-10-30 — 57e8fe4 (коммит только с сырыми данными удалён; код = предок, идентичен); a7f1b74 → агенты образов pi2: слот A (основной) и слот B (пробный)


def autoboot(b):
    return slots.autoboot_parts((b.bootfs / "autoboot.txt").read_text())


def payload(b, version, *, edit=None, edit_after_sign=None, namespace=sig.NS_OTA, signature=None):
    """Поток SHRGOTA1 как в Board.payload, но с правкой манифеста до подписи (edit) или после неё (edit_after_sign)."""
    blobs, imgs = [], []
    b.images_var[version] = {"lib/dpkg/status": f"image {version}\n"}
    for part, size in (("boot", PARTS[3][1]), ("system", PARTS[5][1])):
        raw = image(part, version, size)
        comp = lzma.compress(raw, format=lzma.FORMAT_XZ)
        imgs.append({"part": part, "compression": "xz", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                     "compressed_size": len(comp), "compressed_sha256": hashlib.sha256(comp).hexdigest()})
        blobs.append(comp)
    m = {"format": 1, "kind": ota.KIND, "layout": ota.LAYOUT, "compatible": "sheregesh-lab-pi4-dev-1",
         "version": version, "commit": "c" * 40, "images": imgs}
    if edit:
        edit(m)
    raw_m = json.dumps(m).encode()
    s = sig.sign(raw_m, b.key, namespace) if signature is None else signature
    if edit_after_sign:
        m2 = json.loads(raw_m)
        edit_after_sign(m2)
        raw_m = json.dumps(m2).encode()
    buf = io.BytesIO()
    buf.write(ota.MAGIC)
    buf.write(b"%d\n" % len(raw_m) + raw_m)
    buf.write(b"%d\n" % len(s) + s)                     # пустая подпись → «0\n» (неверная длина блока)
    for blob in blobs:
        buf.write(blob)
    return buf.getvalue()


def untouched(b, before, autoboot0):
    after = b.part_hashes()
    return all(before[n] == after[n] for n in PARTS) and (b.bootfs / "autoboot.txt").read_text() == autoboot0


# ---------------------------------------------------------------- восстановление pi2


def pi2_now(tmp_path):
    """Состояние pi2 на 2026-10-25 (docs/acceptance/s3.2-ota/step5/confirm.json): B в пробной загрузке, TRYBOOT,
    попытка 1 после health FAIL; основной — A ([all]=2, [tryboot]=3); разрешение переключения действует."""
    b = Board(tmp_path)
    b.enable()
    assert b.request("install", b.payload("1.1.0"))[0]["result"] == "OK"
    assert b.request("tryboot")[0]["result"] == "OK"
    b.firmware_boot(tryboot_flag=True)
    assert b.agent().on_boot()["state"] == "TRYBOOT → HEALTH_PENDING"
    b.health = "FAIL"
    assert b.request("confirm")[0]["result"] == "FAILED"
    st = b.request("status")[0]
    assert st["state"]["state"] == "TRYBOOT" and st["state"]["boot_attempts"] == 1 and st["slots"]["active"] == "B"
    assert st["slots"]["tryboot_now"] and autoboot(b) == (2, 3)
    b.commands.clear()
    return b


def _git_show(rev, path):
    try:
        return subprocess.run(["git", "-C", str(ROOT), "show", f"{rev}:{path}"], capture_output=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip(f"нет git-истории {rev}")


def test_b_agent_install_and_abort_logic_is_the_tested_code():
    """Выводы ниже о поведении агента слота B pi2 относятся к a7f1b74: ota.py и slots.py с тех пор не менялись."""
    for f in ("software/sheregesh/update/ota.py", "software/sheregesh/update/slots.py"):
        assert _git_show(OLD_B, f) == (ROOT / f).read_bytes(), f"{f} изменился после {OLD_B}: пересмотреть выводы"


def test_pi2_on_trial_b_cannot_take_new_package_without_returning_to_a(tmp_path):
    """Удержание pi2 на B не приближает новый пакет: агент B отвергает установку в TRYBOOT, confirm возможен только
    из HEALTH_PENDING, а abort на пробной загрузке — только обычная перезагрузка (вернёт A, состояние не меняет)."""
    b = pi2_now(tmp_path)
    before, ab0 = b.part_hashes(), (b.bootfs / "autoboot.txt").read_text()
    res, _ = b.request("install", b.payload("1.2.0"))
    assert res["result"] == "REJECTED" and "TRYBOOT" in res["reason"]
    assert b.request("confirm")[0]["result"] == "REJECTED"
    assert untouched(b, before, ab0) and b.commands == []
    res, _ = b.request("abort")
    assert res["result"] == "OK" and b.commands == [["reboot"]]
    assert b.request("status")[0]["state"]["state"] == "TRYBOOT"           # до перезагрузки состояние прежнее
    assert untouched(b, before, ab0)


def test_pi2_second_tryboot_with_same_failing_confirm_ends_rolled_back_and_inconsistent(tmp_path):
    """Повтор tryboot на B (попытка 2) с тем же дефектом confirm: ROLLED_BACK, состояние не согласовано со слотом B,
    установка отвергнута — путь хуже abort (лишняя перезагрузка, исчерпаны попытки)."""
    b = pi2_now(tmp_path)
    assert b.request("tryboot")[0]["result"] == "OK" and b.commands == [["reboot", "0 tryboot"]]
    b.firmware_boot(tryboot_flag=True)
    assert b.agent().on_boot()["state"] == "TRYBOOT → HEALTH_PENDING"
    assert b.request("confirm")[0]["result"] == "FAILED"
    st = b.request("status")[0]
    assert st["state"]["state"] == "ROLLED_BACK" and st["consistent"] is False and autoboot(b) == (2, 3)
    assert b.request("install", b.payload("1.2.0"))[0]["result"] == "REJECTED"


PI2_STATE = {"state": "TRYBOOT", "active": "A", "target": "B", "version_active": "0.1.1792843040",
             "version_target": "0.1.1792865618", "boot_attempts": 1, "reason": "health FAIL, повтор",
             "history": ["IDLE->VERIFIED", "VERIFIED->INSTALLING", "INSTALLING->INSTALLED", "INSTALLED->TRYBOOT",
                         "TRYBOOT->HEALTH_PENDING (попытка 1)", "HEALTH_PENDING->TRYBOOT (health FAIL, повтор)"],
             "expect": {}}

OLD_A_SCRIPT = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from sheregesh.update import model
path, mode = sys.argv[2], sys.argv[3]
try:
    u = model.Updater(model.FakeSlots(), path, compatible="sheregesh-lab-pi4-dev-1")
except TypeError as e:
    print(json.dumps({"load": "TypeError", "error": str(e)})); sys.exit(0)
u.on_boot("A")                                  # служба sheregesh-ota-boot после загрузки A
after_boot = u.s.state
u.verify({"version": sys.argv[4], "compatible": "sheregesh-lab-pi4-dev-1", "signature_ok": True})
print(json.dumps({"load": "ok", "after_boot": after_boot, "verify": u.s.state, "reason": u.s.reason}))
"""


@pytest.fixture
def old_a_agent(tmp_path):
    try:
        raw = subprocess.run(["git", "-C", str(ROOT), "archive", OLD_A, "software/sheregesh/update",
                              "software/sheregesh/__init__.py"], capture_output=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip(f"нет git-истории {OLD_A}")
    with tarfile.open(fileobj=io.BytesIO(raw)) as t:
        t.extractall(tmp_path / "old", filter="data")

    def run(state, version):
        p = tmp_path / "state.json"
        if state is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(json.dumps(state, ensure_ascii=False))
        r = subprocess.run([sys.executable, "-I", "-c", OLD_A_SCRIPT, str(tmp_path / "old/software"), str(p), "x",
                            version], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)
    return run


def test_pi2_return_to_a_needs_state_repair_and_repair_restores_the_cycle(old_a_agent):
    """После возврата на A агент 57e8fe4 не читает state.json pi2 (поле expect) — нужен ремонт файла с root (карта на
    Linux: p6, sheregesh-ota/state.json). Удаление одного поля expect достаточно: on_boot переводит в ROLLED_BACK,
    пакет более новой версии принимается, старее 0.1.1792843040 — отвергается."""
    assert old_a_agent(PI2_STATE, "0.1.1792900000")["load"] == "TypeError"
    repaired = {k: v for k, v in PI2_STATE.items() if k != "expect"}
    r = old_a_agent(repaired, "0.1.1792900000")
    assert r == {"load": "ok", "after_boot": "ROLLED_BACK", "verify": "VERIFIED", "reason": ""}
    assert old_a_agent(repaired, "0.1.1792800000")["verify"] == "FAILED"


# ---------------------------------------------------------------- OTA-11: пакет отвергается до записи


@pytest.mark.parametrize("case", ["empty", "garbage", "wrong_ns_enable", "manifest_version", "manifest_sha"])
def test_unsigned_or_altered_or_misused_signature_rejected(tmp_path, case):
    b = Board(tmp_path)
    before, ab0 = b.part_hashes(), (b.bootfs / "autoboot.txt").read_text()
    data = {"empty": lambda: payload(b, "1.1.0", signature=b""),
            "garbage": lambda: payload(b, "1.1.0", signature=b"-----BEGIN SSH SIGNATURE-----\nAAAA\n"),
            "wrong_ns_enable": lambda: payload(b, "1.1.0", namespace=sig.NS_OTA_ENABLE),
            "manifest_version": lambda: payload(b, "1.1.0", edit_after_sign=lambda m: m.update(version="9.9.9")),
            "manifest_sha": lambda: payload(b, "1.1.0", edit_after_sign=lambda m: m["images"][1].update(
                sha256="0" * 64))}[case]()
    res, _ = b.request("install", data)
    assert res["result"] == "REJECTED", res
    assert untouched(b, before, ab0) and b.commands == []
    assert b.request("status")[0]["state"]["state"] == "IDLE"


@pytest.mark.parametrize("ns", [sig.NS_OTA, sig.NS_APP])
def test_enable_token_signed_for_other_purpose_rejected(tmp_path, ns):
    b = Board(tmp_path)
    token = json.dumps({"kind": "sheregesh-ota-enable", "serial": test_ota.SERIAL, "switching": True}).encode()
    res, _ = b.request("enable", token=token.decode(), sig=sig.sign(token, b.key, ns).decode())
    assert res["result"] == "REJECTED"
    assert not (b.persistent / "sheregesh-ota/enable.json").exists()


def test_replay_of_older_signed_package_after_commit_rejected(tmp_path):
    """После подтверждения 1.2.0 ранее подписанный пакет 1.1.0 (как a7f1b74 после нового выпуска) не ставится."""
    b = Board(tmp_path)
    b.enable()
    old = b.payload("1.1.0")
    assert b.request("install", b.payload("1.2.0"))[0]["result"] == "OK"
    assert b.request("tryboot")[0]["result"] == "OK"
    b.firmware_boot(tryboot_flag=True)
    b.agent().on_boot()
    assert b.request("confirm")[0]["state"]["state"] == "COMMITTED"
    b.firmware_boot(tryboot_flag=False)
    b.agent().on_boot()
    before, ab0 = b.part_hashes(), (b.bootfs / "autoboot.txt").read_text()
    res, _ = b.request("install", old)
    assert res["result"] == "FAILED" and "AUD-014" in res["reason"]
    assert untouched(b, before, ab0)


@pytest.mark.parametrize("version, ok", [("1.0.9", False), ("1.0.10", True), ("1.0.11", True), ("1.0.100", True)])
def test_version_compared_numerically_equal_reinstall_allowed(tmp_path, version, ok):
    b = Board(tmp_path)
    (b.t / "build.json").write_text(json.dumps({"ota_version": "1.0.10"}))
    res, _ = b.request("install", b.payload(version))
    assert (res["result"] == "OK") is ok, res


# ---------------------------------------------------------------- OTA-12: целостность и версия образа


def test_raw_image_hash_checked_independently_of_compressed_hash(tmp_path):
    """Подписанный манифест с верным SHA-256 сжатого, но неверным SHA-256 образа: запись отвергнута, tryboot нет."""
    b = Board(tmp_path)
    b.enable()
    before = b.part_hashes()
    res, _ = b.request("install", payload(b, "1.1.0", edit=lambda m: m["images"][1].update(sha256="f" * 64)))
    assert res["result"] == "FAILED" and res["state"]["state"] == "FAILED"
    assert all(before[n] == b.part_hashes()[n] for n in (1, 2, 4, 6)) and autoboot(b) == (2, 3)
    assert b.request("tryboot")[0]["result"] == "REJECTED" and b.commands == []


def test_slot_running_other_version_than_manifest_is_not_confirmed(tmp_path, monkeypatch):
    b = Board(tmp_path)
    b.enable()
    assert b.request("install", b.payload("1.1.0"))[0]["result"] == "OK"
    assert b.request("tryboot")[0]["result"] == "OK"
    b.firmware_boot(tryboot_flag=True)
    b.agent().on_boot()
    monkeypatch.setattr(test_ota.FakeProbe, "build", lambda self: {"ota_version": "1.0.0", "commit": "d" * 40})
    res, _ = b.request("confirm")
    failed = [c["id"] for c in res["checks"] if c["mandatory"] and c["status"] != "PASS"]
    assert res["result"] == "FAILED" and failed == ["version"], failed
    assert autoboot(b) == (2, 3) and res["state"]["state"] != "COMMITTED"

