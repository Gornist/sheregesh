# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Аппаратная приёмка DEV deploy на одной плате (S3.1-DEPLOY). Запуск с машины владельца по SSH.

  python3 tools/probe/check_pi_deploy.py --board pi1 --host 192.168.1.91 --out DIR [--only NAME,...]

Сценарии (DIR/<сценарий>.json, итог DIR/summary.json; вердикты PASS | FAIL | SKIP | ERROR):
  image            версия пакета, build.json, ключ подписи в образе = ~/.ssh/sheregesh-deploy.pub и ≠ ключ входа,
                   сокет агента OTA, установщик на заводском коде; аппаратный watchdog — только сведения
  polkit           настоящий polkitd: start install/rollback/factory разрешён (установщик выполняется от root),
                   запрещены другие экземпляры и глаголы, transient-юниты, set-environment, daemon-reload, ssh
  tamper           sheregesh-ui не может изменить выпуски, current, state.json, ключ подписи
  app-stop | app-kill | app-restart   штатная остановка, SIGKILL приложения, перезапуск через systemd
  watchdog-freeze  SIGSTOP приложения → WatchdogSec → перезапуск; не остаётся процессов старой группы, tty1 свободен
  relay-kill-frozen  приложение заморожено, SIGKILL ретранслятора → не остаётся ни cage, ни приложения (ac64aad)
  deploy-good      shdeploy deploy (HEAD) → OK, служба работает из выпуска (SHEREGESH_RELEASE в окружении)
  deploy-bad-start выпуск, падающий при старте (временный коммит, не публикуется) → ROLLED_BACK, прежний выпуск
  deploy-crash-window  выпуск, падающий через 15 с после READY → ROLLED_BACK
  rollback | factory  ручной возврат и заводской код
  foreign-sig      пакет, подписанный посторонним ключом → REJECTED, current и службы не тронуты
  tampered         подписанный пакет, изменённый после подписи → REJECTED, current и службы не тронуты
  login-key-sig    пакет, подписанный ключом SSH-входа (~/.ssh/id_ed25519) → REJECTED: вход ≠ право установки
  no-bytecode      в установленных выпусках нет __pycache__/.pyc (от root ничего не компилировалось), владелец root
  reboot           штатная перезагрузка → SSH, служба active, тот же выпуск (новый boot_id)
  corrupt          повреждение выпуска (посторонний .pyc/файл) на плате требует root — SKIP (защита — tamper;
                   отказ launcher и переход на заводской код — контейнер systemd 257 и тесты launcher)
Во всех сценариях с отказами параллельно каждые 2 с открывается НОВОЕ соединение SSH (systemctl is-active ssh):
любой отказ — FAIL. Только синтетические эмуляторы; удалённых команд через лабораторный канал нет.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHDEPLOY = [sys.executable, str(ROOT / "tools/deploy/shdeploy.py")]
STATE = "/var/lib/sheregesh-deploy/state.json"
VERDICTS = ("PASS", "FAIL", "SKIP", "ERROR")
DENIED = ("Access denied", "Interactive authentication required", "Permission denied", "not allowed",
          "Operation not permitted", "Read-only file system", "already loaded", "fragment")


def ssh(host, cmd, timeout=120, data=None):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ControlMaster=no",
                        "-o", "ControlPath=none", f"sheregesh-ui@{host}", cmd],
                       input=data, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


class SshProbe:
    """Новое соединение SSH каждые 2 с во время сценария — доступ не должен пропадать."""

    def __init__(self, host):
        self.host, self.ok, self.fail, self.stop_ev = host, 0, [], threading.Event()
        self.t = threading.Thread(target=self.loop, daemon=True)

    def loop(self):
        while not self.stop_ev.is_set():
            t0 = time.monotonic()
            try:
                rc, out, err = ssh(self.host, "systemctl is-active ssh", timeout=15)
            except subprocess.TimeoutExpired:
                rc, out, err = -1, "", "timeout"
            if rc == 0 and out == "active":
                self.ok += 1
            else:
                self.fail.append({"t": time.strftime("%H:%M:%S"), "rc": rc, "out": out, "err": err[-120:]})
            self.stop_ev.wait(max(0.0, 2.0 - (time.monotonic() - t0)))

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *exc):
        self.stop_ev.set()
        self.t.join(20)

    def result(self):
        return {"ssh_probes_ok": self.ok, "ssh_probes_failed": self.fail}


