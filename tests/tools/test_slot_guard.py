# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""slot_guard (S3.2): нет ложного PASS при недостатке доказательств. Сверка снимков-файлов «до/после» записи слота B
(шаг 1) и пробной загрузки (шаг 4); функции снимка на настоящем bash — отсутствующий файл, недоступный каталог,
сбой конвейера, прерванный сбор, неудачный SSH."""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/deploy/ota-check"))
import slot_guard as g  # noqa: E402

V = "0.1.1792865618"
H = lambda s: hashlib.sha256(s.encode()).hexdigest()           # noqa: E731
AUTOBOOT = "[all];tryboot_a_b=1;boot_partition=2;[tryboot];boot_partition=3;"
UNIT = "ActiveState=active;InvocationID={};LoadState=loaded;NRestarts=0;SubState=running;"


def ota(state="IDLE", target=None, version=None):
    return json.dumps({"state": {"state": state, "target": target, "version_target": version}, "consistent": True})


def fields_a(board="pi2"):
    role = g.BOARD_ROLE[board]
    f = {"serial": g.BOARDS[board]["serial"], "boot_id": "b-1", "package": "0.1.0~dev0+g57e8fe4",
         "build": json.dumps({"ota_version": "0.1.1792843040"}),
         "slot.root": "system_a", "slot.boot": "boot_a", "slot.link.active.system": "system_a",
         "slot.link.active.boot": "boot_a", "slot.link.other.system": "system_b", "slot.link.other.boot": "boot_b",
         "slot.dt": "boot_a", "dt.tryboot": "0", "mnt.root": "ro,relatime", "mnt.boot": "ro,noatime",
         "mounts": "/dev/mmcblk0p1 /bootfs;/dev/mmcblk0p2 /boot/firmware;/dev/mmcblk0p6 /persistent;",
         "autoboot.sha256": H(AUTOBOOT), "autoboot": AUTOBOOT, "tree.boot": H("boot_a") + " 80",
         "tree.system": H("system_a") + " 1500", "ota.enable_present": "no", "ota.status": ota(),
         "deploy.status": json.dumps({"role": role, "current": "g37adc72c2bb1"}), "failed": "none", "health": "PASS"}
    f.update({f"partname.{p}": n for p, n in g.LAYOUT.items()})
    io = {"p1": (1, 0), "p2": (0, 0), "p3": (0, 0), "p4": (16, 0), "p5": (0, 0), "p6": (250000, 0)}
    f.update({f"io.{p}": f"{w} {d}" for p, (w, d) in io.items()})
    f["io.disk"] = f"{sum(w for w, _ in io.values())} 0"
    for k in g.HOME_FILES:
        f[f"fp.~/{k}"] = "absent" if k.endswith(("console.role" if role == "remote" else "remote.role")) \
            else H(k) + " 600"
    f.update({f"fp.{k}": H(k) for k in g.SYS_FILES})
    f.update({f"unit.{u}": UNIT.format(u) for u in g.UNITS})
    return f


def snapshot(fields, board="pi2"):
    return {"meta": {"board": board, "host": "192.168.1.171", "complete": True}, "fields": fields, "errors": []}


def set_io(f, p, dw=0, dd=0):
    w, d = (int(x) for x in f[f"io.{p}"].split())
    f[f"io.{p}"] = f"{w + dw} {d + dd}"
    dw0, dd0 = (int(x) for x in f["io.disk"].split())
    f["io.disk"] = f"{dw0 + dw} {dd0 + dd}"


def installed():
    f = fields_a()
    set_io(f, "p3", g.BOOT_SIZE // 512)
    set_io(f, "p5", g.SYSTEM_SIZE // 512)
    set_io(f, "p6", 70000)                                      # /var слота B и состояние агента на persistent
    f["ota.status"] = ota("INSTALLED", "B", V)
    return f


def trial():
    f = fields_a()
    f.update({"boot_id": "b-2", "package": "0.1.0~dev0+ga7f1b74", "build": json.dumps({"ota_version": V}),
              "slot.root": "system_b", "slot.boot": "boot_b", "slot.link.active.system": "system_b",
              "slot.link.active.boot": "boot_b", "slot.link.other.system": "system_a",
              "slot.link.other.boot": "boot_a", "slot.dt": "boot_b", "dt.tryboot": "1",
              "mounts": "/dev/mmcblk0p1 /bootfs;/dev/mmcblk0p3 /boot/firmware;/dev/mmcblk0p6 /persistent;",
              "tree.boot": H("boot_b") + " 80", "tree.system": H("system_b") + " 1600", "ota.enable_present": "YES",
              "ota.status": ota("HEALTH_PENDING", "B", V)})
    io = {"p1": (0, 0), "p2": (0, 0), "p3": (0, 0), "p4": (0, 0), "p5": (24, 0), "p6": (9000, 0)}
    f.update({f"io.{p}": f"{w} {d}" for p, (w, d) in io.items()})
    f["io.disk"] = f"{sum(w for w, _ in io.values())} 0"
    f.update({f"unit.{u}": UNIT.format(u + "-2") for u in g.UNITS})
    return f


def run(before, after, mode="install"):
    verdict, res = g.compare(snapshot(before), snapshot(after), mode, V)
    return verdict, sorted({r["id"] for r in res if r["status"] != "PASS"})


# ---------------------------------------------------------------- шаг 1: запись слота B

def test_normal_install_passes():
    assert run(fields_a(), installed()) == ("PASS", [])


@pytest.mark.parametrize("key", ["io.p4", "fp.~/.local/share/sheregesh/link/device.key", "slot.dt", "health"])
def test_partial_snapshot_is_error(key):
    a = installed()
    del a[key]
    assert run(fields_a(), a)[0] == "ERROR"
    s = snapshot(installed())
    s["meta"]["complete"] = False                                # сбор прерван: нет маркера конца
    assert g.compare(snapshot(fields_a()), s, "install", V)[0] == "ERROR"


@pytest.mark.parametrize("key,value", [("tree.system", ""), ("fp./etc/machine-id", g.EMPTY_SHA),
                                       ("tree.boot", g.EMPTY_SHA + " 0"), ("io.p2", "x 0"),
                                       ("fp./etc/ssh/ssh_host_ed25519_key.pub", "absent"),
                                       ("fp.~/.config/sheregesh/remote.role", "absent"), ("ota.status", "{")])
def test_empty_or_bad_values_are_error(key, value):
    a = installed()
    a[key] = value
    assert run(fields_a(), a)[0] == "ERROR"


def test_failed_command_is_error():
    s = snapshot(installed())
    s["errors"] = ["tree.system: код 1"]                        # find/sha256sum не смогли прочитать
    assert g.compare(snapshot(fields_a()), s, "install", V)[0] == "ERROR"


def test_foreign_serial_and_board_mismatch_are_error():
    a = installed()
    a["serial"] = g.BOARDS["pi1"]["serial"]
    assert run(fields_a(), a)[0] == "ERROR"
    assert g.compare(snapshot(fields_a()), snapshot(fields_a("pi1"), "pi1"), "install", V)[0] == "ERROR"


@pytest.mark.parametrize("key,value", [("slot.dt", "boot_b"), ("slot.root", "system_b"),
                                       ("slot.link.other.system", "system_a"), ("slot.boot", "bootconfig")])
def test_ambiguous_active_slot_is_error(key, value):
    a = installed()
    a[key] = value
    assert run(fields_a(), a)[0] == "ERROR"


def test_disk_counter_not_equal_to_partitions_is_error():
    a = installed()
    w, d = a["io.disk"].split()
    a["io.disk"] = f"{int(w) + 8} {d}"                          # запись через /dev/mmcblk0 в обход разделов
    assert run(fields_a(), a)[0] == "ERROR"


@pytest.mark.parametrize("p,dw,dd", [("p4", 8, 0), ("p2", 1, 0), ("p4", 0, 2048)])
def test_write_or_discard_on_active_slot_fails(p, dw, dd):
    a = installed()
    set_io(a, p, dw, dd)
    assert run(fields_a(), a) == ("FAIL", [f"untouched.{p}.{g.LAYOUT[p]}"])


def test_active_slot_change_fails():
    a = installed()
    a.update({"slot.root": "system_b", "slot.boot": "boot_b", "slot.link.active.system": "system_b",
              "slot.link.active.boot": "boot_b", "slot.link.other.system": "system_a",
              "slot.link.other.boot": "boot_a", "slot.dt": "boot_b"})
    v, bad = run(fields_a(), a)
    assert v == "FAIL" and "slot.active_unchanged" in bad and "same.slot.dt" in bad


def test_bootconfig_change_fails():
    a = installed()
    set_io(a, "p1", 8)
    assert run(fields_a(), a) == ("FAIL", ["untouched.p1.bootconfig"])
    a = installed()
    a["autoboot"] = AUTOBOOT.replace("boot_partition=2", "boot_partition=3", 1)
    a["autoboot.sha256"] = H(a["autoboot"])
    assert run(fields_a(), a) == ("FAIL", ["bootconfig.autoboot", "same.autoboot.sha256"])


@pytest.mark.parametrize("key", ["fp.~/.local/share/sheregesh/link/device.key",
                                 "fp.~/.local/share/sheregesh/link/device.crt", "fp.~/.config/sheregesh/link.json",
                                 "fp.~/.config/sheregesh/remote.role", "fp./etc/machine-id",
                                 "fp./etc/ssh/ssh_host_ed25519_key.pub", "fp.~/.ssh/authorized_keys"])
def test_identity_change_fails(key):
    a = installed()
    a[key] = H("other") + " 600"
    assert run(fields_a(), a) == ("FAIL", [f"same.{key}"])


def test_unexpected_restart_or_reboot_fails():
    a = installed()
    a["unit.sheregesh-remote-net"] = UNIT.format("new").replace("NRestarts=0", "NRestarts=1")
    assert run(fields_a(), a) == ("FAIL", ["same.unit.sheregesh-remote-net"])
    a = installed()
    a["boot_id"] = "b-2"
    assert run(fields_a(), a) == ("FAIL", ["same.boot_id"])


def test_incomplete_write_fails():
    a = fields_a()
    set_io(a, "p3", g.BOOT_SIZE // 512)
    set_io(a, "p5", 100000)                                     # запись system_b оборвалась
    a["ota.status"] = ota("FAILED", "B", V)
    assert run(fields_a(), a) == ("FAIL", ["ota.state", "written.p5.system_b"])


def test_enable_token_or_mounted_other_slot_fails():
    a = installed()
    a["ota.enable_present"] = "YES"
    v, bad = run(fields_a(), a)
    assert v == "FAIL" and "ota.enable_absent" in bad
    a = installed()
    a["mounts"] += "/dev/mmcblk0p5 /mnt;"
    v, bad = run(fields_a(), a)
    assert v == "FAIL" and "other_slot_not_mounted" in bad


def test_unchanged_board_mode():
    a = fields_a()
    set_io(a, "p6", 500)                                         # журнал и состояние на persistent — допустимо
    assert run(fields_a(), a, "unchanged") == ("PASS", [])
    a = fields_a()
    set_io(a, "p5", 8)
    assert run(fields_a(), a, "unchanged") == ("FAIL", ["untouched.p5.system_b"])
    assert run(fields_a(), installed(), "unchanged")[0] == "FAIL"


# ---------------------------------------------------------------- шаг 4: пробная загрузка B (expect нет)

def test_trial_with_same_identity_passes():
    assert run(fields_a(), trial(), "trial") == ("PASS", [])


@pytest.mark.parametrize("key", [k for k in fields_a() if k.startswith("fp.") and fields_a()[k] != "absent"])
def test_trial_identity_change_fails(key):
    t = trial()
    t[key] = H("replaced") + (" 600" if key.startswith("fp.~/") else "")
    assert run(fields_a(), t, "trial") == ("FAIL", [f"same.{key}"])


def test_trial_negative_cases():
    t = trial()
    t["dt.tryboot"] = "0"                                       # B стал основным без пробной загрузки
    assert "dt.tryboot" in run(fields_a(), t, "trial")[1]
    t = trial()
    set_io(t, "p4", 8)                                          # кто-то писал прежний слот A
    assert run(fields_a(), t, "trial") == ("FAIL", ["untouched_since_boot.p4.system_a"])
    t = trial()
    t["unit.sheregesh-remote-net"] = t["unit.sheregesh-remote-net"].replace("NRestarts=0", "NRestarts=2")
    assert run(fields_a(), t, "trial") == ("FAIL", ["unit.sheregesh-remote-net"])
    t = trial()
    t["build"] = json.dumps({"ota_version": "0.1.1792843040"})
    assert run(fields_a(), t, "trial") == ("FAIL", ["build.version"])


# ---------------------------------------------------------------- сбор снимка

def test_parse_detects_interruption_errors_and_duplicates():
    f, e, done = g.parse("serial\t1\n!ERR\ttree.boot\tкод 1\nserial\t2\n")
    assert not done and any("tree.boot" in x for x in e) and any("повторяется" in x for x in e)
    assert g.parse("a\tb\n!END\tok\n")[2]


class _R:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_ssh_failure_and_truncated_output_are_error():
    s = g.snap("pi2", "h", runner=lambda *a, **k: _R(255, "", "Permission denied (publickey)."))
    assert g.validate(s)[0]
    full = "".join(f"{k}\t{v}\n" for k, v in installed().items())
    s = g.snap("pi2", "h", runner=lambda *a, **k: _R(0, full[: len(full) // 2]))
    assert g.validate(s)[0]
    s = g.snap("pi2", "h", runner=lambda *a, **k: _R(0, full + "!END\tok\n"))
    assert g.validate(s)[0] == []
    s = g.snap("pi2", "h", runner=lambda *a, **k: _R(1, full + "!END\tok\n"))    # bash упал после конца
    assert g.validate(s)[0]


def test_snapshot_script_is_strict():
    assert "set -euo pipefail" in g.SNAP and g.SNAP.rstrip().endswith("printf '!END\\tok\\n'")
    assert "2>/dev/null)" not in g.SNAP.replace('out=$("$@" 2>/dev/null)', "")


BASH_OK = shutil.which("bash") and shutil.which("sha256sum")


def sh(script, cwd):
    return subprocess.run(["bash", "-c", g.PRELUDE + script], cwd=cwd, capture_output=True, text=True).stdout


@pytest.mark.skipif(not BASH_OK, reason="нужны bash и sha256sum")
def test_bash_helpers_fail_closed(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d/f").write_text("x")
    (tmp_path / "locked").mkdir()
    (tmp_path / "locked/secret").write_text("y")
    (tmp_path / "empty").mkdir()
    out = sh('field ok fsha d/f; field missing fsha nope; field tree treehash d; field tree_missing treehash nope; '
             'field tree_empty treehash empty; field pipe sh -c "false | cat"; field blank printf ""; '
             'field absent fpopt nope; printf "!END\\tok\\n"', tmp_path)
    f, errs, done = g.parse(out)
    assert done and f["ok"] == H("x") and f["absent"] == "absent"
    assert f["tree"].endswith(" 1") and f["tree"].split()[0] != g.EMPTY_SHA
    bad = {e.split(":")[0] for e in errs}
    assert bad == {"missing", "tree_missing", "tree_empty", "pipe", "blank"}
    if os.geteuid() != 0:                                       # root читает и без прав — проверка недоступности смысла не имеет
        os.chmod(tmp_path / "locked", 0)
        try:
            f, errs, _ = g.parse(sh("field tree treehash d locked; printf '!END\\tok\\n'", tmp_path))
            assert "tree" not in f and errs
        finally:
            os.chmod(tmp_path / "locked", 0o755)


@pytest.mark.skipif(not BASH_OK, reason="нужны bash и sha256sum")
def test_bash_unreadable_file_is_error(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root читает без прав")
    (tmp_path / "k").write_text("key")
    os.chmod(tmp_path / "k", 0)
    try:
        f, errs, _ = g.parse(sh("field fp fsha k; field fpo fpopt k; printf '!END\\tok\\n'", tmp_path))
        assert "fp" not in f and "fpo" not in f and len(errs) == 2
    finally:
        os.chmod(tmp_path / "k", 0o600)


def test_validate_rejects_unknown_board_and_copy_is_not_mutated():
    s = snapshot(installed())
    s2 = copy.deepcopy(s)
    s2["meta"]["board"] = "pi9"
    assert g.validate(s2)[0] and g.validate(s)[0] == []
