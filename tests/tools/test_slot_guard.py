# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""slot_guard (S3.2, шаг 1): сверка «до/после» записи неактивного слота по счётчикам ядра и отпечаткам."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/deploy/ota-check"))
import slot_guard  # noqa: E402

V = "0.1.1792865618"


def status(state="IDLE", target=None, version=None, active="A", tryboot=False, default=2):
    return json.dumps({"slots": {"active": active, "tryboot_now": tryboot}, "consistent": True,
                       "state": {"state": state, "target": target, "version_target": version},
                       "autoboot": {"default_part": default, "tryboot_part": 3}})


def before():
    d = {"serial": "100000009d497e91", "boot_id": "b1", "autoboot.sha256": "f3", "tree.boot_a": "t1",
         "tree.system_a": "t2", "fp..ssh/authorized_keys": "3d 600", "unit.sheregesh-remote-net": "active running 0 i1",
         "failed": "", "health": "PASS", "ota.status": status()}
    d.update({f"io.p{n}": "10" for n in range(1, 7)})
    return d


def after(**kw):
    d = before()
    d.update({"io.p3": str(10 + 524288), "io.p5": str(10 + 5242880), "io.p6": "99999",
              "ota.status": status("INSTALLED", "B", V)})
    d.update(kw)
    return d


def failed(a):
    return [r["id"] for r in slot_guard.compare(before(), a, V) if r["status"] != "PASS"]


def test_clean_install_passes():
    assert failed(after()) == []


def test_any_write_to_active_slot_or_bootconfig_fails():
    assert failed(after(**{"io.p4": "11"})) == ["untouched.io.p4"]
    assert failed(after(**{"io.p1": "18"})) == ["untouched.io.p1"]
    assert failed(after(**{"io.p2": "12"})) == ["untouched.io.p2"]


def test_reboot_restart_key_change_autoboot_and_short_write_fail():
    assert failed(after(boot_id="b2")) == ["same.boot_id"]
    assert failed(after(**{"unit.sheregesh-remote-net": "active running 1 i2"})) == ["same.unit.sheregesh-remote-net"]
    assert failed(after(**{"fp..ssh/authorized_keys": "ff 600"})) == ["same.fp..ssh/authorized_keys"]
    assert failed(after(**{"autoboot.sha256": "00"})) == ["same.autoboot.sha256"]
    assert failed(after(**{"io.p5": "1000"})) == ["written.io.p5"]


def test_wrong_state_or_switch_fails():
    assert failed(after(**{"ota.status": status("FAILED", "B", V)})) == ["ota.state"]
    assert failed(after(**{"ota.status": status("INSTALLED", "B", V, default=3)})) == ["ota.autoboot"]
    assert failed(after(**{"ota.status": status("INSTALLED", "B", "0.1.1")})) == ["ota.state"]