def svc(host, unit="sheregesh-console"):
    _, out, _ = ssh(host, f"systemctl show {unit} -p ActiveState,SubState,NRestarts,MainPID,Result,StatusText")
    return dict(x.split("=", 1) for x in out.splitlines() if "=" in x)


def wait_active(host, unit="sheregesh-console", timeout=120):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if svc(host, unit).get("ActiveState") == "active":
            return round(time.monotonic() - t0, 1)
        time.sleep(1)
    return None


def state(host):
    _, out, _ = ssh(host, f"cat {STATE} 2>/dev/null")
    try:
        return json.loads(out)
    except ValueError:
        return {}


def app_release(host):
    """Выпуск, из которого работает приложение пульта: SHEREGESH_RELEASE в окружении процесса (тот же пользователь)."""
    _, out, _ = ssh(host, "p=$(pgrep -f '^/usr/bin/python3 -s -m sheregesh_console' | head -1); "
                          "[ -n \"$p\" ] && tr '\\0' '\\n' < /proc/$p/environ | grep -E '^SHEREGESH_(RELEASE|CODE_ROOT)='")
    return dict(x.split("=", 1) for x in out.splitlines() if "=" in x)


def journal(host, since, unit="sheregesh-console", pattern="."):
    _, out, _ = ssh(host, f"journalctl -u {unit} --since @{int(since)} -o cat --no-pager | grep -E '{pattern}' | tail -8")
    return out.splitlines()


# ---------------------------------------------------------------- сценарии
def sc_image(a):
    h = a.host
    _, ver, _ = ssh(h, "dpkg-query -W -f='${Version}' sheregesh; echo; cat /usr/share/sheregesh/build.json")
    _, signers, _ = ssh(h, "cat /etc/sheregesh/deploy/allowed_signers")
    _, login, _ = ssh(h, "cat ~/.ssh/authorized_keys")
    _, sock, _ = ssh(h, "systemctl is-active sheregesh-ota.socket sheregesh-ota-boot; sheregesh-deployd status")
    _, wd, _ = ssh(h, "systemctl show -p RuntimeWatchdogUSec,RebootWatchdogUSec; "
                      "cat /sys/class/watchdog/watchdog0/identity /sys/class/watchdog/watchdog0/state 2>/dev/null")
    deploy_pub = Path("~/.ssh/sheregesh-deploy.pub").expanduser().read_text().split()
    login_body = {ln.split()[1] for ln in login.splitlines() if len(ln.split()) > 1}
    signer_body = signers.split()[2] if len(signers.split()) > 2 else ""
    st = json.loads(sock[sock.index("{"):]) if "{" in sock else {}
    ok = (signer_body == deploy_pub[1] and signer_body not in login_body and "active" in sock.splitlines()[0]
          and st.get("code_root") == "/usr/lib/sheregesh")
    return {"package_and_build": ver.splitlines(), "signer_matches_mac_deploy_key": signer_body == deploy_pub[1],
            "signer_differs_from_login": signer_body not in login_body, "ota_and_deployd": sock.splitlines()[:2],
            "deployd_status": st, "hardware_watchdog_info": wd.splitlines(), "verdict": "PASS" if ok else "FAIL"}


