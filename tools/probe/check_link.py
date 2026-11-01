# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Испытания защищённого канала pi1 ↔ pi2 на физических платах (S3.1). Запуск с машины владельца по SSH.

  python3 tools/probe/check_link.py --console 192.168.1.89 --remote 192.168.1.169 --out DIR [--only NAME,...]

Сценарии (каждый — DIR/<сценарий>.json, итог — DIR/summary.json):
  connect      штатный перезапуск пульта на pi1 → время до сессии mTLS с pi2 (старт службы + обнаружение + TLS)
  baseline     сессия mTLS, взаимная идентичность, телеметрия, события; метрики за 60 с: частота сообщений,
               RTT, потери событий, загрузка CPU платы за окно, память, RSS служб, температура и питание обеих плат
  ports        с машины владельца: TCP-порты 1–10000 обеих плат; на платах — слушающие TCP/UDP сокеты
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


def sample(host):
    """Счётчики CPU (/proc/stat), память, температура, питание — для загрузки за окно, а не за всё время процесса."""
    out = ssh(host, "head -1 /proc/stat; grep -E 'MemTotal|MemAvailable' /proc/meminfo; vcgencmd measure_temp; "
                    "vcgencmd get_throttled; cut -d' ' -f1-3 /proc/loadavg")
    lines = out.splitlines()
    cpu = [int(x) for x in lines[0].split()[1:]]
    mem = {ln.split(":")[0]: int(ln.split()[1]) for ln in lines[1:3]}
    return {"cpu": cpu, "mem_total_kb": mem["MemTotal"], "mem_available_kb": mem["MemAvailable"],
            "temp": lines[3].split("=")[-1], "throttled": lines[4].split("=")[-1], "loadavg": lines[5]}


def cpu_window(s0, s1):
    d = [b - a for a, b in zip(s0["cpu"], s1["cpu"])]
    total, idle = sum(d), d[3] + (d[4] if len(d) > 4 else 0)
    return {"cpu_busy_pct": round(100 * (total - idle) / total, 1) if total else None,
            "mem_used_mb": round((s1["mem_total_kb"] - s1["mem_available_kb"]) / 1024),
            "mem_total_mb": round(s1["mem_total_kb"] / 1024), "temp_start": s0["temp"], "temp_end": s1["temp"],
            "throttled": s1["throttled"], "loadavg": s1["loadavg"]}


def connected(ch):
    return lambda: jread(ch, C_STATUS).get("phase") == "connected"


def sc_baseline(ch, rh):
    if not wait(connected(ch), 60):
        return {"verdict": "FAIL", "reason": "нет соединения за 60 с", "console": jread(ch, C_STATUS)}
    c0, r0, t0 = jread(ch, C_STATUS), jread(rh, R_STATUS), time.time()
    s0 = {"console": sample(ch), "remote": sample(rh)}
    time.sleep(60)
    c1, r1, t1 = jread(ch, C_STATUS), jread(rh, R_STATUS), time.time()
    s1 = {"console": sample(ch), "remote": sample(rh)}
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
        "boards_window": {k: cpu_window(s0[k], s1[k]) for k in s0},
    }
    ok = (c1.get("phase") == "connected" and c1.get("tls") == "TLSv1.3" and r1.get("connected")
          and "urn:sheregesh:console:" in session and sent > 0 and acked == sent and ui.get("peerLink") == "CONNECTED"
          and {"temperatures", "link"} <= set(tel))      # power — только с модулем питания (на Pi 4 нет, REQ-PWR-007)
    return {**res, "verdict": "PASS" if ok else "FAIL"}


