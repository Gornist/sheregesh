# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Сквозной сценарий E2E на двух платах: обрыв → обнаружение → восстановление, N циклов, с замером времени.

  python3 tools/probe/e2e_run.py --console HOST1 --remote HOST2 --out DIR --fault pause --cycles 10 [--outage 8]
  python3 tools/probe/e2e_run.py --plan --fault stop-start --cycles 5          # только план, без SSH

Чем дополняет check_link / check_video_link: там один цикл и вердикт «восстановилось вообще»; здесь — N циклов подряд,
пороги из SRS, раздельные «нет связи» / «нет видео», живость цикла событий GUI (ui-state.json пишется раз в секунду
из главного потока Qt), накопление ресурсов между циклами, журналы, инварианты стенда. См.
docs/acceptance/e2e/MATRIX.md — сопоставление E2E-01…15 с требованиями и условия допуска.

Модели отказа (--fault). Ни одна, кроме manual, НЕ эквивалентна выдернутому кабелю (MATRIX.md §3):
  pause        SIGSTOP главного процесса sheregesh-remote-net (процесс sheregesh-ui) на --outage ≤ 10 с, затем SIGCONT.
               «Молчащий узел»: нет FIN/RST, ядро pi2 продолжает подтверждать TCP. Страховка: на pi2 заранее запускается
               отложенный SIGCONT; WatchdogSec=15 блока — второй рубеж.
  stop-start   systemctl stop / start sheregesh-remote-net (polkit sheregesh-ui). «Блок недоступен»: соединение
               закрыто, новые — отказ (RST). Страховка: отложенный start на pi2.
  restart      systemctl restart sheregesh-remote-net (polkit). Короткий разрыв с FIN.
  sender-kill  SIGKILL генератора видео на pi2: пропадает только видео, управление должно остаться CONNECTED.
  manual       человек отключает/подключает кабель pi2 по подсказке (Enter в момент действия, погрешность ±1 с).
               Единственная модель, эквивалентная кабелю; SSH к pi2 во время обрыва недоступен — это ожидаемо.

Вердикты: PASS | FAIL | ERROR. ERROR — испытание не состоялось или не доказательно (стенд не готов, отказ не внесён,
восстановление не подтверждено, событие не наблюдалось) — никогда не PASS. Итог: FAIL, если есть FAIL; иначе ERROR,
если есть ERROR; иначе PASS. Пороги без утверждённого требования не влияют на вердикт — только «measured».

