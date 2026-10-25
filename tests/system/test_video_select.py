# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S4: выбор видеоисточника блока пультом (протокол 1.2, video-select) через настоящий процесс блока и TLS-канал.

Список status.video.sources, выбор известного и неизвестного id, сохранение в video-input.json и перечитывание после
перезапуска блока, 20 переключений synthetic ↔ rtsp без утечек (процессы конвейера, дескрипторы, дочерние процессы),
совместимость: старый блок (сессия 1.1) — пульт не отправляет; старый пульт (hello 1.1) — блок не исполняет.
Конвейер — подставной (tests/system/fake_ingest_sender.py), сервер RTSP — TCP-заглушка (проверка присутствия)."""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from procs import Agent, free_port, wait_for
from sheregesh import videolink
from sheregesh.link.client import PeerClient
from sheregesh.link.codec import Decoder, encode

SENDER = Path(__file__).with_name("fake_ingest_sender.py")


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    from sheregesh.link import pki
    if not shutil.which("openssl"):
        pytest.skip("нет openssl")
    root = tmp_path_factory.mktemp("vsel")
    ca = root / "ca"
    pki.ca_init(ca)

    def device(name, role, did, peer):
        d = root / name
        cert = pki.ca_sign(ca, pki.keygen(role, did, d), role, did)
        pki.install(d, cert, (ca / "ca.crt").read_text(), role, did)
        return pki.configure(root / f"{name}.json", role, did, peer, ["127.0.0.1"], None, d)
    return {"remote": device("remote", "remote", "PI2", "console:PI1"),
            "console": device("console", "console", "PI1", "remote:PI2")}


class FakeRTSP:
    """TCP-сервер «камеры»: принимает и закрывает соединения (проверка присутствия RTSPSource и списка)."""

    def __init__(self):
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            c.close()

    def close(self):
        self.sock.close()


class Unit:
    """Блок (сетевой профиль, подставной конвейер) + пульт (PeerClient) + видеосеанс (VideoClient)."""

    def __init__(self, lab, tmp, *extra, video=True):
        self.tmp, self.lab = tmp, lab
        self.link = tmp / "link.json"
        if not self.link.exists():
            shutil.copyfile(lab["remote"].source, self.link)
        self.ctl, self.pids = tmp / "sender.ctl", tmp / "sender.pids"
        self.ctl.write_text("run")
        self.vport = free_port()
        self.agent = Agent("--link-config", str(self.link), "--bind", "127.0.0.1", "--heartbeat-interval", "0.2",
                           "--net-video-port", str(self.vport), *extra, "--video-command", sys.executable, str(SENDER),
                           str(self.ctl), str(self.pids), "30", "h264")
        self.pc = PeerClient(None, self.agent.port, heartbeat_timeout_s=2.0, tls_cfg=lab["console"]).start()
        self.vc = None
        assert wait_for(lambda: self.pc.snapshot()["phase"] == "connected", 10), self.pc.snapshot()
        if video:
            self.vc = videolink.VideoClient(lab["console"], "127.0.0.1", self.vport)
            self.r, self.w = os.pipe()
            self.vt = threading.Thread(target=self.vc.run, args=(self.w,), daemon=True)
            self.dt = threading.Thread(target=lambda: [None for _ in iter(lambda: os.read(self.r, 65536), b"")],
                                       daemon=True)
            self.vt.start()
            self.dt.start()

    def video(self):
        return (self.pc.snapshot()["telemetry"] or {}).get("video") or {}

    def source(self):
        return self.video().get("source") or {}

    def sources(self):
        return self.video().get("sources") or []

    def active(self):
        return [e["id"] for e in self.sources() if e["active"]]

    def select(self, sid):
        self.pc.select_video_source(sid)
        req = self.pc.snapshot()["video_select"]["req"]
        assert wait_for(lambda: (self.video().get("select") or {}).get("req") == req, 5), \
            (self.pc.snapshot()["video_select"], self.video().get("select"))
        return self.video()["select"]

    def sender_pids(self):
        return [int(x) for x in self.pids.read_text().split()] if self.pids.exists() else []

    def close(self):
        if self.vc is not None:
            self.vc.close()
            self.vt.join(5)
        self.pc.stop()
        self.agent.stop()
        if self.vc is not None:
            os.close(self.w)
            self.dt.join(5)
            os.close(self.r)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                                   # зомби (ещё не снят родителем) — не живой
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(st) and not st.startswith("Z")
    except OSError:
        return True


def fd_count(pid):
    if os.path.isdir(f"/proc/{pid}/fd"):
        return len(os.listdir(f"/proc/{pid}/fd"))
    out = subprocess.run(["lsof", "-n", "-P", "-p", str(pid)], capture_output=True, text=True).stdout
    return len(out.splitlines())


def children(pid):
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout
    return [int(x) for x in out.split()]


def write_config(tmp, cam_port, **spec):
    dead = free_port()
    (tmp / "video-input.json").write_text(json.dumps({
        "rtsp_urls": [f"rtsp://op:secret@127.0.0.1:{cam_port}/cam", f"rtsp://127.0.0.1:{dead}/down"],
        **(spec or {"source": "synthetic"})}))


def test_sources_list_select_persist_and_reload_after_restart(lab, tmp_path):
    cam = FakeRTSP()
    write_config(tmp_path, cam.port)
    u = Unit(lab, tmp_path)
    try:
        assert wait_for(lambda: len(u.sources()) == 3 and not u.sources()[2]["available"], 10), u.sources()
        assert u.sources() == [
            {"id": "synthetic", "input": "synthetic", "name": "SHEREGESH synthetic test pattern", "available": True,
             "active": True},
            {"id": "rtsp:0", "input": "rtsp", "name": f"RTSP 127.0.0.1:{cam.port}/cam", "available": True,
             "active": False},
            {"id": "rtsp:1", "input": "rtsp", "name": u.sources()[2]["name"], "available": False, "active": False}]
        assert wait_for(lambda: u.source().get("state") == "streaming", 10), u.source()
        res = u.select("rtsp:0")
        assert res["result"] == "ok" and res["id"] == "rtsp:0" and res["error"] == "", res
        assert wait_for(lambda: u.source().get("type") == "rtsp" and u.source().get("state") == "streaming", 10)
        assert u.active() == ["rtsp:0"] and u.pc.snapshot()["video_select"]["state"] == "sent"
        saved = json.loads((tmp_path / "video-input.json").read_text())
        assert saved["source"] == "rtsp" and saved["url"].endswith(f":{cam.port}/cam") and len(saved["rtsp_urls"]) == 2
        # неизвестный id: ошибка в телеметрии, источник и файл не меняются
        res = u.select("rtsp:7")
        assert res["result"] == "unknown-id" and "rtsp:7" in res["error"]
        time.sleep(0.6)
        assert u.source()["type"] == "rtsp" and u.active() == ["rtsp:0"]
        assert json.loads((tmp_path / "video-input.json").read_text()) == saved
        assert "secret" not in json.dumps(u.video())
    finally:
        u.close()
    log = u.agent.stderr()
    assert "video select 'rtsp:0' (req 1): ok" in log and "unknown-id" in log and "secret" not in log, log
    # перезапуск блока: выбор перечитан из video-input.json
    u = Unit(lab, tmp_path, video=False)
    try:
        assert wait_for(lambda: u.source().get("type") == "rtsp" and u.active() == ["rtsp:0"], 10), u.video()
        res = u.select("synthetic")
        assert res["result"] == "ok"
        assert wait_for(lambda: u.source().get("type") == "synthetic" and u.active() == ["synthetic"], 10)
        assert json.loads((tmp_path / "video-input.json").read_text())["source"] == "synthetic"
    finally:
        u.close()
        cam.close()


def test_20_switches_synthetic_rtsp_without_leaks(lab, tmp_path):
    cam = FakeRTSP()
    write_config(tmp_path, cam.port)
    u = Unit(lab, tmp_path)
    try:
        assert wait_for(lambda: u.source().get("state") == "streaming", 10), u.source()
        time.sleep(0.5)
        pid = u.agent.proc.pid
        fds, kids = fd_count(pid), len(children(pid))
        for i in range(20):
            sid, kind = ("rtsp:0", "rtsp") if i % 2 == 0 else ("synthetic", "synthetic")
            assert u.select(sid)["result"] == "ok", i
            assert wait_for(lambda: u.source().get("type") == kind and u.source().get("state") == "streaming"
                            and u.active() == [sid], 10), (i, u.source(), u.active())
            running = [p for p in u.sender_pids() if alive(p)]
            assert len(running) == 1, (i, running)                       # старый конвейер остановлен
        time.sleep(0.5)
        assert len(u.sender_pids()) == 21
        assert abs(fd_count(pid) - fds) <= 3, (fds, fd_count(pid))       # дескрипторы не копятся
        assert len(children(pid)) == kids, (kids, children(pid))         # дочерние процессы не копятся
        st = u.vc.stats()
        assert st["connected"] and st["seq_gaps"] == 0, st               # один видеосеанс, RTP непрерывен
    finally:
        u.close()
        cam.close()
    assert not any(alive(p) for p in u.sender_pids())


def test_old_remote_session_11_console_does_not_send(lab, tmp_path):
    write_config(tmp_path, free_port())
    u = Unit(lab, tmp_path, "--protocol", "1.1", video=False)
    try:
        assert u.pc.snapshot()["session"] == "1.1"
        assert wait_for(lambda: u.source().get("state"), 10)
        u.pc.select_video_source("rtsp:0")
        assert u.pc.snapshot()["video_select"] == {"req": 1, "id": "rtsp:0", "state": "unsupported"}
        time.sleep(0.8)
        assert u.source()["type"] == "synthetic" and "select" not in u.video()
    finally:
        u.close()
    assert "video select" not in u.agent.stderr()


def _raw_session(port, proto, sid):
    """Пульт «по проводу» (loopback): hello с версией proto, затем video-select; возвращает последний status."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(encode(proto, "hello", 1, 0.0, {"role": "console", "proto": proto, "software": "t",
                                                  "device_id": "t"}))
        s.sendall(encode(proto, "video-select", 2, 0.0, {"id": sid, "req": 5}))
        dec, last, deadline = Decoder(), None, time.monotonic() + 3
        s.settimeout(0.3)
        while time.monotonic() < deadline:
            try:
                data = s.recv(65536)
            except socket.timeout:
                continue
            for m in dec.feed(data):
                if isinstance(m, dict) and m["type"] == "status":
                    last = m["body"]
        return last


