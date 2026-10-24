# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Общее для испытаний прототипа RAUC (вариант C ADR-0009) на тестовой плате test_ota.Board: файлы-разделы,
файловый autoboot.txt, device-tree и mountinfo — каталоги; RAUC подменяется вызовами того же бэкенда."""
import json
import shlex
from pathlib import Path

from test_ota import PARTS, Board, FakeProbe, TestSeeder, image  # noqa: F401  (переиспользование платы)

from sheregesh.update import rauc_backend, rauc_hooks, slots


def config(board: Board, **kw):
    (board.t / "boot_id").write_text("boot-1\n")
    return rauc_backend.Config(paths=board.paths(), state_dir=board.persistent / "sheregesh-rauc",
                               rauc_system_conf=board.t / "etc/rauc/system.conf", boot_id=board.t / "boot_id",
                               root=board.root, reboot_cmd=("reboot",), tryboot_cmd=("reboot", "0 tryboot"),
                               health_timeout=0, **kw)


def rauc_configured(board: Board):
    p = board.t / "etc/rauc/system.conf"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[system]\ncompatible=sheregesh-lab-pi4-dev-1\nbootloader=custom\n")


class FakeRauc:
    """`rauc status mark-good|mark-bad booted|other` → тот же бэкенд (как его вызвал бы RAUC); прочее — журнал."""

    def __init__(self, board):
        self.board, self.commands = board, []

    def __call__(self, cmd, **kw):
        cmd = list(cmd)
        self.commands.append(shlex.join(cmd))
        rc, out = 0, ""
        if cmd[:2] == ["rauc", "status"]:
            b = rauc_backend.TrybootBackend(config(self.board))
            booted = b.get_current()
            slot = booted if cmd[3] == "booted" else b.other(booted)
            try:
                out = b.set_state(slot, "good" if cmd[2] == "mark-good" else "bad")
            except rauc_backend.BackendError as e:
                rc, out = 1, str(e)

        class R:
            returncode, stdout, stderr = rc, out, ""
        return R()


def env_for(board: Board, target="b", version="1.1.0"):
    pn = {"a": (2, 4), "b": (3, 5)}[target]
    return {"RAUC_TARGET_SLOTS": "1 2", "RAUC_MF_VERSION": version, "RAUC_MF_COMPATIBLE": "sheregesh-lab-pi4-dev-1",
            "RAUC_SLOT_CLASS_1": "rootfs", "RAUC_SLOT_NAME_1": f"rootfs.{'ab'.index(target)}",
            "RAUC_SLOT_DEVICE_1": str(board.dev / f"mmcblk0p{pn[1]}"), "RAUC_SLOT_BOOTNAME_1": target.upper(),
            "RAUC_SLOT_CLASS_2": "boot", "RAUC_SLOT_NAME_2": f"boot.{'ab'.index(target)}",
            "RAUC_SLOT_DEVICE_2": str(board.dev / f"mmcblk0p{pn[0]}"), "RAUC_SLOT_BOOTNAME_2": ""}


def write_slot(board: Board, target="b", version="1.1.0"):
    """Как запись RAUC: образы новой версии в разделы неактивного слота."""
    pn = {"a": (2, 4), "b": (3, 5)}[target]
    for part, n in zip(("boot", "system"), pn):
        f = board.dev / f"mmcblk0p{n}"
        raw = image(part, version, PARTS[n][1])
        f.write_bytes(raw + f.read_bytes()[len(raw):])
    board.images_var[version] = {"lib/dpkg/status": f"image {version}\n"}


def hooks(board: Board, env=None, rauc=None, **kw):
    return rauc_hooks.Hooks(config(board, **kw), run=rauc or FakeRauc(board), seeder=TestSeeder(board),
                            probe=FakeProbe(board), env=env if env is not None else env_for(board), sleep=lambda s: None)


def autoboot(board: Board):
    return slots.autoboot_parts((board.bootfs / "autoboot.txt").read_text())


def agent_state(board: Board, state):
    d = board.persistent / "sheregesh-ota"
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({"state": state, "active": "A"}))


def boot_state(board: Board):
    try:
        return json.loads((board.persistent / "sheregesh-rauc/boot-state.json").read_text())
    except FileNotFoundError:
        return {}


def write_conf(board: Board, path: Path, **kw):
    c = config(board, **kw)
    path.write_text(json.dumps({"paths": vars(c.paths), "state_dir": str(c.state_dir),
                                "rauc_system_conf": str(c.rauc_system_conf), "boot_id": str(c.boot_id),
                                "root": str(c.root)}))
    return path