Ничего не меняет в образах и конфигурации. Сигналы — только процессам sheregesh-ui; systemctl — только действия,
разрешённые правилом polkit 50-sheregesh-dev.rules. Перед любым внесением отказа проверяются серийные номера плат и
состояние OTA (только IDLE или COMMITTED; иначе ERROR без действий).
"""
import argparse
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

UI = "/run/sheregesh-ui/ui-state.json"
REMOTE_UNIT, CONSOLE_UNIT = "sheregesh-remote-net", "sheregesh-console"
SERIALS = {"console": "10000000568152de", "remote": "100000009d497e91"}     # pi1, pi2 (tools/deploy/shdeploy.py)
OTA_OK = ("IDLE", "COMMITTED")
FAULTS = ("pause", "stop-start", "restart", "sender-kill", "manual")
PAUSE_MAX_S = 10.0          # WatchdogSec=15 у sheregesh-remote-net: пауза дольше — уже сценарий watchdog
VERDICTS = ("PASS", "FAIL", "ERROR")

# Утверждённые пороги (docs/requirements/requirements.json). Ключ → (предел, с; источник).
LIMITS = {
    "detect_s": (5.0, "REQ-SYS-002: «нет связи» не позднее 5 с после отключения кабеля"),
    "link_back_s": (10.0, "REQ-SYS-002: обмен восстанавливается без ручных действий не позднее 10 с"),
    "video_off_s": (3.0, "REQ-SW-002: при обрыве потока «нет видео» не позднее 3 с"),
    "video_back_s": (10.0, "REQ-SW-002: отображение восстанавливается не позднее 10 с после возобновления"),
}
# Какие пороги применимы к модели отказа. restart: разрыв короче периода ui-state — обнаружение может не попасть
# в выборку, поэтому судится только восстановление. sender-kill: управление не рвётся.
APPLICABLE = {
    "pause": ("detect_s", "link_back_s", "video_off_s", "video_back_s"),
    "stop-start": ("detect_s", "link_back_s", "video_off_s", "video_back_s"),
    "manual": ("detect_s", "link_back_s", "video_off_s", "video_back_s"),
    "restart": ("link_back_s", "video_back_s"),
    "sender-kill": ("video_off_s", "video_back_s"),
}
EQUIVALENCE = {
    "manual": "физическое отключение кабеля — эквивалент условия REQ-SYS-002",
    "pause": "НЕ кабель: молчащий процесс, TCP жив на уровне ядра, несущая и адреса не пропадают",
    "stop-start": "НЕ кабель: служба остановлена, соединения закрыты и отвергаются (RST)",
    "restart": "НЕ кабель: штатный перезапуск службы (FIN)",
    "sender-kill": "НЕ кабель: обрыв только видеопотока при живом управлении",
}


def ssh(host, cmd, timeout=60):
    """(код, stdout). Сбой SSH — код ≠ 0, а не исключение: решает вызывающий."""
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", f"sheregesh-ui@{host}", cmd],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except subprocess.TimeoutExpired:
        return 124, ""


def out(host, cmd, timeout=60):
    return ssh(host, cmd, timeout)[1]


def jload(text):
    try:
        v = json.loads(text or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


# ---------------------------------------------------------------- выборка состояния GUI на пульте


class Sampler:
    """Один долгоживущий SSH к пульту: каждые 0,25 с — часы платы и ui-state.json. Разрешение по состоянию GUI —
    период записи файла (1 с) + 0,25 с; моменты отсчитываются по часам Mac (time.monotonic) при получении строки."""

    CMD = ("while :; do printf '%s ' \"$(date +%s.%N)\"; tr -d '\\n' < " + UI + " 2>/dev/null || printf '{}'; "
           "echo; sleep 0.25; done")

    def __init__(self, host):
        self.host, self.rows, self.lock, self.proc = host, [], threading.Lock(), None

    def start(self):
        self.proc = subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
                                      "-o", "ServerAliveInterval=2", f"sheregesh-ui@{self.host}", self.CMD],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        threading.Thread(target=self._read, daemon=True).start()
        return self

    def _read(self):
        for line in self.proc.stdout:
            now, _, rest = line.partition(" ")
            try:
                board = float(now)
            except ValueError:
                continue
            with self.lock:
                self.rows.append((time.monotonic(), board, jload(rest)))

    def stop(self):
        if self.proc:
            self.proc.terminate()

    def since(self, t0):
        with self.lock:
            return [r for r in self.rows if r[0] >= t0]

    def last(self):
        with self.lock:
            return self.rows[-1] if self.rows else None


def link_up(ui):
    return ui.get("peerLink") == "CONNECTED"


def video_up(ui):
    v = ui.get("video") or {}
    return v.get("mode") == "remote-tls" and v.get("state") == "playing" and (v.get("decoded_fps_5s") or 0) > 0


def first(rows, t0, pred):
    """Секунды от t0 до первой строки, где pred(ui) истинно; None — не наблюдалось."""
    for mono, _board, ui in rows:
        if mono >= t0 and ui and pred(ui):
            return round(mono - t0, 2)
    return None


def gui_stale_max(rows):
    """Наибольший возраст ui-state.json по часам платы: файл пишет таймер главного потока Qt раз в секунду —
    рост возраста означает блокировку цикла событий GUI (E2E-08). Порога в SRS нет — только измерение."""
    ages = [board - ui["time"] for _m, board, ui in rows if isinstance(ui.get("time"), (int, float))]
    return round(max(ages), 2) if ages else None


# ---------------------------------------------------------------- снимки плат


def board_identity(host):
    return {"host": host, "serial": out(host, "tr -d '\\0' </proc/device-tree/serial-number"),
            "package": out(host, "dpkg-query -W -f='${Version}' sheregesh"),
            "deploy_current": out(host, "readlink /var/lib/sheregesh-deploy/current || echo factory"),
            "build": jload(out(host, "cat /usr/share/sheregesh/build.json")),
            "ota": ota_state(host), "kernel": out(host, "uname -r"), "uptime_s": out(host, "cut -d' ' -f1 /proc/uptime")}


def ota_state(host):
    """Состояние агента OTA (только чтение): {state, active, consistent}; None — недоступно (это ERROR допуска)."""
    code, text = ssh(host, "sheregesh-ota-client status")
    if code != 0:
        return None
    d = jload(text)
    st = d.get("state")
    if isinstance(st, dict):                      # UpdateAgent.status(): "state": vars(UpdateState)
        return {"state": st.get("state"), "active": (d.get("slots") or {}).get("active"), "consistent": d.get("consistent")}
    m = re.search(r'"state"\s*:\s*"([A-Z_]+)"', text)
    return {"state": m.group(1), "active": None, "consistent": None} if m else None


PROC = ("for p in {pids}; do [ -d /proc/$p ] || continue; printf '%s %s %s %s\\n' $p "
        "\"$(awk '/^VmRSS/{{print $2}}' /proc/$p/status)\" \"$(awk '/^Threads/{{print $2}}' /proc/$p/status)\" "
        "\"$(ls /proc/$p/fd 2>/dev/null | wc -l)\"; done")
CONSOLE_PIDS = ("$(for p in $(pgrep -u sheregesh-ui -x python3); do tr '\\0' ' ' </proc/$p/cmdline | "
                "grep -qE 'sheregesh[-_]console' && echo $p; done)")


def resources(host, role):
    """RSS (кБ), потоки, дескрипторы приложения роли; число конвейеров GStreamer и сирот; NRestarts; failed."""
    unit = CONSOLE_UNIT if role == "console" else REMOTE_UNIT
    pids = CONSOLE_PIDS if role == "console" else f"$(systemctl show -p MainPID --value {REMOTE_UNIT})"
    pat = "rtpstreamdepay" if role == "console" else "rtpstreampay"
    text = out(host, f"echo P; {PROC.format(pids=pids)}; echo G; ps -eo pid,ppid,args --no-headers | "
                     f"grep -E 'gst-launch-1.0.*{pat}' | grep -v grep; echo S; "
                     f"systemctl show -p NRestarts -p ActiveState -p MainPID {unit} | tr '\\n' ' '; echo; "
                     "echo F; systemctl --failed --no-legend --plain | wc -l; echo M; "
                     "awk '/^MemAvailable/{print $2}' /proc/meminfo")
    sec, data = None, {"procs": [], "gst": [], "service": [], "failed": None, "mem_available_kb": None}
    for ln in text.splitlines():
        if ln in ("P", "G", "S", "F", "M"):
            sec = ln
            continue
        if sec == "P" and len(ln.split()) == 4:
            pid, rss, thr, fds = ln.split()
            data["procs"].append({"pid": pid, "rss_kb": int(rss or 0), "threads": int(thr or 0), "fds": int(fds or 0)})
        elif sec == "G" and ln.strip():
            data["gst"].append(ln.strip())
        elif sec == "S":
            data["service"] = ln.split()
        elif sec == "F" and ln.strip().isdigit():
            data["failed"] = int(ln)
        elif sec == "M" and ln.strip().isdigit():
            data["mem_available_kb"] = int(ln)
    data["gst_count"] = len(data["gst"])
    data["gst_orphans"] = [g for g in data["gst"] if len(g.split()) > 1 and g.split()[1] == "1"]
    svc = dict(kv.split("=", 1) for kv in data["service"] if "=" in kv)
    data["service"] = svc
    data["nrestarts"] = int(svc["NRestarts"]) if svc.get("NRestarts", "").isdigit() else None
    return data


def session_lines(host, since):
    """Строки журнала блока о новых сеансах управления и видео (E2E-12)."""
    text = out(host, f"journalctl -u {REMOTE_UNIT} --since @{int(since)} -o cat --no-pager | "
                     "grep -E 'session from|video: session|ended' | tail -50")
    return text.splitlines()


def console_journal(host, since):
    """Записи пульта в journald (селектор как check_pi_deploy.journal: процессы пульта — в сеансе PAM)."""
    sel = (f"_SYSTEMD_UNIT={CONSOLE_UNIT}.service + UNIT={CONSOLE_UNIT}.service + "
           "SYSLOG_IDENTIFIER=sheregesh-notify-relay + SYSLOG_IDENTIFIER=cage + SYSLOG_IDENTIFIER=sh")
    return out(host, f"journalctl --since @{int(since)} -o cat --no-pager {sel} | "
                     "grep -iE 'link|peer|video|связ' | tail -50").splitlines()


# ---------------------------------------------------------------- внесение и снятие отказа


class Fault:
    """inject() и restore() возвращают (ok, детали). restore() вызывается всегда (finally)."""

    def __init__(self, kind, remote, outage, prompt=input):
        self.kind, self.rh, self.outage, self.prompt, self.pid = kind, remote, outage, prompt, None

    def inject(self):
        rh = self.rh
        if self.kind == "pause":
            self.pid = out(rh, f"systemctl show -p MainPID --value {REMOTE_UNIT}")
            if not self.pid.isdigit() or self.pid == "0":
                return False, f"нет MainPID блока: {self.pid!r}"
            # Страховка на самой плате: SIGCONT через outage+3 с, даже если Mac или SSH пропадут.
            ssh(rh, f"nohup sh -c 'sleep {self.outage + 3:.0f}; kill -CONT {self.pid}' >/dev/null 2>&1 &")
            code, _ = ssh(rh, f"kill -STOP {self.pid}")
            return code == 0, f"SIGSTOP {self.pid}"
        if self.kind == "stop-start":
            ssh(rh, f"nohup sh -c 'sleep {self.outage + 10:.0f}; systemctl start {REMOTE_UNIT}' >/dev/null 2>&1 &")
            code, _ = ssh(rh, f"systemctl stop {REMOTE_UNIT}")
            return code == 0, "systemctl stop"
        if self.kind == "restart":
            code, _ = ssh(rh, f"systemctl restart {REMOTE_UNIT}")
            return code == 0, "systemctl restart"
        if self.kind == "sender-kill":
            pids = out(rh, "pgrep -u sheregesh-ui -f 'gst-launch-1.0.*rtpstreampay'").split()
            if not pids:
                return False, "нет генератора видео на pi2"
            code, _ = ssh(rh, f"kill -KILL {' '.join(pids)}")
            return code == 0, f"SIGKILL {pids}"
        if self.kind == "manual":
            self.prompt("ОТКЛЮЧИТЕ кабель Ethernet удалённого блока (pi2) и нажмите Enter в тот же момент… ")
            return True, "кабель отключён человеком (±1 с)"
        return False, f"неизвестная модель отказа {self.kind!r}"

    def restore(self):
        rh = self.rh
        if self.kind == "pause":
            code, text = ssh(rh, f"kill -CONT {self.pid} 2>/dev/null && echo resumed || "
                                 f"(test -d /proc/{self.pid} && echo stuck || echo gone)")
            return code == 0 and text in ("resumed", "gone"), text
        if self.kind == "stop-start":
            code, _ = ssh(rh, f"systemctl start {REMOTE_UNIT}")
            return code == 0, "systemctl start"
        if self.kind == "manual":
            self.prompt(f"Подождите не менее {self.outage:.0f} с, ПОДКЛЮЧИТЕ кабель обратно и нажмите Enter в тот же момент… ")
            return True, "кабель подключён человеком (±1 с)"
        return True, "снятие не требуется"

    @property
    def holds(self):
        """Отказ держится outage секунд (pause, stop-start, manual — время задаёт человек)."""
        return self.kind in ("pause", "stop-start")


# ---------------------------------------------------------------- суждение


def judge(fault, m, inv):
    """Вердикт цикла из измерений m и инвариантов inv. Чистая функция (тесты без плат)."""
    crit, errors, fails = {}, list(m.get("errors", [])), []
    for key in APPLICABLE[fault]:
        limit, source = LIMITS[key]
        v = m.get(key)
        if v is None:
            crit[key] = {"value": None, "limit": limit, "source": source, "result": "NOT_OBSERVED"}
            errors.append(f"{key}: событие не наблюдалось")
        else:
            ok = v <= limit
            crit[key] = {"value": v, "limit": limit, "source": source, "result": "PASS" if ok else "FAIL"}
            if not ok:
                fails.append(f"{key} = {v} с > {limit} с")
    for name, (ok, detail, severity) in inv.items():
        if not ok:
            (errors if severity == "ERROR" else fails).append(f"{name}: {detail}")
    verdict = "FAIL" if fails else "ERROR" if errors else "PASS"
    return {"verdict": verdict, "criteria": crit, "fails": fails, "errors": errors}


def invariants(fault, before, after, new_sessions):
    """Инварианты стенда (не пороги SRS): основание — протоколы S3.1/S3.1-VIDEO и устройство служб."""
    inv = {}
    for role in ("console", "remote"):
        b, a = before[role], after[role]
        if a is None or b is None:
            inv[f"{role}.snapshot"] = (False, "снимок недоступен", "ERROR")
            continue
        sev = "ERROR" if role == "remote" and fault == "pause" else "FAIL"     # watchdog в pause — сценарий нарушен
        inv[f"{role}.nrestarts"] = (a["nrestarts"] == b["nrestarts"], f"{b['nrestarts']} → {a['nrestarts']}", sev)
        inv[f"{role}.failed_units"] = (a["failed"] == 0, f"failed = {a['failed']}", "FAIL")
        inv[f"{role}.gst_pipelines"] = (a["gst_count"] == 1 and not a["gst_orphans"],
                                        f"конвейеров {a['gst_count']}, сирот {len(a['gst_orphans'])}", "FAIL")
    if fault != "sender-kill":
        inv["remote.journal_session"] = (any("session from" in s and "video" not in s for s in new_sessions),
                                         "нет новой записи «session from» в журнале блока", "FAIL")
    else:
        inv["remote.journal_video_session"] = (any("video: session from" in s for s in new_sessions),
                                               "нет новой записи «video: session from»", "FAIL")
    return inv


def summarize(cycles, snaps):
    counts = {v: sum(c["verdict"] == v for c in cycles) for v in VERDICTS}
    verdict = "FAIL" if counts["FAIL"] else "ERROR" if counts["ERROR"] or not cycles else "PASS"
    stats = {}
    for key in LIMITS:
        vals = [c["measured"][key] for c in cycles if c["measured"].get(key) is not None]
        if vals:
            stats[key] = {"n": len(vals), "min": min(vals), "max": max(vals), "median": sorted(vals)[len(vals) // 2]}
    growth = {}
    for role in ("console", "remote"):
        seq = [s[role] for s in snaps if s.get(role) and s[role]["procs"]]
        if len(seq) >= 2:
            f, l = seq[0]["procs"][0], seq[-1]["procs"][0]
            growth[role] = {"rss_kb": l["rss_kb"] - f["rss_kb"], "threads": l["threads"] - f["threads"],
                            "fds": l["fds"] - f["fds"], "samples": len(seq), "same_pid": f["pid"] == l["pid"],
                            "note": "измерение без утверждённого порога (E2E-11)"}
    return {"verdict": verdict, "counts": counts, "timing": stats, "resource_growth": growth}


# ---------------------------------------------------------------- прогон


def wait_ready(sampler, timeout):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        r = sampler.last()
        if r and link_up(r[2]) and video_up(r[2]):
            return True
        time.sleep(0.5)
    return False


def vsessions(ui):
    return (ui.get("video") or {}).get("sessions") or 0


def measure(rows, t0, back_from, s0):
    """Времена цикла по выборке ui-state (чистая функция). Восстановление видео засчитывается только по НОВОМУ
    видеосеансу (счётчик sessions вырос) — иначе ранняя строка «ещё играет» дала бы ложный ноль."""
    m = {"detect_s": first(rows, t0, lambda u: not link_up(u)),
         "video_off_s": first(rows, t0, lambda u: not video_up(u)), "link_back_s": None}
    if m["detect_s"] is not None:
        t_lost = t0 + m["detect_s"]
        m["link_back_s"] = first([r for r in rows if r[0] > t_lost], back_from, link_up)
    m["video_back_s"] = first(rows, back_from, lambda u: video_up(u) and vsessions(u) > s0)
    m["gui_stale_max_s"] = gui_stale_max(rows)
    m["samples"] = len(rows)
    return m


def run_cycle(i, a, sampler, fault):
    snap_b = {"console": resources(a.console, "console"), "remote": resources(a.remote, "remote")}
    last = sampler.last()
    s0 = vsessions(last[2]) if last else 0
    since, errors = time.time() - 1, []
    t0 = time.monotonic()
    ok, detail = fault.inject()
    if fault.kind == "manual":
        t0 = time.monotonic()                             # момент — Enter человека, а не показ подсказки
    try:
        if not ok:
            errors.append(f"отказ не внесён: {detail}")
        elif fault.holds:
            time.sleep(max(0.0, a.outage - (time.monotonic() - t0)))
    finally:
        rok, rdetail = fault.restore()                    # всегда: SIGCONT / start / подсказка человеку
    t_restore = time.monotonic()
    if not rok:
        errors.append(f"восстановление не подтверждено: {rdetail}")
    back_from = t0 if fault.kind in ("restart", "sender-kill") else t_restore
    deadline = time.monotonic() + a.recover_timeout
    while time.monotonic() < deadline:
        r = sampler.last()
        if r and r[0] > back_from and link_up(r[2]) and video_up(r[2]) and vsessions(r[2]) > s0:
            break
        time.sleep(0.5)
    rows = sampler.since(t0)
    m = {"inject": detail, "restore": rdetail, "video_sessions_before": s0, **measure(rows, t0, back_from, s0)}
    if fault.kind == "sender-kill" and not all(link_up(u) for _x, _y, u in rows if u):
        errors.append("управление прерывалось при обрыве только видео (инвариант ADR-0011)")
    m["errors"] = errors
    time.sleep(2)                                         # конвейер прошлого сеанса успевает завершиться
    snap_a = {"console": resources(a.console, "console"), "remote": resources(a.remote, "remote")}
    sess = session_lines(a.remote, since)
    inv = invariants(fault.kind, snap_b, snap_a, sess)
    res = judge(fault.kind, m, inv)
    return {"cycle": i, "fault": fault.kind, "equivalence": EQUIVALENCE[fault.kind], "measured": m,
            "invariants": {k: {"ok": v[0], "detail": v[1], "severity": v[2]} for k, v in inv.items()},
            "remote_journal": sess, **res}, snap_a


def preflight(a):
    ids, problems = {}, []
    for role, host in (("console", a.console), ("remote", a.remote)):
        ids[role] = board_identity(host)
        want = a.console_serial if role == "console" else a.remote_serial
        if ids[role]["serial"] != want:
            problems.append(f"{role}: серийный номер {ids[role]['serial']!r} ≠ {want!r}")
        ota = ids[role]["ota"] or {}
        if ota.get("state") not in OTA_OK or ota.get("consistent") is False:
            problems.append(f"{role}: состояние OTA {ota or None!r} не IDLE/COMMITTED (или несогласовано) — "
                            "отказы не вносятся")
    return ids, problems


def health(host, role):
    text = out(host, f"sheregesh-health --json --role {role} --link-config ~/.config/sheregesh/link.json", 120)
    d = jload(text)
    return {"verdict": d.get("verdict"), "not_pass": [c for c in d.get("checks", []) if c.get("status") != "PASS"]}


def plan(a):
    lines = [f"Модель отказа: {a.fault} — {EQUIVALENCE[a.fault]}",
             f"Циклов: {a.cycles}; обрыв: {a.outage if a.fault in ('pause', 'stop-start', 'manual') else '—'} с; "
             f"пауза между циклами: {a.gap} с",
             "Предпроверка (только чтение): серийные номера, OTA ∈ IDLE/COMMITTED, health обеих ролей, связь и видео.",
             "Пороги: " + "; ".join(f"{k} ≤ {LIMITS[k][0]} с ({LIMITS[k][1].split(':')[0]})" for k in APPLICABLE[a.fault]),
             "Инварианты: NRestarts пульта и блока неизменны, failed = 0, по одному конвейеру GStreamer без сирот, "
             "новая запись сеанса в журнале блока на каждый цикл.",
             "Измеряется без порога: возраст ui-state (живость GUI), RSS/потоки/дескрипторы, MemAvailable."]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console")
    ap.add_argument("--remote")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--fault", choices=FAULTS, required=True)
    ap.add_argument("--cycles", type=int, default=3)
    ap.add_argument("--outage", type=float, default=8.0, help="длительность обрыва для pause/stop-start, с")
    ap.add_argument("--gap", type=float, default=20.0, help="устойчивая работа между циклами, с")
    ap.add_argument("--recover-timeout", type=float, default=60.0)
    ap.add_argument("--console-serial", default=SERIALS["console"])
    ap.add_argument("--remote-serial", default=SERIALS["remote"])
    ap.add_argument("--plan", action="store_true", help="напечатать план и выйти, без SSH")
    a = ap.parse_args(argv)
    if a.fault == "pause" and a.outage > PAUSE_MAX_S:
        ap.error(f"--outage для pause не более {PAUSE_MAX_S} с (WatchdogSec блока 15 с)")
    if a.fault == "stop-start" and a.gap < 12:
        ap.error("--gap для stop-start не менее 12 с (отложенный start-страховка прошлого цикла)")
    if a.plan:
        print(plan(a))
        return 0
    if not (a.console and a.remote and a.out):
        ap.error("нужны --console, --remote и --out (или --plan)")
    a.out.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    report = {"tool": "e2e_run", "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "fault": a.fault,
              "equivalence": EQUIVALENCE[a.fault], "args": {k: str(v) for k, v in vars(a).items()}, "cycles": []}
    ids, problems = preflight(a)
    report["boards"] = ids
    report["health_before"] = {"console": health(a.console, "console"), "remote": health(a.remote, "remote")}
    problems += [f"health {r} до начала: {h['verdict']!r}" for r, h in report["health_before"].items()
                 if h["verdict"] != "PASS"]
    sampler = Sampler(a.console).start()
    snaps = []
    try:
        if not problems and not wait_ready(sampler, 90):
            problems.append("до начала: нет связи или видео на пульте за 90 с (или ui-state недоступен)")
        if problems:
            report.update(verdict="ERROR", errors=problems)
        else:
            snaps.append({"console": resources(a.console, "console"), "remote": resources(a.remote, "remote")})
            for i in range(1, a.cycles + 1):
                print(f"== цикл {i}/{a.cycles}: {a.fault}", flush=True)
                try:
                    c, snap = run_cycle(i, a, sampler, Fault(a.fault, a.remote, a.outage))
                except Exception as e:                        # noqa: BLE001 — сбой стенда — ERROR, не PASS
                    c, snap = {"cycle": i, "fault": a.fault, "measured": {}, "verdict": "ERROR",
                               "errors": [f"{type(e).__name__}: {e}"]}, {}
                report["cycles"].append(c)
                snaps.append(snap)
                (a.out / f"cycle-{i:02d}.json").write_text(json.dumps(c, ensure_ascii=False, indent=1))
                print(f"   {c['verdict']} {c.get('fails') or ''} {c.get('errors') or ''}", flush=True)
                if any("восстановление не подтверждено" in e for e in c.get("errors", [])):
                    report["aborted"] = f"цикл {i}: стенд не восстановлен — остальные циклы не выполняются"
                    break
                if i < a.cycles:
                    time.sleep(a.gap)
            report.update(summarize(report["cycles"], snaps))
    finally:
        sampler.stop()
    report["resources"] = snaps
    report["health_after"] = {"console": health(a.console, "console"), "remote": health(a.remote, "remote")}
    report["console_journal"] = console_journal(a.console, t_start)
    report["remote_journal_count"] = len(session_lines(a.remote, t_start))
    if report.get("verdict") != "ERROR":
        bad = [r for r, h in report["health_after"].items() if h["verdict"] != "PASS"]
        if bad and report["verdict"] == "PASS":
            report["verdict"] = "FAIL"
            report.setdefault("fails", []).append(f"health после прогона не PASS: {bad}")
    report["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    (a.out / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(json.dumps({k: report.get(k) for k in ("verdict", "counts", "timing", "aborted", "errors")}, ensure_ascii=False))
    return 0 if report.get("verdict") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