def sc_pause(ch, rh, seconds, expect_watchdog):
    if not wait(connected(ch), 60):
        return {"verdict": "ERROR", "reason": "нет соединения до сценария (стенд не готов)"}
    pid, n0 = remote_pid(rh), ssh(rh, "systemctl show -p NRestarts --value sheregesh-remote-net")
    if not pid.isdigit() or pid == "0":
        return {"verdict": "ERROR", "reason": f"нет главного PID sheregesh-remote-net: {pid!r}"}
    t_stop = time.monotonic()
    detect = ui_during = recover = None
    try:
        ssh(rh, f"kill -STOP {pid}", check=True)
        detect = wait(lambda: jread(ch, C_STATUS).get("phase") != "connected", seconds + 10, 0.25)
        # Плитка пульта обновляется раз в секунду — ждём её состояния, пока пауза не кончилась.
        ui_during = jread(ch, C_UI)
        while ((ui_during or {}).get("peerLink") == "CONNECTED"
               and time.monotonic() - t_stop < (seconds if not expect_watchdog else seconds + 10)):
            time.sleep(0.25)
            ui_during = jread(ch, C_UI)
        if not expect_watchdog:
            time.sleep(max(0.0, seconds - (time.monotonic() - t_stop)))
            ssh(rh, f"kill -CONT {pid}", check=True)
        recover = wait(connected(ch), 90, 0.5)
    finally:                                     # при любой ошибке или прерывании процесс не остаётся замороженным
        resumed = ssh(rh, f"kill -CONT {pid} 2>/dev/null && echo resumed || echo gone")
    n1 = ssh(rh, "systemctl show -p NRestarts --value sheregesh-remote-net")
    active = ssh(rh, "systemctl is-active sheregesh-remote-net")
    wd_log = ssh(rh, "journalctl -b -u sheregesh-remote-net -o cat --no-pager | grep -iE 'watchdog|stopped: result' | tail -3")
    res = {"pause_s": seconds, "detect_s": detect, "ui_during": ui_during,
           "recover_s_after_resume": recover, "since_pause_s": round(time.monotonic() - t_stop, 1),
           "restarts_before": n0, "restarts_after": n1, "service_after": active, "final_cont": resumed,
           "journal": wd_log.splitlines()}
    if resumed not in ("resumed", "gone"):        # восстановление не подтверждено (нет SSH?) — дальше не продолжать
        return {**res, "verdict": "ERROR", "abort": True,
                "reason": f"не подтверждено возобновление PID {pid} на блоке: {resumed!r}"}
    if expect_watchdog:
        ok = (detect is not None and recover is not None and int(n1 or 0) > int(n0 or 0)
              and "watchdog" in wd_log.lower() and active == "active")
    else:
        ok = (detect is not None and recover is not None and n1 == n0 and active == "active"
              and (ui_during or {}).get("peerLink") == "RECONNECTING")
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


def sc_connect(ch, rh):
    if not wait(connected(ch), 60):
        return {"verdict": "FAIL", "reason": "нет соединения до сценария"}
    n0 = ssh(ch, "systemctl show -p NRestarts --value sheregesh-console")
    t0 = time.monotonic()
    ssh(ch, "systemctl restart sheregesh-console")
    restart_cmd = round(time.monotonic() - t0, 2)                 # Type=notify: возвращается после READY=1
    gone = wait(lambda: jread(ch, C_STATUS).get("phase") != "connected", 10, 0.2)
    conn = wait(connected(ch), 60, 0.25)
    st = jread(ch, C_STATUS)
    show = ssh(ch, "systemctl show -p ActiveState,SubState,NRestarts,StatusText sheregesh-console")
    res = {"restart_to_ready_s": restart_cmd, "status_reset_s": gone,
           "restart_to_connected_s": round(restart_cmd + (conn or 0), 2) if conn is not None else None,
           "console": {k: st.get(k) for k in ("phase", "tls", "host", "peer_uri", "session")},
           "service": show.splitlines(), "restarts_before": n0}
    ok = conn is not None and st.get("tls") == "TLSv1.3" and "ActiveState=active" in show
    return {**res, "verdict": "PASS" if ok else "FAIL"}


