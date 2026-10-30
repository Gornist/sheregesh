# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Критерии подтверждения нового слота (S3.2-OTA): одного факта загрузки мало.

Агент OTA вызывает gates() на пробной загрузке нового слота перед mark-good. Ожидаемые значения фиксируются при
установке (expect_from_board) — до переключения, на проверенном слоте: роль платы, machine-id, отпечатки ключа и
сертификата устройства, ключа хоста SSH. Любая обязательная проверка не PASS → слот НЕ подтверждается.

Обязательные для всех ролей: слот и пробная загрузка, версия образа, persistent rw с общими /home и /etc/ssh,
те же machine-id/ключи/сертификат/роль, ssh active, сокет агента OTA active, нет failed-юнитов, плата работает
не меньше MIN_UPTIME_S (вызовы watchdog успели пройти), sheregesh-health verdict PASS.
По ролям: console — sheregesh-console active без перезапусков, канал mTLS connected, (стенд) видео remote-tls
playing ≥ 25 к/с; remote — sheregesh-remote-net active без перезапусков, сеанс с пультом, видео пульту идёт: TLS-сеанс
видеосервера с кадрами или служба sheregesh-video-tx streaming по SRTP к пульту сеанса управления (ADR-0016);
standalone — эмулятор и пульт active. Сетевые проверки health (config.link, link.state, certs.*) — PASS, не SKIP.
"""
import hashlib
import json
import subprocess
import time
from pathlib import Path

MIN_UPTIME_S = 90
HOME = Path("/home/sheregesh-ui")
LINK = HOME / ".local/share/sheregesh/link"
CONFIG = HOME / ".config/sheregesh"
ROLE_UNITS = {"console": ["sheregesh-console"], "remote": ["sheregesh-remote-net"],
              "standalone": ["sheregesh-remote-emu", "sheregesh-console"]}
NET_CHECKS = ("config.link", "link.state", "certs.device", "certs.ca", "certs.key-mode")
VIDEO_STATE = "/run/sheregesh-video/state.json"     # состояние службы видео (VIDEO-SERVICE-CONTRACT §4)


def _sha(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def role(config_dir=CONFIG):
    return next((r for r in ("console", "remote") if (Path(config_dir) / f"{r}.role").exists()), "standalone")


def expect_from_board(root=Path("/")):
    """Отпечатки, которые должны пережить переключение слота (снимаются на рабочем слоте при установке)."""
    r = Path(root)
    return {"role": role(r / CONFIG.relative_to("/")),
            "machine_id": (r / "etc/machine-id").read_text().strip() if (r / "etc/machine-id").exists() else None,
            "device_key": _sha(r / LINK.relative_to("/") / "device.key"),
            "device_crt": _sha(r / LINK.relative_to("/") / "device.crt"),
            "link_json": _sha(r / CONFIG.relative_to("/") / "link.json"),
            "ssh_host_key": _sha(r / "etc/ssh/ssh_host_ed25519_key.pub"),
            "authorized_keys": _sha(r / HOME.relative_to("/") / ".ssh/authorized_keys")}


class Probe:
    """Факты платы. Подменяется в испытаниях (без systemd и без корня)."""

    def __init__(self, root=Path("/"), run=subprocess.run):
        self.root, self.run = Path(root), run

    def sh(self, *cmd):
        r = self.run(list(cmd), capture_output=True, text=True, timeout=60)
        return r.returncode, (r.stdout or "").strip()

    def unit(self, name):
        _, out = self.sh("systemctl", "show", "-p", "ActiveState,SubState,NRestarts,Result", name)
        return dict(x.split("=", 1) for x in out.splitlines() if "=" in x)

    def failed_units(self):
        rc, out = self.sh("systemctl", "--failed", "--no-legend", "--plain")
        return [x.split()[0] for x in out.splitlines() if x.strip()] if rc == 0 else ["(systemctl недоступен)"]

    def mount_opts(self, target):
        rc, out = self.sh("findmnt", "-no", "SOURCE,OPTIONS", target)
        return out if rc == 0 else ""

    def uptime(self):
        try:
            return float((self.root / "proc/uptime").read_text().split()[0])
        except (OSError, ValueError):
            return 0.0

    def health(self, role_name):
        # Агент работает от root: «~» в пути профиля по умолчанию указал бы на /root (pi2, 2026-10-24: health FAIL
        # у здоровой платы). Профиль и сертификаты — у пользователя стенда, путь передаётся явно.
        args = ("--role", role_name, "--link-config", str(self.root / CONFIG.relative_to("/") / "link.json")) \
            if role_name != "standalone" else ()
        rc, out = self.sh("/usr/bin/sheregesh-health", "--json", *args)
        try:
            return json.loads(out)
        except ValueError:
            return {"verdict": "ERROR"}

    def json_file(self, path):
        try:
            return json.loads((self.root / str(path).lstrip("/")).read_text())
        except (OSError, ValueError):
            return {}

    def build(self):
        return self.json_file("/usr/share/sheregesh/build.json")

    def now_expect(self):
        return expect_from_board(self.root)


def gates(layout, state, probe: Probe, lab_video=True):
    """→ (ok, [{"id", "status", "detail", "mandatory"}]). state — UpdateState с target, version_target, expect."""
    checks = []

    def add(cid, ok, detail, mandatory=True):
        checks.append({"id": cid, "status": "PASS" if ok else "FAIL", "detail": detail, "mandatory": mandatory})

    exp = getattr(state, "expect", None) or {}
    add("slot", layout.tryboot and layout.active.name.upper() == state.target,
        f"активен {layout.active.name.upper()}, пробная={layout.tryboot}, ожидается {state.target}")
    b = probe.build()
    add("version", b.get("ota_version") == state.version_target,
        f"образ {b.get('ota_version')} ({str(b.get('commit'))[:12]}), ожидается {state.version_target}")
    pers, home, ssh_dir = probe.mount_opts("/persistent"), probe.mount_opts("/home"), probe.mount_opts("/etc/ssh")
    add("persistent", ",rw" in f",{pers.split(' ', 1)[-1]}" and "/home" in home and "/shared/etc/ssh" in ssh_dir,
        f"/persistent {pers!r}; /home {home!r}; /etc/ssh {ssh_dir!r}")
    now = probe.now_expect()
    keys = ("role", "machine_id", "device_key", "device_crt", "link_json", "ssh_host_key", "authorized_keys")
    if not exp:
        # Установку выполнил агент образа без снятия отпечатков (57e8fe4 — первая установка на платах): сравнивать не
        # с чем. Обязательна целостность — всё на месте; сравнение «до/после» — по снимку процедуры (preflight).
        always = ("machine_id", "ssh_host_key", "authorized_keys")
        for k in keys:
            need = k in always or (now.get("role") in ("console", "remote") and k != "role")
            add(f"present.{k}", now.get(k) is not None or not need,
                f"{'есть' if now.get(k) is not None else 'нет'} (отпечатков установки нет — установка агентом старого "
                f"образа; сравнение с снимком «до» выполняет процедура)", mandatory=need)
    for k in keys if exp else ():
        if exp.get(k) is None and k not in ("role",):
            add(f"same.{k}", now.get(k) is None, "не было до обновления — нет и после" if now.get(k) is None
                else "появилось после обновления", mandatory=k in ("machine_id", "ssh_host_key", "authorized_keys"))
            continue
        add(f"same.{k}", now.get(k) == exp.get(k), "совпадает с отпечатком до обновления" if now.get(k) == exp.get(k)
            else f"изменилось: было {str(exp.get(k))[:16]}, стало {str(now.get(k))[:16]}")
    r = exp.get("role") or now.get("role")
    for u in ("ssh", "sheregesh-ota.socket"):
        st = probe.unit(u)
        add(f"unit.{u}", st.get("ActiveState") == "active", f"{st.get('ActiveState')}/{st.get('SubState')}")
    for u in ROLE_UNITS[r]:
        st = probe.unit(f"{u}.service")
        add(f"unit.{u}", st.get("ActiveState") == "active" and st.get("NRestarts") == "0",
            f"{st.get('ActiveState')}/{st.get('SubState')}, перезапусков {st.get('NRestarts')} (READY: Type=notify active)")
    failed = probe.failed_units()
    add("failed-units", not failed, ", ".join(failed) or "нет")
    up = probe.uptime()
    add("uptime", up >= MIN_UPTIME_S, f"{up:.0f} с (не меньше {MIN_UPTIME_S} с: watchdog служб успел сработать бы)")
    h = probe.health(r)
    st = {c["id"]: c["status"] for c in h.get("checks", [])}
    add("health", h.get("verdict") == "PASS", f"verdict {h.get('verdict')}, {h.get('counts')}")
    if r in ("console", "remote"):
        bad = [f"{c}={st.get(c, 'нет')}" for c in NET_CHECKS if st.get(c) != "PASS"]
        add("health.network", not bad, ", ".join(bad) or "config.link, link.state, certs.* — PASS")
    if r == "console":
        ls = probe.json_file("/run/sheregesh-ui/link-status.json")
        add("mtls", ls.get("phase") == "connected" and ls.get("tls") == "TLSv1.3",
            f"{ls.get('phase')} {ls.get('tls')} {ls.get('peer_uri')}")
        if lab_video:
            v = probe.json_file("/run/sheregesh-ui/ui-state.json").get("video") or {}
            add("video", v.get("mode") == "remote-srtp" and v.get("state") == "playing"
                and (v.get("decoded_fps_5s") or 0) >= 25, f"{v.get('mode')} {v.get('state')} {v.get('decoded_fps_5s')} к/с")
    elif r == "remote":
        ls = probe.json_file("/run/sheregesh-remote/link-status.json")
        add("mtls", bool(ls.get("connected")) and bool(ls.get("tls")),
            f"сеанс {'есть' if ls.get('connected') else 'нет'}, {(ls.get('peer') or {}).get('addr')}")
        if lab_video:
            # ADR-0016: видео через службу tx по SRTP. Кадров служба tx не считает — признак потока: RTP-пакеты
            # отправлены пульту того же сеанса управления (адрес peer state.json = адрес сеанса mTLS).
            tx = probe.json_file(VIDEO_STATE)
            m = (tx.get("streams") or {}).get("main") or {} if tx.get("role") == "tx" else {}
            packets = (m.get("rtp") or {}).get("packets")
            peer = (ls.get("peer") or {}).get("addr")
            srtp = (m.get("state") == "streaming" and m.get("transport") == "srtp" and peer is not None
                    and m.get("peer") == peer and isinstance(packets, int) and packets > 0)
            add("video", srtp,
                f"служба tx {m.get('state') or 'нет'} "
                f"{m.get('transport') or '-'} → {m.get('peer') or '-'}, RTP-пакетов {packets}")
    ok = all(c["status"] == "PASS" for c in checks if c["mandatory"])
    return ok, checks


def wait_for_gates(layout, state, probe, timeout=0, step=10):
    """Повторять проверки до timeout секунд (связь и видео поднимаются после загрузки)."""
    deadline = time.monotonic() + timeout
    while True:
        ok, checks = gates(layout, state, probe)
        if ok or time.monotonic() >= deadline:
            return ok, checks
        time.sleep(step)