NEGATIVE = {   # команда от sheregesh-ui → должна быть отвергнута
    "start other instance": "systemctl start sheregesh-deploy@shell.service",
    "stop install": "systemctl stop sheregesh-deploy@install.service",
    "restart install": "systemctl restart sheregesh-deploy@install.service",
    "kill install": "systemctl kill sheregesh-deploy@install.service",
    "transient with installer name": "systemd-run --unit=sheregesh-deploy@install --wait /usr/bin/id",
    "transient with console name": "systemd-run --unit=sheregesh-console-x -p User=root --wait /usr/bin/id",
    "transient arbitrary": "systemd-run --wait /usr/bin/id",
    "set-environment": "systemctl set-environment SHEREGESH_EVIL=1",
    "daemon-reload": "systemctl daemon-reload",
    "enable unit": "systemctl enable sheregesh-deploy@install.service",
    "mask unit": "systemctl mask sheregesh-deploy@install.service",
    "start ota agent": "systemctl start sheregesh-ota@x.service",
    "stop ssh": "systemctl stop ssh.service",
    "restart ssh": "systemctl restart ssh.service",
}


def sc_polkit(a):
    h = a.host
    before = state(h)
    ssh(h, "rm -f ~/.cache/sheregesh-deploy/incoming/bundle.*")
    rc, out, err = ssh(h, "systemctl start sheregesh-deploy@install.service; echo rc=$?")
    after = state(h)
    positive = {"cmd": "systemctl start sheregesh-deploy@install.service (без пакета)", "out": out, "err": err[-200:],
                "installer_ran": after.get("history", []) != before.get("history", []),
                "last": after.get("last")}
    pos_ok = positive["installer_ran"] and (after.get("last") or {}).get("result") == "REJECTED"
    neg = {}
    for name, cmd in NEGATIVE.items():
        rc, out, err = ssh(h, cmd + " 2>&1; echo rc=$?", timeout=60)
        code = out.rsplit("rc=", 1)[-1] if "rc=" in out else "?"
        text = out.rsplit("rc=", 1)[0]
        neg[name] = {"cmd": cmd, "rc": code, "output": text[-200:],
                     "denied": code not in ("0", "?") and any(d.lower() in text.lower() for d in DENIED)}
    ssh_ok = ssh(h, "systemctl is-active ssh")[1] == "active"
    bad = [n for n, v in neg.items() if not v["denied"]]
    return {"positive": positive, "negative": neg, "not_denied": bad, "ssh_active_after": ssh_ok,
            "verdict": "PASS" if pos_ok and not bad and ssh_ok else "FAIL"}


def sc_tamper(a):
    h = a.host
    cmds = {"write releases": "touch /var/lib/sheregesh-deploy/releases/x",
            "replace current": "ln -sfn /tmp /var/lib/sheregesh-deploy/current",
            "edit state": "echo {} > /var/lib/sheregesh-deploy/state.json",
            "write release file": "f=$(ls -d /var/lib/sheregesh-deploy/releases/*/software/sheregesh 2>/dev/null | head -1); "
                                  "[ -n \"$f\" ] && echo x >> $f/__init__.py || touch /var/lib/sheregesh-deploy/x",
            "edit allowed_signers": "echo x >> /etc/sheregesh/deploy/allowed_signers",
            "write /usr/lib/sheregesh": "touch /usr/lib/sheregesh/software/x"}
    res = {}
    for name, cmd in cmds.items():
        rc, out, err = ssh(h, f"({cmd}) 2>&1; echo rc=$?")
        code = out.rsplit("rc=", 1)[-1]
        res[name] = {"rc": code, "output": out.rsplit("rc=", 1)[0][-160:], "denied": code != "0"}
    bad = [n for n, v in res.items() if not v["denied"]]
    return {"attempts": res, "not_denied": bad, "verdict": "PASS" if not bad else "FAIL"}


def _app_failure(a, action):
    h = a.host
    s0, t0 = svc(h), time.time()
    with SshProbe(h) as p:
        if action == "stop":
            ssh(h, "systemctl stop sheregesh-console")
            time.sleep(3)
            stopped = svc(h).get("ActiveState")
            ssh(h, "systemctl start sheregesh-console")
        elif action == "kill":
            ssh(h, "kill -KILL $(pgrep -f '^/usr/bin/python3 -s -m sheregesh_console' | head -1)")
            stopped = None
        else:
            ssh(h, "systemctl restart sheregesh-console")
            stopped = None
        back = wait_active(h)
        time.sleep(4)
    s1 = svc(h)
    leftovers = ssh(h, "ps -eo pid,stat,cmd --no-headers | grep -E 'cage|sheregesh_console' | grep -v grep")[1]
    res = {"before": s0, "after": s1, "stopped_state": stopped, "recovered_s": back, **p.result(),
           "processes_after": leftovers.splitlines(), "journal": journal(h, t0, pattern="stopped:|Started|Main process|Failed")}
    ok = back is not None and not p.fail and s1.get("ActiveState") == "active"
    if action == "stop":
        ok = ok and stopped in ("inactive", "failed")
    return {**res, "verdict": "PASS" if ok else "FAIL"}


