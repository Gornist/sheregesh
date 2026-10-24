# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""confirm под root на изолированном корне (S3.2-OTA, OTA-04/05/06/10).

pi2, 2026-10-24: агент OTA (root, sheregesh-ota@.service) подтверждал пробную загрузку B; Probe.health звал
sheregesh-health без --link-config, «~» раскрывался в /root, и здоровая плата получала health FAIL
(docs/acceptance/s3.2-ota/step5). Здесь — настоящий код confirm.py и sheregesh.health, настоящий сетевой профиль
(PKI стенда, link.json как его пишет `sheregesh-link configure` от sheregesh-ui: dir — абсолютный путь), HOME
агента — каталог /root изолированного корня. Подменяются только внешние команды: systemctl, findmnt, journalctl,
vcgencmd, перезагрузка; слоты и прошивка — файлы-разделы test_ota.Board.
"""
import contextlib
import functools
import io
import json
import subprocess
import time
import types
from pathlib import Path

import pytest
from deploy_helpers import needs_ssh_keygen
from test_ota import Board, TestSeeder  # noqa: F401 — та же «плата», что в test_ota

from sheregesh import PROTOCOL_VERSION, __version__, health
from sheregesh.link import pki
from sheregesh.update import confirm, ota

REMOTE_ID, CONSOLE_ID = "100000009d497e91", "10000000568152de"
LINK_DIR = "home/sheregesh-ui/.local/share/sheregesh/link"
CFG_DIR = "home/sheregesh-ui/.config/sheregesh"

pytestmark = needs_ssh_keygen                                  # подпись пакета OTA — ssh-keygen, как в test_ota


class Stand:
    """Плата-блок (remote) с настоящим профилем mTLS; внешние команды — подменены, health — настоящий."""

    def __init__(self, tmp, monkeypatch):
        self.b = Board(tmp)
        self.mp = monkeypatch
        r = self.b.root
        (r / "root").mkdir()                                   # домашний каталог root: профиля там нет
        ca = self.b.t / "lab-ca"
        pki.ca_init(ca)
        d = r / LINK_DIR
        cert = pki.ca_sign(ca, pki.keygen("remote", REMOTE_ID, d), "remote", REMOTE_ID)
        pki.install(d, cert, (ca / "ca.crt").read_text(), "remote", REMOTE_ID)
        # как `sheregesh-link configure` от sheregesh-ui: dir = DEFAULT_DIR.expanduser() — абсолютный путь
        pki.configure(r / CFG_DIR / "link.json", "remote", REMOTE_ID, f"console:{CONSOLE_ID}", ["192.168.1.92"],
                      "auto", d)
        (r / "proc").mkdir()
        (r / "proc/uptime").write_text("743.12 1400.00\n")
        self.units = {}                                        # «systemctl show» поверх «всё active»
        self.failed = []
        self.kernel_log = []                                   # строки journalctl -k (ошибки ФС)
        self.health_calls = []
        self.health_exc = None                                 # исключение запуска health (таймаут, нет файла)
        self.link_status(fresh=True)

    # ---------- факты платы ----------
    def link_status(self, fresh=True, connected=True):
        p = self.b.root / "run/sheregesh-remote/link-status.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "time": time.time() - (0 if fresh else 600), "connected": connected, "tls": "TLSv1.3",
            "session": PROTOCOL_VERSION, "stats": {"auth_failures": 0},
            "peer": {"addr": "192.168.1.92", "tls": "TLSv1.3",
                     "hello": {"proto": PROTOCOL_VERSION, "software": __version__}},
            "video": {"port": 7444, "transport": "tls1.3-mutual", "active": True, "frames": 21830,
                      "sender_fps_5s": 30.0}}))

    def new_slot_build(self, version="1.1.0"):
        p = self.b.root / "usr/share/sheregesh/build.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"ota_version": version, "commit": "a7f1b744b830" + "0" * 28}))

    # ---------- внешние команды ----------
    def system(self, cmd, timeout=10):
        """Команды, которые health и confirm запускают на плате (systemctl, journalctl, vcgencmd, findmnt)."""
        if cmd[:3] == ["systemctl", "show", "-p"]:
            kv = {"ActiveState": "active", "SubState": "running", "NRestarts": "0", "Result": "success",
                  **self.units.get(cmd[-1], {})}
            return 0, "\n".join(f"{k}={v}" for k, v in kv.items()), ""
        if cmd[:2] == ["systemctl", "--failed"]:
            return 0, "\n".join(f"{u} loaded failed failed x" for u in self.failed), ""
        if cmd[0] == "journalctl":
            return (0, "\n".join(self.kernel_log), "") if self.kernel_log else (1, "-- No entries --", "")
        if cmd == ["vcgencmd", "get_throttled"]:
            return 0, "throttled=0x0", ""
        if cmd == ["vcgencmd", "measure_temp"]:
            return 0, "temp=48.2'C", ""
        if cmd[0] == "findmnt":
            return 0, {"/persistent": "/dev/mmcblk0p6 rw,noatime,lazytime,errors=remount-ro,commit=60",
                       "/home": "/dev/mmcblk0p6[/home] ro,nosuid,noatime,lazytime",
                       "/etc/ssh": "/dev/mmcblk0p6[/shared/etc/ssh] rw,noatime\n"
                                   "/dev/mmcblk0p6[/shared/etc/ssh] ro,nosuid,noatime"}[cmd[-1]], ""
        return 127, "", f"неизвестная команда {cmd}"

    def run_health(self, argv, home):
        """Настоящий sheregesh.health.main с окружением агента: HOME — /root изолированного корня."""
        self.health_calls.append(list(argv))
        mp = pytest.MonkeyPatch()
        try:
            mp.setenv("HOME", str(home))
            mp.setattr(health, "collect", functools.partial(health.collect, runner=self.system))
            mp.setattr(health, "STATUS_FILES", {k: str(self.b.root / v.lstrip("/"))
                                               for k, v in health.STATUS_FILES.items()})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = health.main(argv)
            return rc, out.getvalue()
        finally:
            mp.undo()

    def runner(self, home):
        def run(cmd, **kw):
            cmd = list(cmd)
            if cmd[0] == "/usr/bin/sheregesh-health":
                if self.health_exc:
                    raise self.health_exc
                rc, out = self.run_health(cmd[1:], home)
                return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")
            if cmd[0] in ("systemctl", "findmnt") and cmd[1] in ("show", "--failed", "-no"):
                rc, out, err = self.system(cmd)
                return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)
            return self.b.run(cmd, **kw)                       # перезагрузка, tryboot — только запись в журнал
        return run

    # ---------- агент ----------
    def agent(self, home=None, probe_cls=confirm.Probe):
        home = home or self.b.root / "root"
        b, run = self.b, self.runner(home)
        cfg = ota.AgentConfig(paths=b.paths(), state_dir=b.persistent / "sheregesh-ota", signers=b.signers,
                              build_json=b.t / "build.json", serial=b.t / "serial", dev_mode=b.etc / "DEV-MODE",
                              reboot_cmd=("reboot",), tryboot_cmd=("reboot", "0 tryboot"), root=b.root,
                              probe=probe_cls(b.root, run))
        return ota.Agent(cfg, run=run, seeder=TestSeeder(b))

    def request(self, action, stream=b"", **kw):
        return self.agent(**kw).handle({"action": action}, io.BytesIO(stream), [].append)

    def to_health_pending(self, install_like_old_agent=False):
        self.b.enable()
        assert self.request("install", self.b.payload("1.1.0"))["result"] == "OK"
        if install_like_old_agent:                             # агент 57e8fe4 (pi2) не снимал отпечатков
            p = self.b.persistent / "sheregesh-ota/state.json"
            st = json.loads(p.read_text())
            st.pop("expect", None)
            p.write_text(json.dumps(st))
        assert self.request("tryboot")["result"] == "OK"
        self.b.firmware_boot(tryboot_flag=True)
        self.agent().on_boot()
        self.new_slot_build("1.1.0")
        assert self.state()["state"] == "HEALTH_PENDING"

    def state(self):
        return json.loads((self.b.persistent / "sheregesh-ota/state.json").read_text())

    def autoboot(self):
        from sheregesh.update import slots
        return slots.autoboot_parts((self.b.bootfs / "autoboot.txt").read_text())


class OldProbe(confirm.Probe):
    """Probe.health до 6353c70 (дословно: git show 6353c70^:software/sheregesh/update/confirm.py)."""

    def health(self, role_name):
        rc, out = self.sh("/usr/bin/sheregesh-health", "--json", *(("--role", role_name) if role_name != "standalone"
                                                                    else ()))
        try:
            return json.loads(out)
        except ValueError:
            return {"verdict": "ERROR"}


@pytest.fixture
def stand(tmp_path, monkeypatch):
    return Stand(tmp_path, monkeypatch)


def _failed(res):
    return [c["id"] for c in res["checks"] if c["mandatory"] and c["status"] != "PASS"]


def _check(res, cid):
    return next(c for c in res["checks"] if c["id"] == cid)


# ---------------------------------------------------------------- OTA-04: ложный FAILED воспроизводится до 6353c70

def test_ota04_old_probe_reproduces_pi2_false_failed_under_root(stand):
    """Код до 6353c70 под root (HOME=/root): здоровая плата → FAILED с теми же деталями, что на pi2 (step5)."""
    stand.to_health_pending(install_like_old_agent=True)       # как на pi2: установка агентом 57e8fe4, expect нет
    res = stand.request("confirm", probe_cls=OldProbe)
    assert res["result"] == "FAILED"
    assert _failed(res) == ["health", "health.network"]        # остальные 17 критериев — PASS, как на плате
    assert _check(res, "health.network")["detail"] == \
        "config.link=FAIL, link.state=FAIL, certs.device=FAIL, certs.ca=нет, certs.key-mode=нет"
    assert "'FAIL': 3" in _check(res, "health")["detail"]
    assert stand.health_calls[-1] == ["--json", "--role", "remote"]
    assert res["state"]["state"] == "TRYBOOT" and res["state"]["reason"] == "health FAIL, повтор"
    assert stand.autoboot()[0] == 2                            # основной слот — прежний A


def test_ota04_old_probe_passes_as_stand_user_why_dry_run_was_green(stand):
    """Тот же код от sheregesh-ui (HOME=/home/sheregesh-ui) — PASS: поэтому сухой прогон шага 4 был зелёным."""
    stand.to_health_pending(install_like_old_agent=True)
    res = stand.request("confirm", probe_cls=OldProbe, home=stand.b.root / "home/sheregesh-ui")
    assert res["result"] == "OK", _failed(res)


def test_ota04_old_confirm_from_git_reproduces(stand):
    """Исходный файл confirm.py из 6353c70^ (если история доступна) — тот же ложный FAILED."""
    r = subprocess.run(["git", "show", "6353c70^:software/sheregesh/update/confirm.py"], capture_output=True,
                       text=True, cwd=Path(__file__).parent)
    if r.returncode:
        pytest.skip("история git с 6353c70 недоступна")
    old = types.ModuleType("confirm_6353c70_parent")
    exec(compile(r.stdout, "confirm@6353c70^", "exec"), old.__dict__)   # noqa: S102 — свой код из истории
    stand.to_health_pending(install_like_old_agent=True)
    res = stand.request("confirm", probe_cls=old.Probe)
    assert res["result"] == "FAILED" and _failed(res) == ["health", "health.network"]


def test_ota04_current_probe_does_not_reproduce(stand):
    """v2 (6353c70): тот же корень, тот же HOME=/root — health PASS, сетевые проверки PASS, слот подтверждён."""
    stand.to_health_pending(install_like_old_agent=True)
    res = stand.request("confirm")
    assert res["result"] == "OK", [(c["id"], c["detail"]) for c in res["checks"] if c["status"] != "PASS"]
    assert stand.health_calls[-1] == ["--json", "--role", "remote", "--link-config",
                                      str(stand.b.root / CFG_DIR / "link.json")]
    assert _check(res, "health.network")["status"] == "PASS"


# ---------------------------------------------------------------- OTA-05/06: исправная система → COMMITTED

def test_ota05_06_healthy_board_confirm_commits_with_real_health_and_fingerprints(stand):
    stand.to_health_pending()
    assert set(stand.state()["expect"]) >= {"role", "machine_id", "device_key", "device_crt", "link_json"}
    n_cmd = len(stand.b.commands)
    res = stand.request("confirm")
    assert res["result"] == "OK", [(c["id"], c["detail"]) for c in res["checks"] if c["status"] != "PASS"]
    ids = {c["id"]: c["status"] for c in res["checks"]}
    assert all(ids[f"same.{k}"] == "PASS" for k in ("role", "machine_id", "device_key", "device_crt", "link_json",
                                                    "ssh_host_key", "authorized_keys"))
    assert ids["health"] == ids["health.network"] == ids["mtls"] == ids["video"] == "PASS"
    st = stand.state()
    assert st["state"] == "COMMITTED" and st["active"] == "B" and st["version_active"] == "1.1.0"
    assert stand.autoboot() == (3, 2)                          # mark-good: основной — B
    assert stand.b.commands[n_cmd:] == []                      # confirm не перезагружает и не делает tryboot


def test_health_report_under_root_has_no_skip_or_fail(stand):
    """Отчёт sheregesh-health с аргументами Probe под HOME=/root — все сетевые проверки реальные (не SKIP)."""
    stand.new_slot_build()
    rc, out = stand.run_health(["--json", "--role", "remote", "--link-config", str(stand.b.root / CFG_DIR /
                                                                               "link.json")], stand.b.root / "root")
    rep = json.loads(out)
    st = {c["id"]: c["status"] for c in rep["checks"]}
    assert rc == 0 and rep["verdict"] == "PASS", rep["checks"]
    for cid in confirm.NET_CHECKS + ("link.versions",):
        assert st[cid] == "PASS", (cid, st)


def test_link_json_dir_is_absolute_so_root_health_finds_keys(stand):
    """Оставшаяся зависимость health от HOME: LinkConfig.load раскрывает «~» в поле dir (или DEFAULT_DIR без dir) —
    под root это /root/... и снова config.link/link.state/certs.device = FAIL. Безопасно, пока профиль пишет
    `sheregesh-link configure` (dir = абсолютный путь пользователя стенда); это и проверяется."""
    raw = json.loads((stand.b.root / CFG_DIR / "link.json").read_text())
    assert "dir" in raw and Path(raw["dir"]).is_absolute() and "~" not in raw["dir"]
    assert Path(raw["dir"]) == stand.b.root / LINK_DIR


# ---------------------------------------------------------------- реальные отказы → не COMMITTED

def _refused(stand, res, gate):
    assert res["result"] == "FAILED" and gate in _failed(res), (_failed(res), res["reason"])
    assert stand.state()["state"] != "COMMITTED" and stand.autoboot()[0] == 2


def test_real_health_fail_is_not_committed_retry_then_rollback(stand):
    stand.to_health_pending()
    stand.link_status(fresh=False)                             # служба блока зависла: состояние канала устарело
    res = stand.request("confirm")
    _refused(stand, res, "health")
    assert "link.state=FAIL" in _check(res, "health.network")["detail"]
    st = stand.state()
    assert st["state"] == "TRYBOOT" and st["boot_attempts"] == 1 and st["target"] == "B"


def test_missing_link_json_fails_with_clear_reason(stand):
    stand.to_health_pending()
    (stand.b.root / CFG_DIR / "link.json").unlink()
    res = stand.request("confirm")
    _refused(stand, res, "health.network")
    assert "same.link_json" in _failed(res)
    assert "config.link=FAIL" in _check(res, "health.network")["detail"]


def test_role_changed_fails_with_clear_reason(stand):
    stand.to_health_pending()
    (stand.b.root / CFG_DIR / "remote.role").rename(stand.b.root / CFG_DIR / "console.role")
    res = stand.request("confirm")
    _refused(stand, res, "same.role")
    assert _check(res, "same.role")["detail"] == "изменилось: было remote, стало console"


def test_kernel_fs_error_seen_by_root_fails(stand):
    """Под root journalctl видит весь журнал ядра (от sheregesh-ui — нет): ошибка ФС — настоящий FAIL."""
    stand.to_health_pending()
    stand.kernel_log = ["EXT4-fs error (device mmcblk0p6): ext4_find_entry: inode #2: comm x: reading directory"]
    _refused(stand, stand.request("confirm"), "health")


def test_health_timeout_changes_nothing(stand):
    """Таймаут запуска health: агент завершается без ответа (P2), но слоты и состояние не меняются, повтор возможен."""
    stand.to_health_pending()
    before = stand.state()
    stand.health_exc = subprocess.TimeoutExpired(["/usr/bin/sheregesh-health"], 60)
    with pytest.raises(subprocess.TimeoutExpired):
        stand.request("confirm")
    assert stand.state() == before and stand.autoboot() == (2, 3)
    stand.health_exc = None
    assert stand.request("confirm")["result"] == "OK"         # тот же HEALTH_PENDING — подтверждение возможно


def test_health_binary_missing_is_failed_answer_not_commit(stand):
    stand.to_health_pending()
    before = stand.state()
    stand.health_exc = FileNotFoundError(2, "No such file", "/usr/bin/sheregesh-health")
    out = io.BytesIO()
    assert ota.serve(stand.agent(), io.BytesIO(b'{"action": "confirm"}\n'), out) == 1
    assert json.loads(out.getvalue().splitlines()[-1])["result"] == "FAILED"
    assert stand.state() == before and stand.autoboot() == (2, 3)


# ---------------------------------------------------------------- OTA-10: повторный confirm идемпотентен

def _repeat_confirm_is_noop(stand):
    state_bytes = (stand.b.persistent / "sheregesh-ota/state.json").read_bytes()
    autoboot_bytes = (stand.b.bootfs / "autoboot.txt").read_bytes()
    n_cmd, n_health = len(stand.b.commands), len(stand.health_calls)
    res = stand.request("confirm")
    assert res["result"] == "REJECTED"
    assert (stand.b.persistent / "sheregesh-ota/state.json").read_bytes() == state_bytes
    assert (stand.b.bootfs / "autoboot.txt").read_bytes() == autoboot_bytes      # нет повторного mark-good/tryboot
    assert len(stand.b.commands) == n_cmd and len(stand.health_calls) == n_health  # нет перезагрузки, нет проверок
    return res


def test_ota10_repeat_confirm_after_success(stand):
    stand.to_health_pending()
    assert stand.request("confirm")["result"] == "OK"
    _repeat_confirm_is_noop(stand)
    _repeat_confirm_is_noop(stand)
    assert stand.state()["state"] == "COMMITTED" and stand.autoboot() == (3, 2)


def test_ota10_repeat_confirm_after_failed(stand):
    stand.to_health_pending()
    stand.link_status(fresh=False)
    assert stand.request("confirm")["result"] == "FAILED"
    stand.link_status(fresh=True)                              # даже если плата «выздоровела» — без новой загрузки нет
    _repeat_confirm_is_noop(stand)
    st = stand.state()
    assert st["state"] == "TRYBOOT" and st["boot_attempts"] == 1 and stand.autoboot() == (2, 3)