def test_old_console_hello_11_video_select_not_executed(tmp_path):
    cfg = tmp_path / "video-input.json"
    cfg.write_text(json.dumps({"source": "synthetic", "rtsp_urls": ["rtsp://127.0.0.1:9/cam"]}))
    before = cfg.read_text()
    a = Agent("--video", "--video-config", str(cfg), "--heartbeat-interval", "0.2")
    try:
        st = _raw_session(a.port, "1.1", "rtsp:0")                       # старый пульт: сессия 1.1
        assert st is not None and "select" not in st["video"] and cfg.read_text() == before
        st = _raw_session(a.port, "1.2", "rtsp:0")                       # контроль: сессия 1.2 исполняет
        assert st["video"]["select"] == {"req": 5, "id": "rtsp:0", "result": "ok", "error": ""}, st["video"]
        assert json.loads(cfg.read_text())["source"] == "rtsp"
    finally:
        a.stop()


# ---------------- перечитывание по SIGHUP (находки совместимости Н-1, Н-2) ----------------
def test_pipeline_survives_sighup_to_whole_group():
    """systemctl kill -s HUP шлёт сигнал всей cgroup службы: конвейер (video.spawn) должен его игнорировать."""
    import signal
    from sheregesh import video
    p = video.spawn([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.5)
        os.killpg(p.pid, signal.SIGHUP)
        time.sleep(0.5)
        assert p.poll() is None
    finally:
        os.killpg(p.pid, signal.SIGKILL)
        p.wait(5)


def test_sighup_keeps_video_and_deleted_config_means_synthetic(lab, tmp_path):
    import signal
    cam = FakeRTSP()
    write_config(tmp_path, cam.port, source="rtsp", url=f"rtsp://127.0.0.1:{cam.port}/cam")
    u = Unit(lab, tmp_path)
    try:
        assert wait_for(lambda: u.source().get("type") == "rtsp" and u.source().get("state") == "streaming", 10)
        pids = u.sender_pids()
        u.agent.signal(signal.SIGHUP)                     # конвейер в своей группе; cgroup — test_pipeline_survives_…
        time.sleep(1.0)
        assert u.sender_pids() == pids and all(alive(p) for p in pids)       # конфиг тот же — конвейер не тронут
        assert u.source()["state"] == "streaming"
        (tmp_path / "video-input.json").unlink()
        u.agent.signal(signal.SIGHUP)
        assert wait_for(lambda: u.source().get("type") == "synthetic" and u.source().get("state") == "streaming"
                        and u.active() == ["synthetic"], 10), u.source()
    finally:
        u.close()
        cam.close()
    log = u.agent.stderr()
    assert "video config unchanged" in log and "not found" not in log, log


def test_videoselect_cli_same_path_as_gui(lab, tmp_path):
    """python3 -m sheregesh.link.videoselect (стенд/приёмка): список, выбор (код 0, active), неизвестный id (код 1)."""
    from procs import ENV
    cam = FakeRTSP()
    write_config(tmp_path, cam.port)
    u = Unit(lab, tmp_path, video=False)
    u.pc.stop()                                                  # блок держит один сеанс: освобождаем его для команды
    cfg = json.loads(Path(lab["console"].source).read_text())
    cfg["port"] = u.agent.port
    cli_cfg = tmp_path / "console-link.json"
    cli_cfg.write_text(json.dumps(cfg))

    def cli(*args):
        r = subprocess.run([sys.executable, "-m", "sheregesh.link.videoselect", "--link-config", str(cli_cfg), *args],
                           env=ENV, capture_output=True, text=True, timeout=40)
        return r.returncode, json.loads(r.stdout or "{}"), r.stderr
    try:
        rc, out, err = cli("--list")
        assert rc == 0 and [e["id"] for e in out["sources"]] == ["synthetic", "rtsp:0", "rtsp:1"], (out, err)
        rc, out, err = cli("rtsp:0", "--wait", "10")
        assert rc == 0 and out["result"] == "ok" and out["active"] == ["rtsp:0"], (out, err)
        assert json.loads((tmp_path / "video-input.json").read_text())["source"] == "rtsp"
        rc, out, _ = cli("v4l2:dead:beef")
        assert rc == 1 and out["result"] == "unknown" and out["active"] == ["rtsp:0"], out
    finally:
        u.agent.stop()
        cam.close()