WD_SCRIPT = r"""
MODE=$1
svc() { systemctl show sheregesh-console -p "$1" --value; }
tty1() { for f in /proc/[0-9]*/fd/*; do [ "$(readlink $f 2>/dev/null)" = /dev/tty1 ] && echo "${f#/proc/}" | cut -d/ -f1; done | sort -u | tr "\n" " "; }
RELAY=$(svc MainPID); CAGE=$(pgrep -P $RELAY -x cage); APP=$(pgrep -P $CAGE -f sheregesh_console); PG=$(ps -o pgid= -p $CAGE | tr -d " ")
N0=$(svc NRestarts); echo "before relay=$RELAY cage=$CAGE app=$APP pgid=$PG restarts=$N0"
case $MODE in freeze) kill -STOP $APP ;; relay-kill-frozen) kill -STOP $APP; kill -KILL $RELAY ;; esac
T0=$(date +%s)
while [ "$(svc NRestarts)" = "$N0" ] && [ $(( $(date +%s)-T0 )) -lt 60 ]; do sleep 1; done
echo "restart_after_s=$(( $(date +%s)-T0 ))"
for i in $(seq 1 60); do [ "$(svc ActiveState)" = active ] && break; sleep 1; done; sleep 3
echo "after=$(svc ActiveState)/$(svc SubState) restarts=$(svc NRestarts) result=$(svc Result)"
for n in relay:$RELAY cage:$CAGE app:$APP; do p=${n#*:}; [ -d /proc/$p ] && echo "LEFT ${n%%:*} $p $(awk '{print $3}' /proc/$p/stat)" || echo "gone ${n%%:*} $p"; done
L=$(ps -eo pgid=,pid=,cmd= | awk -v g="$PG" '$1==g'); [ -n "$L" ] && echo "LEFT group $PG: $L" || echo "group_empty $PG"
echo "tty1_holders=$(tty1) new_cage=$(pgrep -x cage | tr "\n" " ")"
"""


def _wd(a, mode):
    h = a.host
    t0 = time.time()
    with SshProbe(h) as p:
        rc, out, err = ssh(h, f"sh -s {mode}", data=WD_SCRIPT, timeout=180)
    lines = out.splitlines()
    old = next((ln for ln in lines if ln.startswith("before")), "")
    ok = (any(ln.startswith("after=active/running") for ln in lines) and not any(ln.startswith("LEFT") for ln in lines)
          and any(ln.startswith("group_empty") for ln in lines) and not p.fail
          and any(ln.startswith("restart_after_s=") and ln != "restart_after_s=60" for ln in lines))
    old_pids = {x.split("=")[1] for x in old.split() if x.split("=")[0] in ("cage", "app")}
    tty = next((ln for ln in lines if ln.startswith("tty1_holders")), "")
    ok = ok and not (old_pids & set(tty.split("=")[1].split()))
    return {"output": lines, **p.result(), "journal": journal(h, t0, pattern="Watchdog|notify-relay|Main process|Started"),
            "verdict": "PASS" if ok else "FAIL"}


