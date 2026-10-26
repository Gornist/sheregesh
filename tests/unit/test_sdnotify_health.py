# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""sd_notify (ADR-0002) и диагностический sheregesh-health (S3.1)."""
import json
import os
import shutil
import socket
import tempfile
import time

import pytest

from sheregesh import health, sdnotify


@pytest.fixture
def notify_socket(monkeypatch):
    d = tempfile.mkdtemp(prefix="sd", dir="/tmp")             # путь AF_UNIX короткий и на macOS
    path = os.path.join(d, "n")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(path)
    s.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    yield s
    s.close()
    shutil.rmtree(d)


def test_ready_ping_and_watchdog_interval(notify_socket, monkeypatch):
    assert sdnotify.ready("up")
    assert notify_socket.recv(256) == b"READY=1\nSTATUS=up"
    assert sdnotify.ping() and notify_socket.recv(64) == b"WATCHDOG=1"
    monkeypatch.setenv("WATCHDOG_USEC", "20000000")
    assert sdnotify.watchdog_interval() == 10.0
    monkeypatch.setenv("WATCHDOG_PID", "1")                   # watchdog другого процесса — не наш
    assert sdnotify.watchdog_interval() is None


def test_outside_systemd_is_noop(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert sdnotify.ready() is False and sdnotify.watchdog_interval() is None


def fake_runner(units_state="active", failed="", throttled="throttled=0x0", temp="temp=48.2'C", journal=""):
    def run(cmd, timeout=10):
        if cmd[:2] == ["systemctl", "show"]:
            return 0, f"ActiveState={units_state}\nSubState=running\nNRestarts=0\nResult=success", ""
        if cmd[:2] == ["systemctl", "--failed"]:
            return 0, failed, ""
        if cmd[:2] == ["vcgencmd", "get_throttled"]:
            return 0, throttled, ""
        if cmd[:2] == ["vcgencmd", "measure_temp"]:
            return 0, temp, ""
        if cmd[0] == "journalctl":
            return (0 if journal else 1), journal, ""
        return 127, "", "нет"
    return run


def test_services_power_and_fs_checks():
    rep = health.Report()
    health.check_services(rep, "remote", fake_runner())
    health.check_power(rep, fake_runner(throttled="throttled=0x50005", temp="temp=81.0'C"))
    health.check_fs_errors(rep, fake_runner(journal="EXT4-fs error (device mmcblk0p6): bad block"))
    by = {c["id"]: c for c in rep.checks}
    assert by["service.sheregesh-remote-net"]["status"] == "PASS"
    assert by["power.throttled"]["status"] == "FAIL" and "недонапряжение" in by["power.throttled"]["detail"]
    assert by["power.temperature"]["status"] == "FAIL"
    assert by["storage.fs-errors"]["status"] == "FAIL"
    rep = health.Report()
    health.check_services(rep, "console", fake_runner(units_state="failed", failed="console-setup.service loaded failed"))
    assert {c["status"] for c in rep.checks} == {"FAIL"}


def test_link_state_fresh_stale_and_versions(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"time": time.time(), "phase": "connected", "secure": True, "tls": "TLSv1.3", "host": "h",
                             "port": 7443, "heartbeat_age": 0.4, "detail": "ok",
                             "peer": {"proto": "1.1", "software": "0.1.0", "hardware": "HW-1.0 Rev A"}}))
    rep = health.Report()
    health.check_link(rep, "console", f)
    assert [c["status"] for c in rep.checks] == ["PASS", "PASS"]
    rep = health.Report()
    health.check_link(rep, "console", f, now=time.time() + 60)
    assert rep.checks[0]["status"] == "FAIL" and "устарело" in rep.checks[0]["detail"]
    f.write_text(json.dumps({"time": time.time(), "connected": False, "tls": True, "stats": {"auth_failures": 3}}))
    rep = health.Report()
    health.check_link(rep, "remote", f)
    assert rep.checks[0]["status"] == "FAIL" and "3" in rep.checks[0]["detail"]