# Слушающие TCP (0A) и не соединённые UDP: соединённый UDP-сокет (01, connect()) получает датаграммы только от своего
# собеседника — это клиент (chronyd к серверу пула на пульте, ADR-0017), а не открытый порт (ota-90b9b9c, 2026-11-01).
LISTEN = r"""for f in /proc/net/tcp /proc/net/tcp6 /proc/net/udp /proc/net/udp6; do
  awk -v f="${f##*/}" 'NR>1 && ((f ~ /udp/ && $4!="01") || $4=="0A") {print f, $2, $10}' "$f"; done"""
# Сокеты службы видео платы (ADR-0016): inode из /proc/<MainPID>/fd — служба работает от sheregesh-ui, как вход по SSH
VIDEO_SOCKETS = {"console": "sheregesh-video-rx", "remote": "sheregesh-video-tx"}
VIDEO_FDS = ("p=$(systemctl show -p MainPID --value {unit}); [ \"$p\" -gt 0 ] 2>/dev/null && "
             "ls -l /proc/$p/fd 2>/dev/null | sed -n 's/.*socket:\\[\\([0-9]*\\)\\].*/\\1/p'")
EPHEMERAL = range(32768, 61000)            # net.ipv4.ip_local_port_range Linux по умолчанию


def _decode(proto, addr):
    host, port = addr.rsplit(":", 1)
    if len(host) == 8:
        ip = ".".join(str(int(host[i:i + 2], 16)) for i in (6, 4, 2, 0))
    else:
        import ipaddress
        b = bytes.fromhex(host)
        ip = str(ipaddress.IPv6Address(b"".join(b[i:i + 4][::-1] for i in range(0, 16, 4))))
    return f"{proto} {ip}:{int(port, 16)}"


# UDP 7444/7445 пульта и 7445 блока — видео SRTP/RTCP (ADR-0016, nftables: только адрес второй платы); 123 — chrony
# (ADR-0017: пульт отвечает только блоку, блок — никому)
ALLOWED = {"console": {"tcp": {22}, "udp": {68, 123, 546, 7444, 7445}},
           "remote": {"tcp": {22, 7443}, "udp": {68, 123, 546, 7445}}}     # TCP 7444 (TLS-видео) удалён


def judge_ports(role, listening, open_tcp, allowed=ALLOWED, video_owned=()):
    """Не loopback-сокеты платы и порты, открытые из LAN, — только разрешённые. UDP снаружи не сканируется
    (без ответа нельзя отличить закрытый порт от фильтра) — решение по слушающим сокетам самой платы. Временный порт UDP
    допустим, только если сокет принадлежит службе видео платы (video_owned — такие строки): это сокеты отправки
    GStreamer (udpsink RTP/RTCP), а не службы, принимающие соединения."""
    bad = []
    for line in listening:
        proto, addr = line.split()
        host, port = addr.rsplit(":", 1)
        if host.startswith("127.") or host == "::1":
            continue
        fam = "udp" if proto.startswith("udp") else "tcp"
        if int(port) in allowed[role][fam]:
            continue
        if fam == "udp" and int(port) in EPHEMERAL and line in video_owned:
            continue
        bad.append(line)
    bad += [f"lan tcp {p}" for p in open_tcp if p not in allowed[role]["tcp"]]
    return bad


def sc_ports(ch, rh):
    import concurrent.futures as cf
    out, bad = {}, []
    for role, h in (("console", ch), ("remote", rh)):
        def probe(port, h=h):
            try:
                with socket.create_connection((h, port), timeout=0.5):
                    return port
            except OSError:
                return None
        with cf.ThreadPoolExecutor(64) as ex:
            open_ = sorted(p for p in ex.map(probe, range(1, 10001)) if p)
        raw = ssh(h, LISTEN)
        if not raw.strip():
            return {"verdict": "ERROR", "reason": f"{role}: нет списка сокетов с платы"}
        rows = [ln.split() for ln in raw.splitlines() if ln.strip()]
        inodes = set(ssh(h, VIDEO_FDS.format(unit=VIDEO_SOCKETS[role])).split())
        local = sorted({_decode(*r[:2]) for r in rows})
        owned = {_decode(*r[:2]) for r in rows if len(r) > 2 and r[2] in inodes}
        b = judge_ports(role, local, open_, video_owned=owned)
        bad += [f"{role}: {x}" for x in b]
        out[role] = {"open_from_lan_tcp_ipv4_1_10000": open_, "allowed": {k: sorted(v) for k, v in ALLOWED[role].items()},
                     "listening_on_board_tcp_udp_v4_v6": local, "video_service_sockets": sorted(owned),
                     "not_allowed": b,
                     "note": "UDP снаружи не сканируется; IPv6 — по сокетам платы (внешний скан только IPv4)"}
    return {**out, "verdict": "FAIL" if bad else "PASS"}