def _variant(name, path, transform):
    """Временный коммит поверх HEAD с изменённым файлом — только для испытания, без ветки и без публикации."""
    base = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    src = subprocess.run(["git", "-C", str(ROOT), "show", f"{base}:{path}"], capture_output=True, text=True, check=True).stdout
    blob = subprocess.run(["git", "-C", str(ROOT), "hash-object", "-w", "--stdin"], input=transform(src),
                          capture_output=True, text=True, check=True).stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        who = {f"GIT_{r}_{k}": v for r in ("AUTHOR", "COMMITTER")       # не зависит от настроек git машины/CI
               for k, v in (("NAME", "check_pi_deploy"), ("EMAIL", "check-pi-deploy@sheregesh.invalid"))}
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index"), **who}
        g = lambda *x, **k: subprocess.run(["git", "-C", str(ROOT), *x], env=env, capture_output=True, text=True,  # noqa: E731
                                           check=True, **k).stdout.strip()
        g("read-tree", base)
        g("update-index", "--cacheinfo", f"100644,{blob},{path}")
        tree = g("write-tree")
        return g("commit-tree", tree, "-p", base, "-m", f"TEST ONLY (check_pi_deploy {name}): не публикуется")


def _deploy(a, action, rev=None):
    cmd = [*SHDEPLOY, action, a.board, "--host", a.host] + (["--rev", rev] if rev else [])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    return r.returncode, r.stdout.strip().splitlines()[-12:], r.stderr.strip()[-400:]


def _deploy_case(a, action, rev=None, expect="OK", expect_release=None):
    h = a.host
    s0 = state(h)
    t0 = time.time()
    with SshProbe(h) as p:
        rc, out, err = _deploy(a, action, rev)
        back = wait_active(h)
    s1 = state(h)
    last = s1.get("last") or {}
    rel = app_release(h)
    res = {"rev": rev, "shdeploy_rc": rc, "shdeploy_out": out, "shdeploy_err": err, "result": last.get("result"),
           "reason": last.get("reason"), "current_before": s0.get("current"), "current_after": s1.get("current"),
           "app_env": rel, "service_active_after_s": back, **p.result(),
           "journal": journal(h, t0, pattern="sheregesh-launcher|stopped:|Started|Main process")}
    ok = last.get("result") == expect and back is not None and not p.fail
    if expect == "ROLLED_BACK":
        ok = ok and s1.get("current") == s0.get("current")
    if expect_release is not None:
        ok = ok and rel.get("SHEREGESH_RELEASE") == expect_release
    return {**res, "verdict": "PASS" if ok else "FAIL"}


def sc_deploy_good(a):
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    r = _deploy_case(a, "deploy", None, "OK")
    rid = r.get("current_after")
    ok = r["verdict"] == "PASS" and rid and commit[:12] in rid and r["app_env"].get("SHEREGESH_RELEASE") == rid
    return {**r, "verdict": "PASS" if ok else "FAIL"}


def sc_deploy_bad_start(a):
    rev = _variant("bad-start", "software/console/sheregesh_console/__main__.py",
                   lambda s: s.replace("from .app import main", "raise SystemExit(3)  # TEST ONLY\nfrom .app import main"))
    return _deploy_case(a, "deploy", rev, "ROLLED_BACK")


def sc_deploy_crash_window(a):
    rev = _variant("crash-window", "software/console/sheregesh_console/__main__.py",
                   lambda s: s.replace("from .app import main", "import os, threading  # TEST ONLY\n"
                                       "threading.Timer(15, lambda: os._exit(9)).start()\nfrom .app import main"))
    return _deploy_case(a, "deploy", rev, "ROLLED_BACK")


def sc_rollback(a):
    return _deploy_case(a, "rollback", None, "OK")


def sc_factory(a):
    r = _deploy_case(a, "factory", None, "OK")
    ok = r["verdict"] == "PASS" and r["current_after"] is None and r["app_env"].get("SHEREGESH_CODE_ROOT") == "/usr/lib/sheregesh"
    return {**r, "verdict": "PASS" if ok else "FAIL"}


def _upload_install(a, data, signature):
    """Загрузка пакета и подписи как это делает shdeploy, запуск install; итог — из state.json."""
    h = a.host
    inc = ".cache/sheregesh-deploy/incoming"
    ssh(h, f"mkdir -p ~/{inc} && rm -f ~/{inc}/bundle.*")
    for name, blob in (("bundle.tar.xz.sig", signature), ("bundle.tar.xz", data)):
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", f"sheregesh-ui@{h}",
                            f"cat > ~/{inc}/{name}.part && mv ~/{inc}/{name}.part ~/{inc}/{name}"],
                           input=blob, capture_output=True, timeout=120)
        if r.returncode:
            raise RuntimeError(f"загрузка {name}: {r.stderr.decode()[-200:]}")
    return ssh(h, "systemctl start sheregesh-deploy@install.service; echo rc=$?")