@pytest.mark.skipif(not shutil.which("openssl"), reason="нет openssl")
def test_certificate_validity_and_key_mode(tmp_path):
    from datetime import datetime, timedelta, timezone

    from sheregesh.link import pki
    pki.ca_init(tmp_path / "ca")
    d = tmp_path / "dev"
    pki.install(d, pki.ca_sign(tmp_path / "ca", pki.keygen("remote", "X1", d), "remote", "X1"),
                (tmp_path / "ca" / "ca.crt").read_text(), "remote", "X1")
    cfg = pki.configure(tmp_path / "c" / "link.json", "remote", "X1", "console:Y2", [], None, d)
    assert (tmp_path / "c" / "remote.role").exists() and not (tmp_path / "c" / "console.role").exists()
    rep = health.Report()
    health.check_certs(rep, cfg)
    assert {c["status"] for c in rep.checks} == {"PASS"}
    rep = health.Report()
    health.check_certs(rep, cfg, now=datetime.now(timezone.utc) + timedelta(days=350))
    assert rep.checks[0]["status"] == "WARN"                    # осталось < 30 дней
    rep = health.Report()
    health.check_certs(rep, cfg, now=datetime.now(timezone.utc) + timedelta(days=400))
    assert rep.checks[0]["status"] == "FAIL"


def test_report_is_diagnostic_only(tmp_path):
    r = health.collect(role="remote", link_config=tmp_path / "none.json", data_dir=None, runner=fake_runner())
    assert r["mode"].startswith("diagnostic") and "mark-good" in r["mode"]
    assert {"checks", "counts", "verdict", "software", "protocol"} <= r.keys()


def test_networked_role_without_profile_is_fail_not_skip(tmp_path):
    """Роль задана (маркер или --role), профиля нет: конфигурация, канал и сертификаты — FAIL, а не SKIP."""
    (tmp_path / "remote.role").write_text("urn:sheregesh:remote:X\n")
    r = health.collect(link_config=tmp_path / "link.json", data_dir=None, runner=fake_runner())
    st = {c["id"]: c["status"] for c in r["checks"]}
    assert r["role"] == "remote" and r["networked"]
    assert st["config.link"] == st["link.state"] == st["certs.device"] == "FAIL" and r["verdict"] == "FAIL"
    (tmp_path / "remote.role").unlink()
    r = health.collect(link_config=tmp_path / "link.json", data_dir=None, runner=fake_runner())
    st = {c["id"]: c["status"] for c in r["checks"]}
    assert not r["networked"] and st["config.link"] == st["certs.device"] == "SKIP"    # автономный режим


def test_remote_versions_use_console_hello_not_session_id(tmp_path):
    """Блок: совместимость — по протоколу из hello пульта; согласованная сессия — не версия второй стороны."""
    f = tmp_path / "r.json"
    base = {"time": time.time(), "connected": True, "tls": True, "session": "1.1", "stats": {}}
    f.write_text(json.dumps({**base, "peer": {"addr": "192.168.1.91", "tls": "TLSv1.3",
                                              "hello": {"proto": "1.1", "software": "0.1.0", "hardware": "HW-1.0 Rev A"}}}))
    rep = health.Report()
    health.check_link(rep, "remote", f)
    v = next(c for c in rep.checks if c["id"] == "link.versions")
    assert v["status"] == "PASS" and "вторая сторона 1.1" in v["detail"]
    f.write_text(json.dumps({**base, "peer": {"addr": "192.168.1.91", "hello": {"proto": "2.0"}}}))
    rep = health.Report()
    health.check_link(rep, "remote", f)
    assert next(c for c in rep.checks if c["id"] == "link.versions")["status"] == "FAIL"     # MAJOR не совпадает
    f.write_text(json.dumps({**base, "peer": {"addr": "192.168.1.91"}}))                     # блок без hello в статусе
    rep = health.Report()
    health.check_link(rep, "remote", f)
    v = next(c for c in rep.checks if c["id"] == "link.versions")
    assert v["status"] == "WARN" and v["data"]["source"] == "session"