def classify_client(exc=None, data=None):
    """Итог попытки на стороне клиента. Отказом аутентификации считаются только TLS-отказ и закрытие/ошибка
    LINK-AUTH без прикладных данных; таймаут и ошибка соединения — неопределённость (ERROR), не успех."""
    if exc is not None:
        if isinstance(exc, ssl.SSLError):
            return "tls-rejected", getattr(exc, "reason", None) or str(exc)[:120]
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return "timeout", "нет ответа — отказ не подтверждён"
        if isinstance(exc, ConnectionResetError | BrokenPipeError):
            return "closed", type(exc).__name__
        return "conn-error", f"{type(exc).__name__}: {exc}"[:120]
    if data is None or data == b"":
        return "closed", "соединение закрыто сервером без данных"
    if b"LINK-AUTH" in data and b'"type":"hello"' not in data:
        return "auth-error", "сервер ответил error LINK-AUTH"
    if b'"type":"hello"' in data or b'"type":"telemetry"' in data:
        return "data", "ПОЛУЧЕНЫ ДАННЫЕ SHRG-LAB"
    return "unknown", f"неожиданный ответ {data[:60]!r}"


def judge_attempt(client, new_auth_lines, new_sessions):
    """REJECTED — только при трёх доказательствах: отказ у клиента, запись auth: в журнале сервера, нет сессии."""
    kind = client[0]
    if kind == "data" or new_sessions:
        return "ACCEPTED"
    if kind in ("tls-rejected", "closed", "auth-error") and new_auth_lines:
        return "REJECTED"
    return "INCONCLUSIVE"


def _journal(rh, pattern):
    return [x for x in ssh(rh, f"journalctl -b -u sheregesh-remote-net -o cat --no-pager | grep -E '{pattern}'")
            .splitlines() if x.strip()]