def _reject_case(a, make):
    sys.path[:0] = [str(ROOT / "software"), str(ROOT / "tools/deploy"), str(ROOT / "tools/image")]
    import shdeploy  # noqa: E402
    from sheregesh.deploy import sig  # noqa: E402
    h = a.host
    data, version, commit, _ = shdeploy.build_release("HEAD")
    with tempfile.TemporaryDirectory() as tmp:
        data, signature = make(data, sig, Path(tmp))
    s0, v0 = state(h), svc(h)
    with SshProbe(h) as p:
        rc, out, err = _upload_install(a, data, signature)
        time.sleep(3)
    s1, v1 = state(h), svc(h)
    last = s1.get("last") or {}
    untouched = (s1.get("current") == s0.get("current") and v1.get("MainPID") == v0.get("MainPID")
                 and v1.get("NRestarts") == v0.get("NRestarts"))
    ran = s1.get("history") != s0.get("history")
    res = {"result": last.get("result"), "reason": last.get("reason"), "installer_ran": ran, "unit_output": out,
           "current_before": s0.get("current"), "current_after": s1.get("current"),
           "service_untouched": untouched, **p.result()}
    ok = ran and last.get("result") == "REJECTED" and untouched and not p.fail
    return {**res, "verdict": "PASS" if ok else "FAIL"}


def sc_foreign_sig(a):
    def make(data, sig, tmp):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp / "foreign")], check=True)
        return data, sig.sign(data, tmp / "foreign", sig.NS_APP)
    return _reject_case(a, make)


