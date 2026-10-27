# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""DEV deploy (S3.1-DEPLOY): формат пакета, подпись, установщик с откатом и launcher — на временных каталогах.

Реальные службы заменены FakeSystemd: «служба» исполняет тот код, который выбрал launcher, так что проверяется
вся цепочка: пакет → подпись → распаковка → current → выбор кода при старте → READY/падение → откат.
"""
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from unittest import mock

import pytest
from deploy_helpers import FakeClock, FakeSystemd, app_files, keypair, make_bundle, needs_ssh_keygen, signers_file

from sheregesh.deploy import bundle, installer, launcher, sig

# ---------------------------------------------------------------- формат пакета


def test_bundle_is_deterministic_and_roundtrips():
    a, b = make_bundle(), make_bundle()
    assert a == b
    manifest, files = bundle.parse_bundle(a)
    assert manifest["id"] == "0.1.0.dev0+gabc1234" and manifest["compatible"] == bundle.COMPATIBLE
    assert set(files) == set(app_files()) and files["tools/probe/s2probe.py"].startswith(b"#!")
    assert manifest["files"]["tools/probe/s2probe.py"]["mode"] == 0o755


def _tar(members, manifest=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        if manifest is not None:
            raw = json.dumps(manifest).encode()
            ti = tarfile.TarInfo("manifest.json")
            ti.size = len(raw)
            tar.addfile(ti, io.BytesIO(raw))
        for ti, data in members:
            tar.addfile(ti, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def _good_manifest():
    return bundle.parse_bundle(make_bundle())[0]


@pytest.mark.parametrize("case", ["traversal", "symlink", "absolute", "extra", "missing", "hash", "pycache",
                                  "manifest_not_first", "foreign_prefix", "incompatible", "garbage"])
def test_hostile_bundles_are_rejected(case):
    m = _good_manifest()
    files = {p: d for p, (d, _) in app_files().items()}

    def reg(name, data):
        ti = tarfile.TarInfo(name)
        ti.size = len(data)
        return ti, data

    members = [reg("files/" + p, d) for p, d in files.items()]
    if case == "traversal":
        members.append(reg("files/software/sheregesh/../../../etc/passwd", b"x"))
    elif case == "symlink":
        ti = tarfile.TarInfo("files/software/sheregesh/link.py")
        ti.type, ti.linkname = tarfile.SYMTYPE, "/etc/shadow"
        members.append((ti, None))
    elif case == "absolute":
        members.append(reg("/etc/cron.d/x", b"x"))
    elif case == "extra":
        members.append(reg("files/software/sheregesh/evil.py", b"import os"))
    elif case == "missing":
        members = members[1:]
    elif case == "hash":
        members[0] = reg(members[0][0].name, b"tampered = True\n")
    elif case == "pycache":
        members.append(reg("files/software/sheregesh/__pycache__/x.cpython-313.pyc", b"x"))
    elif case == "foreign_prefix":
        m["files"]["usr/bin/sudo"] = {"sha256": "0" * 64, "size": 1, "mode": 0o755}
    elif case == "incompatible":
        m["compatible"] = "sheregesh-lab-pi4-app-0"
    if case == "manifest_not_first":
        data = _tar(members + [reg("manifest.json", json.dumps(m).encode())])
    elif case == "garbage":
        data = b"\xfd7zXZ\x00garbage"
    else:
        data = _tar(members, m)
    with pytest.raises(bundle.BundleError):
        bundle.parse_bundle(data)


def test_release_id_sanitizes_and_rejects():
    assert bundle.release_id("0.1.0~dev0+g52c6438") == "0.1.0~dev0+g52c6438"
    assert bundle.release_id("1.0 beta/x") == "1.0_beta_x"
    with pytest.raises(bundle.BundleError):
        bundle.release_id("../x")


def test_python_paths_agree_with_launcher_and_build_deb():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/image"))
    import build_deb
    assert launcher.PYTHON_PATHS == bundle.PYTHON_PATHS
    assert build_deb.PYTHONPATH == ":".join(f"/usr/lib/sheregesh/{p}" for p in bundle.PYTHON_PATHS)
    assert all(any(src.startswith(pre.rstrip("/")) or pre.startswith(src.rstrip("/") + "/") for pre in
                   bundle.ALLOWED_PREFIXES) for src in build_deb.SOURCES)


# ---------------------------------------------------------------- подпись


@needs_ssh_keygen
def test_signature_accepts_owner_and_rejects_foreign_key_wrong_namespace_and_tampering(tmp_path):
    owner, other = keypair(tmp_path, "owner"), keypair(tmp_path, "other")
    signers = signers_file(tmp_path, owner)
    data = make_bundle()
    good = sig.sign(data, owner, sig.NS_APP)
    assert "Good" in sig.verify(data, good, signers, sig.NS_APP)
    for d, s, ns in ((data, sig.sign(data, other, sig.NS_APP), sig.NS_APP),       # посторонний ключ
                     (data, sig.sign(data, owner, sig.NS_OTA), sig.NS_APP),       # подпись для другого назначения
                     (data + b"x", good, sig.NS_APP),                              # изменённое содержимое
                     (data, b"", sig.NS_APP)):                                     # без подписи
        with pytest.raises(sig.SignatureError):
            sig.verify(d, s, signers, ns)
    with pytest.raises(sig.SignatureError):
        sig.verify(data, good, tmp_path / "absent", sig.NS_APP)


def test_allowed_signers_line_refuses_private_key():
    assert sig.allowed_signers_line("ssh-ed25519 AAAAC3Nz test").startswith('sheregesh-deploy namespaces="')
    with pytest.raises(sig.SignatureError):
        sig.allowed_signers_line("-----BEGIN OPENSSH " + "PRIVATE KEY-----\nabc")     # склейка: не шаблон секрета для scan.py


# ---------------------------------------------------------------- установщик


class Board:
    """Плата в каталоге: заводской код, каталог выпусков, входящие, роль; службы — FakeSystemd."""

    def __init__(self, tmp, role=None, skip=()):
        self.tmp = Path(tmp)
        self.factory = self.tmp / "usr/lib/sheregesh"
        for path, (data, _) in app_files("ok", "factory").items():
            (self.factory / path).parent.mkdir(parents=True, exist_ok=True)
            (self.factory / path).write_bytes(data)
        self.key = keypair(self.tmp, "owner")
        self.signers = signers_file(self.tmp, self.key)
        self.cfgdir = self.tmp / "home/.config/sheregesh"
        self.cfgdir.mkdir(parents=True)
        if role:
            (self.cfgdir / f"{role}.role").write_text("")
        self.clock = FakeClock()
        self.settings = installer.Settings(
            deploy_dir=self.tmp / "var/lib/sheregesh-deploy", incoming=self.tmp / "home/.cache/incoming",
            signers=self.signers, config_dir=self.cfgdir, owner=_me(), require_root=False,
            boot_id="boot-1")
        self.svc = FakeSystemd(self.settings.deploy_dir, self.factory, self.clock, skip)
        self.log = []

    def push(self, data, key=None, namespace=sig.NS_APP):
        inc = self.settings.incoming
        inc.mkdir(parents=True, exist_ok=True)
        (inc / "bundle.tar.xz").write_bytes(data)
        (inc / "bundle.tar.xz.sig").write_bytes(sig.sign(data, key or self.key, namespace))

    def run(self, action):
        inst = installer.Installer(self.settings, self.svc, clock=self.clock, sleep=self.clock.sleep, out=self.log.append)
        return inst.run(action), json.loads((self.settings.deploy_dir / "state.json").read_text())

    def code(self):
        return launcher.select(str(self.settings.deploy_dir), str(self.factory), current_boot="boot-1",
                               require_root=False)


def _me():
    import pwd
    return pwd.getpwuid(os.getuid()).pw_name


@needs_ssh_keygen
def test_install_switches_atomically_and_restarts_only_role_service(tmp_path):
    b = Board(tmp_path, role="console")
    b.push(make_bundle("0.1.0+g1"))
    rc, st = b.run("install")
    assert rc == 0 and st["last"]["result"] == "OK" and st["current"] == "0.1.0+g1" and st.get("previous") is None
    assert os.readlink(b.settings.deploy_dir / "current") == "releases/0.1.0+g1"
    assert b.svc.restarts == ["sheregesh-video-rx", "sheregesh-console"]  # только службы роли пульта
    assert b.code()[1] == "0.1.0+g1" and "pending" not in st
    assert not list(b.settings.incoming.iterdir())                         # входящие убраны
    rel = b.settings.deploy_dir / "releases/0.1.0+g1"
    assert json.loads((rel / "RELEASE.json").read_text())["commit"] == "a" * 40


@needs_ssh_keygen
@pytest.mark.parametrize("role, units", [("remote", ["sheregesh-video-tx", "sheregesh-remote-net"]),
                                         (None, ["sheregesh-remote-emu", "sheregesh-console"])])
def test_role_selects_services(tmp_path, role, units):
    b = Board(tmp_path, role=role)
    b.push(make_bundle())
    assert b.run("install")[0] == 0 and b.svc.restarts == units


@needs_ssh_keygen
@pytest.mark.parametrize("behaviour", ["nostart", "crash", "hang"])
def test_failed_service_rolls_back_to_previous_release(tmp_path, behaviour):
    b = Board(tmp_path, role="console")
    b.push(make_bundle("0.1.0+good"))
    assert b.run("install")[0] == 0
    b.push(make_bundle("0.1.0+bad", behaviour=behaviour))
    rc, st = b.run("install")
    assert rc == 1 and st["last"]["result"] == "ROLLED_BACK" and st["last"]["rejected"] == "0.1.0+bad"
    assert st["current"] == "0.1.0+good" and b.code()[1] == "0.1.0+good"
    assert b.svc.restarts == ["sheregesh-video-rx", "sheregesh-console"] * 3                    # установка, плохой, возврат
    assert b.svc.units["sheregesh-console"]["release"] == "0.1.0+good"


@needs_ssh_keygen
def test_first_bad_release_falls_back_to_factory(tmp_path):
    b = Board(tmp_path)
    b.push(make_bundle("0.1.0+bad", behaviour="nostart"))
    rc, st = b.run("install")
    assert rc == 1 and st["current"] is None and b.code()[1] is None
    assert not (b.settings.deploy_dir / "current").is_symlink()


@needs_ssh_keygen
def test_rollback_and_factory_actions(tmp_path):
    b = Board(tmp_path, role="console")
    for v in ("0.1.0+a", "0.1.0+b"):
        b.push(make_bundle(v))
        assert b.run("install")[0] == 0
    rc, st = b.run("rollback")
    assert rc == 0 and st["current"] == "0.1.0+a" and st["previous"] == "0.1.0+b"
    rc, st = b.run("rollback")                                            # повторный откат возвращает обратно
    assert rc == 0 and st["current"] == "0.1.0+b"
    rc, st = b.run("factory")
    assert rc == 0 and st["current"] is None and b.code()[1] is None and st["previous"] == "0.1.0+b"
    assert b.run("factory")[1]["last"]["reason"] == "уже заводской код"


@needs_ssh_keygen
def test_rollback_without_previous_is_refused(tmp_path):
    b = Board(tmp_path)
    rc, st = b.run("rollback")
    assert rc == 2 and st["last"]["result"] == "REJECTED"


@needs_ssh_keygen
@pytest.mark.parametrize("attack", ["foreign_key", "wrong_namespace", "tampered", "no_sig", "symlink",
                                    "too_big", "incompatible"])
def test_rejections_change_nothing(tmp_path, attack):
    b = Board(tmp_path, role="console")
    b.push(make_bundle("0.1.0+good"))
    assert b.run("install")[0] == 0
    restarts = list(b.svc.restarts)
    data = make_bundle("0.1.0+evil")
    inc = b.settings.incoming
    if attack == "foreign_key":
        b.push(data, key=keypair(tmp_path, "intruder"))
    elif attack == "wrong_namespace":
        b.push(data, namespace=sig.NS_OTA)
    elif attack == "tampered":
        b.push(data)
        (inc / "bundle.tar.xz").write_bytes(data[:-8] + b"\0" * 8)
    elif attack == "no_sig":
        b.push(data)
        (inc / "bundle.tar.xz.sig").unlink()
    elif attack == "symlink":
        (tmp_path / "elsewhere.tar.xz").write_bytes(data)
        b.push(data)
        (inc / "bundle.tar.xz").unlink()
        (inc / "bundle.tar.xz").symlink_to(tmp_path / "elsewhere.tar.xz")
    elif attack == "too_big":
        b.push(data)
        (inc / "bundle.tar.xz").write_bytes(b"\0" * (bundle.MAX_BUNDLE + 1))
    elif attack == "incompatible":                   # подписан верно, но собран для другой системной части
        with mock.patch.object(bundle, "COMPATIBLE", "sheregesh-lab-pi4-app-999"):
            raw = bundle.build_bundle(app_files(), "0.1.0+evil", "b" * 40, 1)
        b.push(raw)
    rc, st = b.run("install")
    assert rc == 2 and st["last"]["result"] == "REJECTED", st["last"]
    assert st["current"] == "0.1.0+good" and b.svc.restarts == restarts
    assert not (b.settings.deploy_dir / "releases/0.1.0+evil").exists()
    assert not [p for p in inc.iterdir() if p.name.startswith("bundle")]


@needs_ssh_keygen
def test_reinstall_same_release_is_idempotent_and_corruption_is_repaired(tmp_path):
    b = Board(tmp_path, role="console")
    b.push(make_bundle("0.1.0+a"))
    assert b.run("install")[0] == 0
    f = b.settings.deploy_dir / "releases/0.1.0+a/software/sheregesh/__init__.py"
    f.write_text("corrupted = True\n")
    assert b.code()[1] is None                                             # битый выпуск → заводской код
    b.push(make_bundle("0.1.0+a"))
    rc, st = b.run("install")
    assert rc == 0 and b.code()[1] == "0.1.0+a" and "corrupted" not in f.read_text()


@needs_ssh_keygen
def test_unconfirmed_switch_is_undone_after_reboot_and_on_next_run(tmp_path):
    b = Board(tmp_path, role="console")
    b.push(make_bundle("0.1.0+a"))
    assert b.run("install")[0] == 0
    # Имитация: установщик переключил current на новый выпуск и пропал питание до подтверждения.
    b.push(make_bundle("0.1.0+b"))
    inst = installer.Installer(b.settings, b.svc, clock=b.clock, sleep=b.clock.sleep, out=b.log.append)
    manifest, files = bundle.parse_bundle(make_bundle("0.1.0+b"))
    inst._materialize(manifest, files)
    st = inst.load()
    st["pending"] = {"release": "0.1.0+b", "previous": "0.1.0+a", "boot_id": "boot-1"}
    inst.save(st)
    inst._set_current("0.1.0+b")
    dd = str(b.settings.deploy_dir)
    assert launcher.select(dd, str(b.factory), "boot-1", False)[1] == "0.1.0+b"   # до перезагрузки — новый
    assert launcher.select(dd, str(b.factory), "boot-2", False)[1] == "0.1.0+a"   # после — прежний
    for p in b.settings.incoming.iterdir():
        p.unlink()
    rc, st = b.run("rollback")                     # следующий запуск установщика сначала отменяет pending
    assert "незавершённое переключение на 0.1.0+b отменено" in b.log


@needs_ssh_keygen
def test_old_releases_are_garbage_collected(tmp_path):
    b = Board(tmp_path, role="console")
    for i in range(6):
        b.push(make_bundle(f"0.1.0+r{i}"))
        assert b.run("install")[0] == 0
        os.utime(b.settings.deploy_dir / f"releases/0.1.0+r{i}", (1000 + i, 1000 + i))
    names = sorted(p.name for p in (b.settings.deploy_dir / "releases").iterdir())
    assert len(names) <= installer.KEEP_RELEASES + 1 and "0.1.0+r5" in names and "0.1.0+r4" in names


def test_skipped_service_is_not_judged(tmp_path):
    if not __import__("shutil").which("ssh-keygen"):
        pytest.skip("нет ssh-keygen")
    b = Board(tmp_path, role="remote", skip={"sheregesh-remote-net"})
    b.push(make_bundle())
    rc, st = b.run("install")
    assert rc == 0 and "не оценивается" in st["last"]["units"]["sheregesh-remote-net"]


def test_unknown_action_and_non_root_cli():
    assert installer.main(["format-disk"]) == installer.EX_USAGE        # не входит в SuccessExitStatus юнита
    assert installer.main(["install", "extra"]) == installer.EX_USAGE
    if os.geteuid() != 0:
        assert installer.main(["install"]) == installer.EX_USAGE


# ---------------------------------------------------------------- launcher


@needs_ssh_keygen
def test_launcher_rejects_tampered_or_foreign_files(tmp_path):
    b = Board(tmp_path, role="console")
    b.push(make_bundle("0.1.0+a"))
    assert b.run("install")[0] == 0
    rel = b.settings.deploy_dir / "releases/0.1.0+a"
    assert b.code()[1] == "0.1.0+a"
    (rel / "software/sheregesh/json.py").write_text("evil = 1\n")          # подложенный модуль
    root, rid, why = b.code()
    assert rid is None and root == str(b.factory) and "посторонний файл" in why
    (rel / "software/sheregesh/json.py").unlink()
    (rel / "software/sheregesh/__pycache__").mkdir()
    (rel / "software/sheregesh/__pycache__/x.cpython-313.pyc").write_bytes(b"pyc")
    root, rid, why = b.code()                                             # непроверенный байт-код — отказ
    assert rid is None and "посторонний файл" in why
    shutil.rmtree(rel / "software/sheregesh/__pycache__")
    assert b.code()[1] == "0.1.0+a"
    os.symlink("/etc", rel / "software/sheregesh/etc")
    assert b.code()[1] is None


def test_launcher_requires_root_owner_on_device(tmp_path):
    rel = tmp_path / "releases/x"
    rel.mkdir(parents=True)
    (rel / "RELEASE.json").write_text(json.dumps({"kind": "sheregesh-app-release", "files": {}}))
    os.symlink("releases/x", tmp_path / "current")
    root, rid, why = launcher.select(str(tmp_path), "/usr/lib/sheregesh", "b", require_root=os.geteuid() != 0)
    assert rid is None and root == "/usr/lib/sheregesh"


def test_launcher_exec_uses_selected_root_and_refuses_system_modules(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "select", lambda: ("/var/lib/sheregesh-deploy/releases/x", "x", "выпуск x"))
    monkeypatch.setattr(launcher.os, "execve", lambda *a: calls.append(a))
    launcher.main(["sheregesh_console", "--peer", "127.0.0.1:7700"])
    path, argv, env = calls[0]
    assert path == "/usr/bin/python3" and argv == ["/usr/bin/python3", "-s", "-m", "sheregesh_console", "--peer",
                                                   "127.0.0.1:7700"]
    assert env["PYTHONPATH"].split(":")[0] == "/var/lib/sheregesh-deploy/releases/x/software"
    assert env["SHEREGESH_RELEASE"] == "x"
    for mod in ("sheregesh.deploy.installer", "sheregesh.health", "os"):
        with pytest.raises(SystemExit):
            launcher.main([mod])



def test_launcher_runs_video_services(tmp_path):
    """Службы видео ADR-0016 запускаются через выпуск, как пульт и блок (обёртки build_deb ведут через launcher)."""
    for module in ("sheregesh.videosvc.tx", "sheregesh.videosvc.rx"):
        argv, env = launcher.exec_args(module, str(tmp_path), ["--runtime-dir", "/run/x"])
        assert argv[-3:] == [module, "--runtime-dir", "/run/x"] and env["SHEREGESH_CODE_ROOT"] == str(tmp_path)

def test_launcher_without_release_uses_factory(tmp_path):
    assert launcher.select(str(tmp_path), "/usr/lib/sheregesh", "b")[0] == "/usr/lib/sheregesh"
    os.symlink("/etc", tmp_path / "current")
    root, rid, why = launcher.select(str(tmp_path), "/usr/lib/sheregesh", "b")
    assert rid is None and "releases/" in why


# ---------------------------------------------------------------- аудит привилегий (S3.1-DEPLOY)


@needs_ssh_keygen
def test_installer_never_compiles_or_runs_release_code_as_root(tmp_path, monkeypatch):
    """Установщик (root на плате) запускает только ssh-keygen; ни python, ни compileall по выпуску. Код выпуска с
    побочным эффектом при импорте/компиляции не срабатывает при установке, байт-кода в выпуске нет."""
    b = Board(tmp_path, role="console")
    marker = tmp_path / "pwned"
    files = app_files("ok", "0.1.0+evil")
    files["software/sheregesh/deploy_evil.py"] = (f"open({str(marker)!r}, 'w').write('x')\n".encode(), 0o644)
    b.push(bundle.build_bundle(files, "0.1.0+evil", "a" * 40, 1790000000))
    calls = []
    real = subprocess.run

    def spy(cmd, *a, **k):
        calls.append([str(x) for x in cmd])
        return real(cmd, *a, **k)
    monkeypatch.setattr(subprocess, "run", spy)
    assert b.run("install")[0] == 0
    assert not marker.exists()
    progs = {Path(c[0]).name for c in calls}
    assert progs <= {"ssh-keygen"}, calls                                  # FakeSystemd вместо systemctl
    assert not any("compileall" in " ".join(c) or "py_compile" in " ".join(c) for c in calls)
    rel = b.settings.deploy_dir / "releases/0.1.0+evil"
    assert not list(rel.rglob("*.pyc")) and not list(rel.rglob("__pycache__"))


@needs_ssh_keygen
def test_incoming_symlink_is_refused(tmp_path):
    """Каталог входящих — ссылка (например, на чужой каталог): отказ без чтения и удаления по ссылке."""
    b = Board(tmp_path, role="console")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    data = make_bundle("0.1.0+s")
    (elsewhere / "bundle.tar.xz").write_bytes(data)
    (elsewhere / "bundle.tar.xz.sig").write_bytes(sig.sign(data, b.key, sig.NS_APP))
    b.settings.incoming.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(elsewhere, b.settings.incoming)
    rc, st = b.run("install")
    assert rc == 2 and st["last"]["result"] == "REJECTED" and "ссылка" in st["last"]["reason"]
    assert (elsewhere / "bundle.tar.xz").exists()                         # по ссылке ничего не удалено


@needs_ssh_keygen
def test_release_files_get_commit_time(tmp_path):
    """Все файлы выпуска — с временем коммита: .ts не «новее» .qm из-за порядка записи (найдено на pi1)."""
    b = Board(tmp_path, role="console")
    files = app_files("ok", "0.1.0+t")
    files["software/console/sheregesh_console/i18n/sheregesh_ru.qm"] = (b"qm", 0o644)
    files["software/console/sheregesh_console/i18n/sheregesh_ru.ts"] = (b"<TS/>", 0o644)
    b.push(bundle.build_bundle(files, "0.1.0+t", "a" * 40, 1790000000))
    assert b.run("install")[0] == 0
    rel = b.settings.deploy_dir / "releases/0.1.0+t"
    stamps = {os.stat(rel / p).st_mtime for p in files}
    assert stamps == {1790000000}


@needs_ssh_keygen
@pytest.mark.parametrize('role, video, client', [
    ('console', 'sheregesh-video-rx', 'sheregesh-console'),
    ('remote', 'sheregesh-video-tx', 'sheregesh-remote-net'),
])
def test_deploy_restarts_video_before_client_and_rolls_back_video_failure(tmp_path, role, video, client):
    b = Board(tmp_path, role=role)
    b.push(make_bundle('0.1.0+good'))
    assert b.run('install')[0] == 0
    assert b.svc.restarts == [video, client]
    restart = b.svc.restart

    def fail_new_video(unit, timeout):
        result = restart(unit, timeout)
        if unit == video and b.code()[1] == '0.1.0+bad':
            b.svc.units[unit].update(ActiveState='failed', SubState='failed')
            return False, 'video failed'
        return result

    b.svc.restart = fail_new_video
    b.push(make_bundle('0.1.0+bad'))
    rc, st = b.run('install')
    assert rc == 1 and st['last']['result'] == 'ROLLED_BACK'
    assert st['current'] == '0.1.0+good'
    assert b.svc.units[video]['release'] == b.svc.units[client]['release'] == '0.1.0+good'
    assert video in st['last']['units'] and video in st['last']['rollback_units']


@needs_ssh_keygen
def test_deploy_old_image_skips_only_missing_video_unit(tmp_path):
    b = Board(tmp_path, role='console')
    show = b.svc.show
    b.svc.show = lambda unit: {'LoadState': 'not-found'} if unit == 'sheregesh-video-rx' else show(unit)
    b.push(make_bundle())
    rc, st = b.run('install')
    assert rc == 0 and b.svc.restarts == ['sheregesh-console']
    assert 'sheregesh-video-rx' in st['last']['units']


@needs_ssh_keygen
def test_deploy_broken_video_unit_is_not_treated_as_old_image(tmp_path):
    b = Board(tmp_path, role='console')
    show = b.svc.show
    b.svc.show = lambda unit: {'LoadState': 'error', 'ActiveState': 'inactive'} if unit == 'sheregesh-video-rx' else show(unit)
    b.push(make_bundle())
    rc, st = b.run('install')
    assert rc == 1 and st['last']['result'] == 'BROKEN'