def sc_outsider(ch, rh, port=7443):
    from sheregesh.link import pki
    if not wait(connected(ch), 60):
        return {"verdict": "ERROR", "reason": "нет штатного соединения до сценария"}
    try:
        with socket.create_connection((rh, port), timeout=5) as s:
            me = s.getsockname()[0]                                                # адрес Mac, как его видит pi2
    except OSError as e:
        return {"verdict": "ERROR", "reason": f"сервер блока недоступен: {e}"}

    def attempt(name, fn):
        a0 = len(_journal(rh, f"auth: .*{me}"))
        s0 = len(_journal(rh, f"session from {me}"))
        try:
            client = classify_client(data=fn())
        except Exception as e:                                                    # noqa: BLE001 — классифицируется
            client = classify_client(exc=e)
        time.sleep(1.0)
        auth = _journal(rh, f"auth: .*{me}")[a0:]
        sess = _journal(rh, f"session from {me}")[s0:]
        return {"client": {"kind": client[0], "detail": client[1]}, "server_auth_log": auth,
                "server_sessions": sess, "result": judge_attempt(client, auth, sess)}

    def plain():
        with socket.create_connection((rh, port), timeout=5) as s:
            s.sendall(b'{"proto":"1.1","type":"hello","seq":1,"ts":0,"body":{}}\n')
            s.settimeout(8)
            return s.recv(4096)

    def tls_try(cert=None, key=None):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if cert:
            ctx.load_cert_chain(cert, key)
        with socket.create_connection((rh, port), timeout=5) as raw, ctx.wrap_socket(raw) as s:
            s.settimeout(8)
            return s.recv(4096)

    res = {"plain_tcp": attempt("plain_tcp", plain), "tls_no_cert": attempt("tls_no_cert", tls_try)}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pki.ca_init(tmp / "rogue", name="Rogue CA")
        d = tmp / "rogue-dev"
        pki.install(d, pki.ca_sign(tmp / "rogue", pki.keygen("console", "ROGUE", d), "console", "ROGUE"),
                    (tmp / "rogue" / "ca.crt").read_text(), "console", "ROGUE")
        res["tls_rogue_ca"] = attempt("tls_rogue_ca", lambda: tls_try(str(d / "device.crt"), str(d / "device.key")))
        if (CA_DIR / "ca.key").exists():                                          # подлинный CA, чужое устройство
            d2 = tmp / "intruder"
            pki.install(d2, pki.ca_sign(CA_DIR, pki.keygen("console", "INTRUDER", d2), "console", "INTRUDER"),
                        (CA_DIR / "ca.crt").read_text(), "console", "INTRUDER")
            res["tls_lab_ca_wrong_identity"] = attempt(
                "tls_lab_ca_wrong_identity", lambda: tls_try(str(d2 / "device.crt"), str(d2 / "device.key")))
        else:
            res["tls_lab_ca_wrong_identity"] = {"result": "SKIPPED", "reason": f"нет CA стенда в {CA_DIR}"}
    restored = wait(connected(ch), 30)                                              # штатный канал не пострадал
    results = [v["result"] for v in res.values()]
    verdict = ("FAIL" if "ACCEPTED" in results else "ERROR" if "INCONCLUSIVE" in results
               else "SKIP" if "SKIPPED" in results else "PASS" if restored is not None else "FAIL")
    return {"outsider_addr": me, "attempts": res, "console_connected_after_s": restored, "verdict": verdict}


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


def _client_run(ch, peer_id, port, rh):
    out = ssh(ch, f"python3 - {peer_id} {port} {rh} <<'EOF'\n{CLIENT_SNIPPET}\nEOF")
    try:
        return json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        return None


def sc_identity(ch, rh):
    r = _client_run(ch, "remote:SOMEONE-ELSE", 7443, rh)
    if r is None or r.get("phase") not in ("auth-failed", "connected", "incompatible"):
        return {"verdict": "ERROR", "reason": "клиент не дал определённого результата", "result": r}
    restored = wait(connected(ch), 30)
    ok = r["phase"] == "auth-failed" and "identity" in (r.get("detail") or "") and restored is not None
    return {"client_on_console_expecting": "remote:SOMEONE-ELSE", "result": r, "console_connected_after_s": restored,
            "verdict": "PASS" if ok else "FAIL"}


def sc_version(ch, rh):
    # --no-video: временный сервер 2.0 не занимает видеопорт 7444 рабочей службы (S3.1-VIDEO, ADR-0011).
    rid = json.loads(ssh(rh, "cat ~/.config/sheregesh/link.json"))["device_id"]
    ssh(rh, "nohup sheregesh-remote-emu --link-config ~/.config/sheregesh/link.json --protocol 2.0 --port 7543 --no-video "
            ">/tmp/v2.log 2>&1 & echo $! > /tmp/v2.pid")
    time.sleep(3)
    try:
        r = _client_run(ch, f"remote:{rid}", 7543, rh)
    finally:                                     # временный сервер 2.0 не остаётся
        # Остановка — по PID и по слушающему порту 7543 (0x1D77), а не pgrep -f: тот находит саму команду ssh.
        log = ssh(rh, "p=$(cat /tmp/v2.pid 2>/dev/null); kill $p 2>/dev/null; sleep 1; "
                      "grep -E 'hello|session' /tmp/v2.log | tail -2; rm -f /tmp/v2.pid; "
                      "if [ -n \"$p\" ] && kill -0 $p 2>/dev/null; then echo \"alive $p\"; "
                      "elif awk 'NR>1 && $4==\"0A\"' /proc/net/tcp /proc/net/tcp6 | grep -qi ':1D77 '; then echo listening; "
                      "else echo stopped; fi")
    if r is None or r.get("phase") not in ("auth-failed", "connected", "incompatible"):
        return {"verdict": "ERROR", "reason": "клиент не дал определённого результата", "result": r, "server": log}
    restored = wait(connected(ch), 30)
    ok = r["phase"] == "incompatible" and r["detail"] == "major" and log.endswith("stopped") and restored is not None
    return {"result": r, "server_log": log.splitlines(), "console_connected_after_s": restored,
            "verdict": "PASS" if ok else "FAIL"}


