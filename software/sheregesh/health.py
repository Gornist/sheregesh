# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""sheregesh-health — диагностическая проверка здоровья платы (S3.1, ADR-0006 — подготовка к mark-good).

  sheregesh-health [--json] [--out FILE]

Только чтение. На этом этапе НЕ переключает загрузочные разделы, НЕ выполняет mark-good и НЕ перезагружает плату.
Проверки: обязательные службы роли и неуспешные юниты; конфигурация (сетевой профиль, данные пульта); раздел
persistent (смонтирован, запись, свободное место); ошибки ФС в журнале ядра; питание и температура; состояние
защищённого канала pi1↔pi2; совместимость версий; действительность сертификатов и права ключа.
Итог — JSON: каждая проверка PASS | WARN | FAIL | SKIP с причиной; общий вердикт — FAIL при любом FAIL.
Код выхода: 0 — без FAIL, 1 — есть FAIL.
"""
import argparse
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import PROTOCOL_VERSION, __version__
from .link import tls
from .protocol import negotiate

UNITS = {"console": ["sheregesh-console", "seatd"], "remote": ["sheregesh-remote-net"],
         "standalone": ["sheregesh-console", "sheregesh-remote-emu", "seatd"]}
STATUS_FILES = {"console": "/run/sheregesh-ui/link-status.json", "remote": "/run/sheregesh-remote/link-status.json"}
MIN_FREE_MB, MIN_FREE_PCT = 100, 10
TEMP_WARN_C, TEMP_FAIL_C = 70.0, 80.0
CERT_WARN_DAYS = 30
STATUS_MAX_AGE_S = 15


def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)


class Report:
    def __init__(self):
        self.checks = []

    def add(self, cid, status, detail, **data):
        self.checks.append({"id": cid, "status": status, "detail": detail, **({"data": data} if data else {})})


def check_services(rep, role, runner=run):
    for unit in UNITS[role]:
        rc, out, _ = runner(["systemctl", "show", "-p", "ActiveState,SubState,NRestarts,Result", f"{unit}.service"])
        kv = dict(x.split("=", 1) for x in out.splitlines() if "=" in x)
        ok = kv.get("ActiveState") == "active"
        rep.add(f"service.{unit}", "PASS" if ok else "FAIL",
                f"{kv.get('ActiveState', '?')}/{kv.get('SubState', '?')}, перезапусков {kv.get('NRestarts', '?')}", **kv)
    rc, out, err = runner(["systemctl", "--failed", "--no-legend", "--plain"])
    if rc:
        rep.add("service.failed-units", "SKIP", f"systemctl недоступен: {err[-80:]}")
        return
    failed = [x.split()[0] for x in out.splitlines() if x.strip()]
    rep.add("service.failed-units", "FAIL" if failed else "PASS", ", ".join(failed) or "нет неуспешных юнитов")


def check_config(rep, cfg_path, data_dir):
    cfg = None
    if Path(cfg_path).expanduser().exists():
        try:
            cfg = tls.LinkConfig.load(cfg_path)
            rep.add("config.link", "PASS", f"{cfg.own_uri} → {cfg.peer_uri}, порт {cfg.port}")
        except tls.LinkConfigError as e:
            rep.add("config.link", "FAIL", str(e))
    else:
        rep.add("config.link", "SKIP", f"нет {cfg_path}: сетевой профиль не настроен (только loopback)")
    if data_dir:
        active = Path(data_dir) / "config" / "active"
        if Path(data_dir).exists():
            ok = active.exists() or any((Path(data_dir) / "config").glob("r*"))
            rep.add("config.console", "PASS" if ok else "WARN",
                    "ревизия конфигурации пульта есть" if ok else f"нет активной ревизии в {data_dir}/config")
    return cfg


def check_persistent(rep, mount="/persistent", mounts_file="/proc/mounts"):
    try:
        mounts = {x.split()[1]: x.split() for x in Path(mounts_file).read_text().splitlines()}
    except OSError:
        mounts = {}
    m = mounts.get(mount)
    if not m:
        rep.add("storage.persistent", "SKIP" if not Path("/proc/device-tree/model").exists() else "FAIL",
                f"{mount} не смонтирован")
        return
    rw = "rw" in m[3].split(",")
    st = shutil.disk_usage(mount)
    free_mb, pct = st.free // 2**20, 100 * st.free / st.total
    bad = not rw or free_mb < MIN_FREE_MB or pct < MIN_FREE_PCT
    rep.add("storage.persistent", "FAIL" if bad else "PASS",
            f"{m[0]} {m[2]} {'rw' if rw else 'ro'}, свободно {free_mb} МБ ({pct:.0f} %)", free_mb=free_mb, pct=round(pct, 1))
    rootro = mounts.get("/", ["", "", "", ""])[3].split(",")
    rep.add("storage.root-ro", "PASS" if "ro" in rootro else "WARN", "корень только для чтения" if "ro" in rootro
            else "корень смонтирован на запись")


def check_fs_errors(rep, runner=run):
    rc, out, err = runner(["journalctl", "-k", "-b", "--no-pager", "-o", "cat", "-g",
                           "EXT4-fs error|EXT4-fs warning|I/O error|FAT-fs .*error|mmc.*(error|timeout)"])
    if rc not in (0, 1):
        rep.add("storage.fs-errors", "SKIP", f"журнал ядра недоступен: {err[-120:]}")
        return
    lines = [x for x in out.splitlines() if x.strip() and "No entries" not in x]
    rep.add("storage.fs-errors", "FAIL" if lines else "PASS", lines[-1][:160] if lines else "ошибок ФС в журнале нет",
            count=len(lines))


def check_power(rep, runner=run):
    rc, out, _ = runner(["vcgencmd", "get_throttled"])
    if rc:
        rep.add("power.throttled", "SKIP", "vcgencmd недоступен (не Raspberry Pi)")
    else:
        v = int(out.split("=")[1], 16)
        now, past = v & 0xF, (v >> 16) & 0xF
        status = "FAIL" if now & 0x1 else "WARN" if (now or past) else "PASS"
        names = {0: "недонапряжение", 1: "ограничение частоты", 2: "троттлинг", 3: "мягкий температурный предел"}
        rep.add("power.throttled", status, f"{out}: сейчас {[names[i] for i in range(4) if now >> i & 1] or 'норма'}, "
                f"с загрузки {[names[i] for i in range(4) if past >> i & 1] or 'норма'}", raw=out)
    rc, out, _ = runner(["vcgencmd", "measure_temp"])
    if rc:
        try:
            t = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
        except (OSError, ValueError):
            rep.add("power.temperature", "SKIP", "температура недоступна")
            return
    else:
        t = float(out.split("=")[1].split("'")[0])
    rep.add("power.temperature", "FAIL" if t >= TEMP_FAIL_C else "WARN" if t >= TEMP_WARN_C else "PASS",
            f"SoC {t:.1f} °C (WARN ≥ {TEMP_WARN_C:.0f}, FAIL ≥ {TEMP_FAIL_C:.0f})", celsius=t)


def check_link(rep, role, status_file, now=None):
    now = now or time.time()
    try:
        st = json.loads(Path(status_file).read_text())
    except (OSError, ValueError):
        rep.add("link.state", "FAIL", f"нет состояния канала {status_file} (служба не пишет его)")
        return None
    age = now - st.get("time", 0)
    if age > STATUS_MAX_AGE_S:
        rep.add("link.state", "FAIL", f"состояние канала устарело на {age:.0f} с — служба зависла или остановлена")
        return st
    if role == "console":
        ok = st.get("phase") == "connected" and st.get("secure") and st.get("tls") == "TLSv1.3"
        rep.add("link.state", "PASS" if ok else "FAIL",
                f"{st.get('phase')} ({st.get('detail')}), {st.get('tls') or 'без TLS'}, {st.get('host')}:{st.get('port')}, "
                f"heartbeat {st.get('heartbeat_age')} с", phase=st.get("phase"), tls=st.get("tls"))
        peer = st.get("peer") or {}
    else:
        ok = st.get("connected") and st.get("tls")
        p = st.get("peer") or {}
        rep.add("link.state", "PASS" if ok else "FAIL",
                f"{'сессия с ' + str(p.get('addr')) + ' ' + str(p.get('tls')) if st.get('connected') else 'нет сессии'}, "
                f"отказов аутентификации {st.get('stats', {}).get('auth_failures', 0)}", connected=st.get("connected"))
        peer = {"proto": st.get("session")}
    if peer.get("proto"):
        n = negotiate(PROTOCOL_VERSION, peer.get("proto"), peer.get("software"), peer.get("hardware"))
        rep.add("link.versions", "PASS" if n.compatible else "FAIL",
                f"локально {PROTOCOL_VERSION} / {__version__}, вторая сторона {peer.get('proto')} "
                f"{peer.get('software') or ''}: {n.reason}, сессия {n.session}")
    else:
        rep.add("link.versions", "SKIP", "версия второй стороны неизвестна (нет сессии)")
    return st


def check_certs(rep, cfg, now=None):
    if cfg is None:
        rep.add("certs.device", "SKIP", "сетевой профиль не настроен")
        return
    now = now or datetime.now(timezone.utc)
    for name, path in (("device", cfg.cert), ("ca", cfg.ca)):
        info = tls.cert_info(path)
        after = datetime.fromtimestamp(ssl.cert_time_to_seconds(info["notAfter"]), timezone.utc)
        before = datetime.fromtimestamp(ssl.cert_time_to_seconds(info["notBefore"]), timezone.utc)
        days = (after - now).days
        status = "FAIL" if not (before <= now <= after) else "WARN" if days < CERT_WARN_DAYS else "PASS"
        rep.add(f"certs.{name}", status, f"действителен до {info['notAfter']} (осталось {days} дн.)"
                + (f", URI {tls.cert_uris(info)}" if name == "device" else ""), days_left=days)
    mode = oct(os.stat(cfg.key).st_mode & 0o777)
    rep.add("certs.key-mode", "PASS" if mode == "0o600" else "FAIL", f"{cfg.key}: {mode}")


def collect(role=None, link_config=tls.DEFAULT_CONFIG, data_dir="/var/lib/sheregesh", runner=run):
    rep = Report()
    cfg = check_config(rep, link_config, data_dir)
    role = role or (cfg.role if cfg else "standalone")
    check_services(rep, role, runner)
    check_persistent(rep)
    check_fs_errors(rep, runner)
    check_power(rep, runner)
    if cfg is not None:
        check_link(rep, role, STATUS_FILES[role])
    else:
        rep.add("link.state", "SKIP", "сетевой профиль не настроен — канал pi1↔pi2 не проверяется")
    check_certs(rep, cfg)
    counts = {s: sum(c["status"] == s for c in rep.checks) for s in ("PASS", "WARN", "FAIL", "SKIP")}
    serial = ""
    try:
        serial = Path("/proc/device-tree/serial-number").read_text().strip("\0\n ")
    except OSError:
        pass
    return {"tool": "sheregesh-health", "mode": "diagnostic (no mark-good, no slot switch, no reboot)",
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "role": role, "serial": serial,
            "software": __version__, "protocol": PROTOCOL_VERSION, "checks": rep.checks, "counts": counts,
            "verdict": "FAIL" if counts["FAIL"] else "PASS"}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sheregesh-health", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="полный отчёт в JSON")
    ap.add_argument("--out", help="записать JSON в файл")
    ap.add_argument("--role", choices=list(UNITS))
    ap.add_argument("--link-config", default=str(tls.DEFAULT_CONFIG))
    a = ap.parse_args(argv)
    r = collect(a.role, a.link_config)
    text = json.dumps(r, ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).write_text(text)
    if a.json:
        print(text)
    else:
        for c in r["checks"]:
            print(f"[{c['status']:<4}] {c['id']}: {c['detail']}")
        print(f"Итог: {r['verdict']} {r['counts']}")
    return 1 if r["verdict"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
