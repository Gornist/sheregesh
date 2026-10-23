# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Испытания защищённого канала pi1 ↔ pi2 на физических платах (S3.1). Запуск с машины владельца по SSH.

  python3 tools/probe/check_link.py --console 192.168.1.89 --remote 192.168.1.169 --out DIR [--only NAME,...]

Сценарии (каждый — DIR/<сценарий>.json, итог — DIR/summary.json):
  baseline     сессия mTLS, взаимная идентичность, телеметрия, события; метрики за 60 с: частота сообщений,
               RTT, потери событий, CPU/RSS служб, температура и питание обеих плат
  hb-loss      пауза сервера блока 5 с (SIGSTOP) → пульт обнаруживает потерю (RECONNECTING) и восстанавливается
  watchdog     пауза 25 с → systemd по WatchdogSec перезапускает службу блока; пульт восстанавливается
  restart      штатный перезапуск sheregesh-remote-net → пульт восстанавливается
  outsider     машина владельца как посторонний узел сети: TCP без TLS, TLS без сертификата, сертификат чужого CA,
               сертификат CA стенда с чужой идентичностью — доступа нет, отказы в журнале блока
  identity     клиент на pi1, ожидающий другой блок, отказывает подлинному pi2 (проверка сервера клиентом)
  version      временный сервер на pi2 с протоколом 2.0 → клиент на pi1: INCOMPATIBLE
  health       sheregesh-health --json на обеих платах