def sc_tampered(a):
    key = Path("~/.ssh/sheregesh-deploy").expanduser()

    def make(data, sig, tmp):
        signature = sig.sign(data, key, sig.NS_APP)                   # подпись владельца над исходными байтами
        bad = bytearray(data)
        bad[len(bad) // 2] ^= 0x01                                     # затем пакет изменён
        return bytes(bad), signature
    return _reject_case(a, make)


def sc_login_key_sig(a):
    login = Path("~/.ssh/id_ed25519").expanduser()
    if not login.exists():
        return {"verdict": "SKIP", "reason": f"нет {login}"}
    return _reject_case(a, lambda data, sig, tmp: (data, sig.sign(data, login, sig.NS_APP)))


def sc_no_bytecode(a):
    _, out, _ = ssh(a.host, "d=/var/lib/sheregesh-deploy/releases; ls $d 2>/dev/null | wc -l; "
                            "find $d \\( -name '*.pyc' -o -name __pycache__ \\) 2>/dev/null | head -5; echo ---; "
                            "find $d -maxdepth 1 -mindepth 1 -printf '%u %m %f\\n' 2>/dev/null")
    count, rest = out.split("\n", 1) if "\n" in out else (out, "")
    found, owners = rest.split("---", 1) if "---" in rest else (rest, "")
    owners = [x for x in owners.strip().splitlines() if x]
    if int(count or 0) == 0:
        return {"verdict": "SKIP", "reason": "выпусков на плате нет (сначала deploy-good)"}
    ok = not found.strip() and owners and all(x.split()[0] == "root" and x.split()[1] == "755" for x in owners)
    return {"releases": owners, "bytecode_found": found.strip().splitlines(), "verdict": "PASS" if ok else "FAIL"}


def sc_reboot(a):
    h = a.host
    _, boot0, _ = ssh(h, "cat /proc/sys/kernel/random/boot_id")
    s0, rel0 = state(h), app_release(h)
    ssh(h, "systemctl reboot", timeout=30)
    t0 = time.monotonic()
    down = up = None
    while time.monotonic() - t0 < 300:
        try:
            rc, b, _ = ssh(h, "cat /proc/sys/kernel/random/boot_id", timeout=15)
        except subprocess.TimeoutExpired:
            rc, b = 255, ""
        if rc != 0 and down is None:
            down = round(time.monotonic() - t0, 1)
        if rc == 0 and b and b != boot0:
            up = round(time.monotonic() - t0, 1)
            break
        time.sleep(3)
    if up is None:
        return {"verdict": "FAIL", "reason": "плата не вернулась по SSH за 300 с (нужен физический доступ)"}
    active = wait_active(h, timeout=150)
    s1, rel1 = state(h), app_release(h)
    _, sysstate, _ = ssh(h, "systemctl is-system-running; systemd-analyze | head -1")
    ok = (active is not None and rel1.get("SHEREGESH_RELEASE") == rel0.get("SHEREGESH_RELEASE")
          and s1.get("current") == s0.get("current"))
    return {"ssh_down_after_s": down, "ssh_back_after_s": up, "console_active_after_s": active,
            "release_before": rel0, "release_after": rel1, "system": sysstate.splitlines(),
            "verdict": "PASS" if ok else "FAIL"}


def sc_corrupt(a):
    return {"verdict": "SKIP", "reason": "повреждение выпуска на плате требует root (sheregesh-ui не может — см. tamper); "
                                         "переход на заводской код подтверждён контейнерным испытанием systemd 257"}


SCENARIOS = {"image": sc_image, "polkit": sc_polkit, "tamper": sc_tamper,
             "app-stop": lambda a: _app_failure(a, "stop"), "app-kill": lambda a: _app_failure(a, "kill"),
             "app-restart": lambda a: _app_failure(a, "restart"),
             "watchdog-freeze": lambda a: _wd(a, "freeze"), "relay-kill-frozen": lambda a: _wd(a, "relay-kill-frozen"),
             "deploy-good": sc_deploy_good, "deploy-bad-start": sc_deploy_bad_start,
             "deploy-crash-window": sc_deploy_crash_window, "rollback": sc_rollback, "factory": sc_factory,
             "foreign-sig": sc_foreign_sig, "tampered": sc_tampered, "login-key-sig": sc_login_key_sig, "no-bytecode": sc_no_bytecode,
             "reboot": sc_reboot, "corrupt": sc_corrupt}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", required=True, choices=("pi1", "pi2"))
    ap.add_argument("--host", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--only", default=",".join(SCENARIOS))
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    _, serial, _ = ssh(a.host, "tr -d '\\0' </proc/device-tree/serial-number")
    _, pkg, _ = ssh(a.host, "dpkg-query -W -f='${Version}' sheregesh")
    meta = {"board": a.board, "host": a.host, "serial": serial, "package": pkg,
            "mac_commit": subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True,
                                         text=True).stdout.strip()}
    summary = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **meta, "results": {}}
    for name in a.only.split(","):
        print(f"== {name}", flush=True)
        started, t0 = time.strftime("%Y-%m-%dT%H:%M:%S%z"), time.monotonic()
        try:
            r = SCENARIOS[name](a)
        except Exception as e:                                   # noqa: BLE001 — сбой стенда — ERROR, не PASS
            r = {"verdict": "ERROR", "error": f"{type(e).__name__}: {e}"}
        if r.get("verdict") not in VERDICTS:
            r = {**r, "verdict": "ERROR", "error": f"неизвестный вердикт {r.get('verdict')!r}"}
        r = {"scenario": name, "started": started, "duration_s": round(time.monotonic() - t0, 1), **r}
        (a.out / f"{name}.json").write_text(json.dumps({"meta": meta, **r}, ensure_ascii=False, indent=1))
        summary["results"][name] = r["verdict"]
        print(f"   {r['verdict']}" + (f" — {r.get('reason') or r.get('error') or r.get('not_denied') or ''}"
                                      if r["verdict"] != "PASS" else ""), flush=True)
    summary["counts"] = {v: sum(x == v for x in summary["results"].values()) for v in VERDICTS}
    (a.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(json.dumps(summary["results"], ensure_ascii=False), summary["counts"])
    return 0 if all(v in ("PASS", "SKIP") for v in summary["results"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