NET_CHECKS = ("config.link", "link.state", "link.versions", "certs.device", "certs.ca", "certs.key-mode")


def sc_health(ch, rh):
    """Сетевой режим: проверки профиля, канала, версий и сертификатов обязаны быть PASS — SKIP здесь не успех."""
    out, problems = {}, []
    for name, h in (("console", ch), ("remote", rh)):
        try:
            out[name] = json.loads(ssh(h, f"sheregesh-health --json --role {name}") or "{}")
        except ValueError:
            return {"verdict": "ERROR", "reason": f"{name}: health не вернул JSON"}
        st = {c["id"]: c["status"] for c in out[name].get("checks", [])}
        problems += [f"{name}:{cid}={st.get(cid, 'нет')}" for cid in NET_CHECKS if st.get(cid) != "PASS"]
        if out[name].get("verdict") != "PASS":
            problems.append(f"{name}: verdict {out[name].get('verdict')}")
    return {**out, "network_checks_not_pass": problems, "verdict": "FAIL" if problems else "PASS"}


VERDICTS = ("PASS", "FAIL", "SKIP", "ERROR")
SCENARIOS = {"connect": sc_connect, "baseline": sc_baseline, "ports": sc_ports, "hb-loss": lambda c, r: sc_pause(c, r, 5, False),
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
    aborted = None
    for name in a.only.split(","):
        print(f"== {name}", flush=True)
        if aborted:                               # стенд в неподтверждённом состоянии — разрушительные сценарии не идут
            r = {"scenario": name, "verdict": "SKIP", "reason": f"не выполнялся: стенд не восстановлен после {aborted}"}
            (a.out / f"{name}.json").write_text(json.dumps({"boards": meta, **r}, ensure_ascii=False, indent=1))
            summary["results"][name] = "SKIP"
            print("   SKIP", flush=True)
            continue
        started, t0 = time.strftime("%Y-%m-%dT%H:%M:%S%z"), time.monotonic()
        try:
            r = SCENARIOS[name](a.console, a.remote)
        except Exception as e:                                   # noqa: BLE001 — сбой стенда — ERROR, не PASS
            r = {"verdict": "ERROR", "error": f"{type(e).__name__}: {e}"}
        if r.get("verdict") not in VERDICTS:
            r = {**r, "verdict": "ERROR", "error": f"неизвестный вердикт {r.get('verdict')!r}"}
        r = {"scenario": name, "started": started, "duration_s": round(time.monotonic() - t0, 1), **r}
        (a.out / f"{name}.json").write_text(json.dumps({"boards": meta, **r}, ensure_ascii=False, indent=1))
        summary["results"][name] = r["verdict"]
        if r.get("abort"):
            aborted = name
        print(f"   {r['verdict']}" + (f" — {r.get('reason') or r.get('error')}" if r["verdict"] != "PASS" else ""),
              flush=True)
    summary["counts"] = {v: sum(x == v for x in summary["results"].values()) for v in VERDICTS}
    (a.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(json.dumps(summary["results"], ensure_ascii=False), summary["counts"])
    return 0 if all(v == "PASS" for v in summary["results"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