Ничего не меняет в образах. Сигналы — только процессам пользователя sheregesh-ui; перезапуск — разрешённый polkit.
"""
import argparse
import json
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "software"))
CA_DIR = Path("~/.sheregesh-lab-ca").expanduser()
C_STATUS, C_UI, R_STATUS = "/run/sheregesh-ui/link-status.json", "/run/sheregesh-ui/ui-state.json", \
    "/run/sheregesh-remote/link-status.json"


def ssh(host, cmd, timeout=120, check=False):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"sheregesh-ui@{host}", cmd],
                       capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"{host}: {cmd[:60]}: {r.stderr.strip()[-300:]}")
    return r.stdout.strip()


def jread(host, path):
    try:
        return json.loads(ssh(host, f"cat {path}") or "{}")
    except ValueError:
        return {}


def wait(pred, timeout, step=0.5):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = pred()
        if v:
            return round(time.monotonic() - t0, 2)
        time.sleep(step)
    return None


def remote_pid(rh):
    return ssh(rh, "systemctl show -p MainPID --value sheregesh-remote-net")


def resources(host, pattern):
    out = ssh(host, f"ps -eo pid,%cpu,rss,args | grep -E '{pattern}' | grep -v grep; vcgencmd measure_temp; "
                    "vcgencmd get_throttled; cat /proc/loadavg")
    return out.splitlines()


def connected(ch):
    return lambda: jread(ch, C_STATUS).get("phase") == "connected"


def sc_baseline(ch, rh):
    if not wait(connected(ch), 60):
        return {"verdict": "FAIL", "reason": "нет соединения за 60 с", "console": jread(ch, C_STATUS)}
    c0, r0, t0 = jread(ch, C_STATUS), jread(rh, R_STATUS), time.time()
    time.sleep(60)
    c1, r1, t1 = jread(ch, C_STATUS), jread(rh, R_STATUS), time.time()
    dt = t1 - t0
    rs0, rs1 = r0.get("stats", {}), r1.get("stats", {})
    sent, acked = rs1["events_sent"] - rs0["events_sent"], rs1["events_acked"] - rs0["events_acked"]
    hb_rx = c1["counters"]["received"] - c0["counters"]["received"]
    ui = jread(ch, C_UI)
    session = ssh(rh, "journalctl -b -u sheregesh-remote-net -o cat --no-pager | grep 'session from' | tail -1")
    tel = c1.get("telemetry") or {}
    res = {
        "console": {k: c1.get(k) for k in ("phase", "detail", "tls", "host", "port", "session", "peer_uri", "peer")},
        "remote": {k: r1.get(k) for k in ("connected", "session", "peer", "own_uri", "listen")},
        "remote_session_log": session, "ui": ui,
        "telemetry_fields": sorted(tel.keys()), "remote_temps": tel.get("temperatures"),
        "metrics": {"seconds": round(dt, 1), "messages_per_s": round(hb_rx / dt, 2),
                    "seq_gaps": c1["counters"]["seq_gaps"] - c0["counters"]["seq_gaps"],
                    "events_sent": sent, "events_acked": acked, "event_loss": sent - acked,
                    "rtt_avg_ms": rs1.get("rtt_avg_ms"), "rtt_max_ms": rs1.get("rtt_max_ms"),
                    "rtt_last_ms": rs1.get("rtt_last_ms")},
        "resources": {"console": resources(ch, "sheregesh_console|cage"), "remote": resources(rh, "sheregesh_remote")},
    }
    ok = (c1.get("phase") == "connected" and c1.get("tls") == "TLSv1.3" and r1.get("connected")
          and "urn:sheregesh:console:" in session and sent > 0 and acked == sent and ui.get("peerLink") == "CONNECTED"
          and {"temperatures", "power", "link"} <= set(tel))
    return {**res, "verdict": "PASS" if ok else "FAIL"}


def sc_pause(ch, rh, seconds, expect_watchdog):
    if not wait(connected(ch), 60):
        return {"verdict": "FAIL", "reason": "нет соединения до сценария"}
    pid, n0 = remote_pid(rh), ssh(rh, "systemctl show -p NRestarts --value sheregesh-remote-net")
    t_stop = time.monotonic()
    ssh(rh, f"kill -STOP {pid}")
    detect = wait(lambda: jread(ch, C_STATUS).get("phase") != "connected", seconds + 10, 0.25)
    ui_during = jread(ch, C_UI)
    if not expect_watchdog:
        time.sleep(max(0.0, seconds - (time.monotonic() - t_stop)))
        ssh(rh, f"kill -CONT {pid}")
    t_resume = time.monotonic()
    recover = wait(connected(ch), 90, 0.5)
    n1 = ssh(rh, "systemctl show -p NRestarts --value sheregesh-remote-net")
    wd_log = ssh(rh, "journalctl -b -u sheregesh-remote-net -o cat --no-pager | grep -iE 'watchdog|stopped: result' | tail -3")
    res = {"pause_s": seconds, "detect_s": detect, "ui_during": ui_during,
           "recover_s_after_resume": recover, "since_pause_s": round(time.monotonic() - t_stop, 1),
           "restarts_before": n0, "restarts_after": n1, "journal": wd_log.splitlines()}
    if expect_watchdog:
        ssh(rh, f"kill -CONT {pid} 2>/dev/null; true")
        ok = detect is not None and recover is not None and int(n1 or 0) > int(n0 or 0) and "watchdog" in wd_log.lower()
    else:
        ok = detect is not None and recover is not None and n1 == n0 and ui_during.get("peerLink") == "RECONNECTING"
    del t_resume
    return {**res, "verdict": "PASS" if ok else "FAIL"}


def sc_restart(ch, rh):
    if not wait(connected(ch), 60):
        return {"verdict": "FAIL", "reason": "нет соединения до сценария"}
    t0 = time.monotonic()
    ssh(rh, "systemctl restart sheregesh-remote-net")
    restarted = round(time.monotonic() - t0, 2)
    lost = wait(lambda: jread(ch, C_STATUS).get("phase") != "connected", 10, 0.2)
    recover = wait(connected(ch), 60, 0.5)
    return {"restart_cmd_s": restarted, "console_noticed_s": lost, "recover_s": recover,
            "verdict": "PASS" if recover is not None else "FAIL"}


def sc_outsider(ch, rh, port=7443):
    from sheregesh.link import pki
    log0 = int(ssh(rh, "journalctl -b -u sheregesh-remote-net -o cat --no-pager | grep -c 'auth:'") or 0)
    res = {}
    try:
        with socket.create_connection((rh, port), timeout=5) as s:                 # 1. TCP без TLS
            s.sendall(b'{"proto":"1.1","type":"hello","seq":1,"ts":0,"body":{}}\n')
            s.settimeout(8)
            data = s.recv(4096)
            res["plain_tcp"] = "данных SHRG-LAB нет" if b'"type":"hello"' not in data else "ПОЛУЧЕНЫ ДАННЫЕ"
    except OSError as e:
        res["plain_tcp"] = f"отказ: {type(e).__name__}"

    def tls_try(cert=None, key=None, ca=None):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if cert:
            ctx.load_cert_chain(cert, key)
        try:
            with socket.create_connection((rh, port), timeout=5) as raw, ctx.wrap_socket(raw) as s:
                s.settimeout(8)
                data = s.recv(4096)
                return "данных SHRG-LAB нет" if b'"type":"hello"' not in data else "ПОЛУЧЕНЫ ДАННЫЕ"
        except (ssl.SSLError, OSError) as e:
            return f"отказ: {getattr(e, 'reason', None) or type(e).__name__}"
    res["tls_no_cert"] = tls_try()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pki.ca_init(tmp / "rogue", name="Rogue CA")
        d = tmp / "rogue-dev"
        pki.install(d, pki.ca_sign(tmp / "rogue", pki.keygen("console", "ROGUE", d), "console", "ROGUE"),
                    (tmp / "rogue" / "ca.crt").read_text(), "console", "ROGUE")
        res["tls_rogue_ca"] = tls_try(str(d / "device.crt"), str(d / "device.key"))
        if (CA_DIR / "ca.key").exists():                                          # подлинный CA, чужое устройство
            d2 = tmp / "intruder"
            pki.install(d2, pki.ca_sign(CA_DIR, pki.keygen("console", "INTRUDER", d2), "console", "INTRUDER"),
                        (CA_DIR / "ca.crt").read_text(), "console", "INTRUDER")
            res["tls_lab_ca_wrong_identity"] = tls_try(str(d2 / "device.crt"), str(d2 / "device.key"))
    time.sleep(1)
    log = ssh(rh, "journalctl -b -u sheregesh-remote-net -o cat --no-pager | grep 'auth:' | tail -6")
    log1 = int(ssh(rh, "journalctl -b -u sheregesh-remote-net -o cat --no-pager | grep -c 'auth:'") or 0)
    still = jread(ch, C_STATUS).get("phase")
    ok = all("ПОЛУЧЕНЫ" not in v for v in res.values()) and log1 - log0 >= len(res) - 1 and still == "connected"
    return {"attempts": res, "remote_auth_log": log.splitlines(), "auth_rejections": log1 - log0,
            "console_session_unaffected": still, "verdict": "PASS" if ok else "FAIL"}


CLIENT_SNIPPET = r'''
import json, sys, time
sys.path.insert(0, "/usr/lib/sheregesh/software")
from sheregesh.link.tls import LinkConfig
from sheregesh.link.client import PeerClient
c = LinkConfig.load("~/.config/sheregesh/link.json")
cfg = LinkConfig(c.role, c.device_id, sys.argv[1], c.dir, port=int(sys.argv[2]), peer_hosts=(sys.argv[3],))
cl = PeerClient(None, int(sys.argv[2]), tls_cfg=cfg).start()
for _ in range(40):
    s = cl.snapshot()
    if s["phase"] in ("auth-failed", "incompatible", "connected"):
        break
    time.sleep(0.25)
cl.stop()
print(json.dumps({k: s[k] for k in ("phase", "detail", "tls", "session", "peer")}))
'''


def sc_identity(ch, rh):
    out = ssh(ch, f"python3 - remote:SOMEONE-ELSE 7443 {rh} <<'EOF'\n{CLIENT_SNIPPET}\nEOF")
    r = json.loads(out.splitlines()[-1])
    return {"client_on_console_expecting": "remote:SOMEONE-ELSE", "result": r,
            "verdict": "PASS" if r["phase"] == "auth-failed" and "identity" in r["detail"] else "FAIL"}


def sc_version(ch, rh):
    rid = json.loads(ssh(rh, "cat ~/.config/sheregesh/link.json"))["device_id"]
    ssh(rh, "nohup sheregesh-remote-emu --link-config ~/.config/sheregesh/link.json --protocol 2.0 --port 7543 "
            ">/tmp/v2.log 2>&1 & echo $! > /tmp/v2.pid")
    time.sleep(3)
    try:
        out = ssh(ch, f"python3 - remote:{rid} 7543 {rh} <<'EOF'\n{CLIENT_SNIPPET}\nEOF")
        r = json.loads(out.splitlines()[-1])
    finally:
        ssh(rh, "kill $(cat /tmp/v2.pid) 2>/dev/null; sleep 1; grep -E 'hello|session' /tmp/v2.log | tail -2; rm -f /tmp/v2.pid")
    return {"result": r, "verdict": "PASS" if r["phase"] == "incompatible" and r["detail"] == "major" else "FAIL"}


def sc_health(ch, rh):
    out = {}
    for name, h in (("console", ch), ("remote", rh)):
        try:
            out[name] = json.loads(ssh(h, "sheregesh-health --json") or "{}")
        except ValueError:
            out[name] = {"verdict": "ERROR"}
    return {**out, "verdict": "PASS" if all(v.get("verdict") == "PASS" for v in out.values()) else "FAIL"}


SCENARIOS = {"baseline": sc_baseline, "hb-loss": lambda c, r: sc_pause(c, r, 5, False),
             "watchdog": lambda c, r: sc_pause(c, r, 25, True), "restart": sc_restart, "outsider": sc_outsider,
             "identity": sc_identity, "version": sc_version, "health": sc_health}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console", required=True)
    ap.add_argument("--remote", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--only", default=",".join(SCENARIOS))
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    meta = {}
    for role, h in (("console", a.console), ("remote", a.remote)):
        meta[role] = {"host": h, "serial": ssh(h, "tr -d '\\0' </proc/device-tree/serial-number"),
                      "package": ssh(h, "dpkg-query -W -f='${Version}' sheregesh"),
                      "kernel": ssh(h, "uname -r"), "uptime_s": ssh(h, "cut -d' ' -f1 /proc/uptime")}
    summary = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "boards": meta, "results": {}}
    for name in a.only.split(","):
        print(f"== {name}", flush=True)
        try:
            r = SCENARIOS[name](a.console, a.remote)
        except Exception as e:                                   # noqa: BLE001 — протокол фиксирует любой сбой сценария
            r = {"verdict": "ERROR", "error": f"{type(e).__name__}: {e}"}
        (a.out / f"{name}.json").write_text(json.dumps({"boards": meta, **r}, ensure_ascii=False, indent=1))
        summary["results"][name] = r["verdict"]
        print(f"   {r['verdict']}", flush=True)
    (a.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(json.dumps(summary["results"], ensure_ascii=False))
    return 0 if all(v == "PASS" for v in summary["results"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
