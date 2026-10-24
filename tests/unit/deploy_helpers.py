# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Общие заготовки для испытаний DEV deploy и OTA: ключи ssh-keygen, мини-пакеты, имитация systemd."""
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from sheregesh.deploy import bundle, launcher, sig

needs_ssh_keygen = pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="нет ssh-keygen")


def keypair(directory: Path, name="signer"):
    key = Path(directory) / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"test-{name}", "-f", str(key)], check=True)
    return key


def signers_file(directory: Path, key: Path, name="allowed_signers"):
    f = Path(directory) / name
    f.write_text(sig.allowed_signers_line(key.with_suffix(".pub").read_text()))
    return f


def app_files(behaviour="ok", marker="1"):
    """Минимальное «приложение» во всех разрешённых каталогах; поведение службы — в __init__.py пульта."""
    return {
        "software/sheregesh/__init__.py": (f'__version__ = "{marker}"\n'.encode(), 0o644),
        "software/emulators/sheregesh_emulators/__init__.py": (b"", 0o644),
        "software/console/sheregesh_console/__init__.py": (f'BEHAVIOUR = "{behaviour}"\n'.encode(), 0o644),
        "software/console/sheregesh_console/__main__.py": (b"import sys\nsys.exit(0)\n", 0o644),
        "software/console/sheregesh_console/qml/Main.qml": (b"import QtQuick\nItem {}\n", 0o644),
        "software/remote/sheregesh_remote/__init__.py": (b"", 0o644),
        "tools/probe/s2probe.py": (b"#!/usr/bin/env python3\n", 0o755),
    }


def make_bundle(version="0.1.0.dev0+gabc1234", behaviour="ok", commit="a" * 40):
    return bundle.build_bundle(app_files(behaviour, version), version, commit, 1790000000)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class FakeSystemd:
    """Службы «запускают» код, выбранный launcher: поведение берётся из BEHAVIOUR в выбранном каталоге.

    ok — READY и работает; nostart — не выходит в READY; crash — READY, затем падает и перезапускается в окне;
    hang — READY, затем watchdog (то же, что crash для наблюдателя).
    """

    def __init__(self, deploy_dir, factory, clock, skip=()):
        self.deploy_dir, self.factory, self.clock, self.skip = str(deploy_dir), str(factory), clock, set(skip)
        self.units, self.restarts = {}, []

    def _behaviour(self):
        root, rid, _ = launcher.select(self.deploy_dir, self.factory, current_boot="boot-1", require_root=False)
        text = (Path(root) / "software/console/sheregesh_console/__init__.py").read_text()
        m = re.search(r'BEHAVIOUR = "(\w+)"', text)
        return (m[1] if m else "ok"), rid

    def restart(self, unit, timeout):
        self.restarts.append(unit)
        if unit in self.skip:
            self.units[unit] = {"ActiveState": "inactive", "SubState": "dead", "ConditionResult": "no",
                                "NRestarts": "0", "InvocationID": ""}
            return True, "ok"
        b, rid = self._behaviour()
        st = {"ActiveState": "active", "SubState": "running", "ConditionResult": "yes", "NRestarts": "0",
              "InvocationID": uuid.uuid4().hex, "Result": "success", "release": rid, "since": self.clock(), "b": b}
        if b == "nostart":
            st.update(ActiveState="failed", SubState="failed", Result="protocol")
            self.units[unit] = st
            return False, "Job failed"
        self.units[unit] = st
        return True, "ok"

    def show(self, unit):
        st = self.units.get(unit, {"ActiveState": "inactive", "SubState": "dead", "NRestarts": "0"})
        if st.get("b") in ("crash", "hang") and self.clock() - st["since"] >= 5:
            st = dict(st, NRestarts=str(int(st["NRestarts"]) + 1), InvocationID=uuid.uuid4().hex)
            self.units[unit] = st
        return {k: v for k, v in st.items() if k not in ("since", "b", "release")}

