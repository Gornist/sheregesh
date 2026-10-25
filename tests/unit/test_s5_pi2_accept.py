# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Приёмка pi2 после OTA bd74c8c (tools/probe/s5_pi2_accept.py): суждения без плат — состояние OTA по фазам и запрет
перезагрузки на пробной загрузке, P2-01, живость по ui-state, сохранение выбора (S5-D1), отказы эмулятора, отказ
polkit на enable, вход vivid, slot_guard «до confirm / после» на настоящих снимках фиксации pi1, pi1 не затронут,
ожидание перезагрузки, команды — в пределах правил polkit образа, план."""
import argparse
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "probe"))
sys.path.insert(0, str(ROOT / "tools" / "deploy" / "ota-check"))
import s5_pi2_accept as s  # noqa: E402

RETRY = ROOT / "docs/acceptance/e2e/ota-pi1/cycle2/retry"
V_PI1 = "0.1.1792924537"                     # образ b1d95a2 настоящей фиксации pi1 (cycle2/retry)
TRIAL = json.loads((RETRY / "ota-status-trial.json").read_text())
COMMITTED = json.loads(json.loads((RETRY / "pi1-final.json").read_text())["fields"]["ota.status"])


def snap(name):
    return json.loads((RETRY / name).read_text())


def test_ota_phase_on_real_status():
    t, c = s.ota_summary(TRIAL), s.ota_summary(COMMITTED)
    assert t["state"] == "HEALTH_PENDING" and t["slot"] == "B" and t["tryboot_now"] is True
    assert s.ota_problems(t, "trial", V_PI1) == []
    assert s.ota_problems(c, "committed", V_PI1) == []
    assert s.ota_problems(t, "committed", V_PI1)                   # пробная — не фиксация
    assert s.ota_problems(c, "trial", V_PI1)
    assert any("version_target" in p for p in s.ota_problems(t, "trial", s.EXPECT_VERSION))
    assert s.ota_summary({"result": "ERROR"}) is None and s.ota_problems(None, "trial")
    on_a = copy.deepcopy(TRIAL)
    on_a["slots"].update(active="A", tryboot_now=False)
    assert any("слот 'A'" in p for p in s.ota_problems(s.ota_summary(on_a), "trial", V_PI1))


def test_reboot_never_on_trial():
    assert s.reboot_allowed(s.ota_summary(TRIAL))[0] is False
    assert s.reboot_allowed(s.ota_summary(COMMITTED))[0] is True
    assert s.reboot_allowed(None)[0] is False
    bad = copy.deepcopy(COMMITTED)
    bad["autoboot"]["default_part"] = 2                            # основной всё ещё A
    assert s.reboot_allowed(s.ota_summary(bad))[0] is False


def ident(ota=TRIAL, commit="bd74c8c412028f8f", version=V_PI1, failed="", health="PASS", dep="factory"):
    return {"ota": s.ota_summary(ota), "build": {"commit": commit, "ota_version": version}, "failed": failed,
            "health": {"verdict": health, "not_pass": []}, "uptime_s": "201.3", "deploy_current": dep}


def test_judge_boot():
    v, d, crit = s.judge_boot(ident(), "trial", version=V_PI1)
    assert (v, crit) == ("PASS", False) and "HEALTH_PENDING" in d
    v, d, crit = s.judge_boot(ident(commit="57e8fe48713b"), "trial", version=V_PI1)
    assert v == "FAIL" and crit and "57e8fe48713b" in d
    v, d, crit = s.judge_boot(ident(failed="console-setup.service loaded failed"), "trial", version=V_PI1)
    assert v == "FAIL" and not crit and "console-setup" in d
    v, d, crit = s.judge_boot(ident(dep="/var/lib/sheregesh-deploy/releases/x"), "trial", version=V_PI1)
    assert v == "FAIL" and "DEV-выпуска" in d and not crit
    assert s.judge_boot(ident(dep="/x"), "trial", version=V_PI1, allow_dev_release=True)[0] == "PASS"
    assert s.judge_boot(ident(health="FAIL"), "trial", version=V_PI1)[0] == "FAIL"
    v, _d, crit = s.judge_boot(ident(ota=COMMITTED), "trial", version=V_PI1)
    assert v == "FAIL" and crit
    assert s.judge_boot(ident(ota=COMMITTED), "committed", version=V_PI1)[0] == "PASS"
    assert s.judge_boot({**ident(), "ota": None}, "trial")[0] == "ERROR"


def ui(inp, frames, live=True, fps=30.0, display="playing", sessions=1):
    return {"videoSource": {"input": inp, "live": live, "display": display},
            "video": {"frames_decoded": frames, "decoded_fps_5s": fps, "sessions": sessions}}


def test_judge_live():
    assert s.judge_live([ui("rtsp", 100), ui("rtsp", 130), ui("rtsp", 190)], "rtsp")[0]
    ok, d = s.judge_live([ui("rtsp", 100), ui("rtsp", 101)], "rtsp")
    assert not ok and "не растут" in d
    ok, d = s.judge_live([ui("synthetic", 100), ui("synthetic", 190)], "rtsp")
    assert not ok and "synthetic" in d
    assert not s.judge_live([ui("hdmi", 1, live=False), ui("hdmi", 90, live=False)], "hdmi")[0]
    assert not s.judge_live([ui("rtsp", 100), ui("rtsp", 190, fps=0)], "rtsp")[0]


CONF = json.dumps({"source": "synthetic", "rtsp_urls": [
    {"url": "rtsp://192.168.1.92:8554/cam", "codec": "mjpeg", "name": "PI1 RTSP test"},
    {"url": "rtsp://192.168.1.193:8554/cam", "codec": "h264", "name": "shcam Pi5 IMX477"}]})


def test_judge_saved_s5_d1():
    cur = json.loads(CONF)
    rtsp = json.dumps({**cur, "source": "rtsp", "url": "rtsp://192.168.1.193:8554/cam", "codec": "h264"})
    assert s.judge_saved(rtsp, "rtsp:1", CONF)[0]
    ok, d = s.judge_saved(CONF, "rtsp:1", CONF)                     # выбор не записан (дефект S5-D1)
    assert not ok and "source='synthetic'" in d
    assert not s.judge_saved(rtsp, "rtsp:0", CONF)[0]
    assert s.judge_saved(CONF, "synthetic", rtsp)[0]
    lost = json.dumps({"source": "rtsp", "url": "rtsp://192.168.1.193:8554/cam"})
    ok, d = s.judge_saved(lost, "rtsp:1", CONF)
    assert not ok
    v4 = json.dumps({**cur, "source": "v4l2", "device": "534d:2109"})
    assert s.judge_saved(v4, "v4l2:534d:2109", CONF)[0]
    assert not s.judge_saved("__ABSENT__", "synthetic", CONF)[0]
    assert s.persist_target("synthetic", "rtsp:1") == "rtsp:1" and s.persist_target("rtsp:1", "rtsp:1") == "synthetic"


def test_judge_persist():
    m = {"want": "rtsp:1", "event": "systemctl reboot", "selected": True, "saved_ok": True, "event_ok": True,
         "ready_s": 95.0, "active_after": ["rtsp:1"], "saved_after_ok": True, "timeout_s": 180}
    assert s.judge_persist(m)[0] == "PASS"
    assert s.judge_persist({**m, "saved_ok": False, "saved_detail": "x"})[0] == "FAIL"
    v, d = s.judge_persist({**m, "active_after": ["synthetic"]})
    assert v == "FAIL" and "synthetic" in d
    assert s.judge_persist({**m, "ready_s": None})[0] == "FAIL"
    assert s.judge_persist({**m, "event_ok": False, "event_detail": "SSH не вернулся"})[0] == "ERROR"
    assert s.judge_persist({**m, "selected": False})[0] == "ERROR"


def test_judge_outage():
    ok = {"off_s": 1.2, "console_fault_s": 1.5, "remote_fault_state": "no-device", "back_s": 6.0}
    assert s.judge_outage(ok, "no-device")[0] == "PASS"
    v, d = s.judge_outage({**ok, "console_fault_s": None}, "no-device")
    assert v == "FAIL" and "не показал" in d
    assert s.judge_outage({**ok, "back_s": 14.0}, "no-device")[0] == "FAIL"
    assert s.judge_outage({**ok, "remote_fault_state": "streaming"}, "no-signal")[0] == "FAIL"
    assert s.judge_outage({"error": "отказ не внесён"}, "no-signal")[0] == "ERROR"


def test_enable_must_be_denied_by_polkit():
    v, _ = s.judge_enable_denied(1, "Failed to enable unit: Access denied", "static")
    assert v == "PASS"
    assert s.judge_enable_denied(1, "Failed to enable unit: Interactive authentication required.", "")[0] == "PASS"
    assert s.judge_enable_denied(0, "", "enabled")[0] == "FAIL"
    assert s.judge_enable_denied(1, "Failed to enable unit: Read-only file system", "disabled")[0] == "FAIL"
    assert s.judge_enable_denied(1, "что-то иное", "disabled")[0] == "ERROR"


def test_pick_vivid():
    before = [{"id": "synthetic", "input": "synthetic", "available": True}]
    after = before + [{"id": "v4l2:vivid-000", "input": "cvbs", "available": True},
                      {"id": "v4l2:vivid-001", "input": "hdmi", "available": True},
                      {"id": "v4l2:534d:0021", "input": "cvbs", "available": True}]
    assert s.pick_vivid(before, after) == ("v4l2:vivid-000", None)
    sid, why = s.pick_vivid(before, before)
    assert sid is None and why.startswith(s.S5.HW_UNAVAILABLE)
    two = after + [{"id": "v4l2:x", "input": "cvbs", "available": True}]
    assert "несколько" in s.pick_vivid(before, two)[1]
    assert s.pick_vivid(before, after, "v4l2:vivid-000") == ("v4l2:vivid-000", None)


@pytest.mark.parametrize("after", ["pi1-committed.json", "pi1-reboot.json", "pi1-final.json"])
def test_commit_real_pi1_snapshots(after):
    """Настоящая фиксация pi1: «до confirm» — пробная загрузка B; «после» — та же загрузка, перезагрузка, disable."""
    v, bad, _checks = s.judge_commit(snap("pi1-trial.json"), snap(after), V_PI1)
    assert (v, bad) == ("PASS", []), bad


def test_commit_detects_unexpected_changes():
    before, after = snap("pi1-trial.json"), snap("pi1-reboot.json")
    a = copy.deepcopy(after)
    a["fields"]["tree.system"] = "0" * 64 + " 672"
    v, bad, _ = s.judge_commit(before, a, V_PI1)
    assert v == "FAIL" and any("tree.system" in b for b in bad)
    a = copy.deepcopy(after)
    w4 = int(a["fields"]["io.p4"].split()[0])                       # слот A писался после перезагрузки
    a["fields"]["io.p4"] = f"{w4 + 8} 0"
    wd, dd = a["fields"]["io.disk"].split()
    a["fields"]["io.disk"] = f"{int(wd) + 8} {dd}"
    v, bad, _ = s.judge_commit(before, a, V_PI1)
    assert v == "FAIL" and any("system_a" in b for b in bad)
    a = copy.deepcopy(after)
    st = json.loads(a["fields"]["ota.status"])
    st["state"]["state"] = "HEALTH_PENDING"
    a["fields"]["ota.status"] = json.dumps(st, sort_keys=True)
    v, bad, _ = s.judge_commit(before, a, V_PI1)
    assert v == "FAIL" and any("COMMITTED" in b for b in bad)
    a = copy.deepcopy(after)
    a["fields"]["unit.sheregesh-console"] = a["fields"]["unit.sheregesh-console"].replace("NRestarts=0", "NRestarts=2")
    assert s.judge_commit(before, a, V_PI1)[0] == "FAIL"
    committed = snap("pi1-committed.json")                          # без перезагрузки InvocationID обязаны совпасть
    c = copy.deepcopy(committed)
    c["fields"]["unit.ssh"] = c["fields"]["unit.ssh"].replace("InvocationID=", "InvocationID=f")
    assert s.judge_commit(before, c, V_PI1)[0] == "FAIL"
    assert s.judge_commit(before, {"meta": {}, "fields": {}, "errors": ["нет"]}, V_PI1)[0] == "ERROR"


def test_judge_pi1_untouched():
    o = s.ota_summary(COMMITTED)
    base = {"ota": o, "boot_id": "b1"}
    assert s.judge_pi1(base, {"ota": dict(o), "boot_id": "b1"}) == ("PASS", [])
    v, p = s.judge_pi1(base, {"ota": dict(o), "boot_id": "b2"})
    assert v == "FAIL" and "boot_id" in p[0]
    v, p = s.judge_pi1(base, {"ota": {**o, "state": "INSTALLED"}, "boot_id": "b1"})
    assert v == "FAIL"
    assert s.judge_pi1(base, {"ota": None, "boot_id": "b1"})[0] == "ERROR"
    assert s.judge_pi1({"ota": s.ota_summary(TRIAL), "boot_id": "b"}, {"ota": s.ota_summary(TRIAL),
                                                                        "boot_id": "b"})[0] == "FAIL"


def test_gui_pick():
    st = [{"shot": "01-02-stream-video-t+1s.png", "videoSource": {"input": "rtsp"}, "frame_changed": True},
          {"shot": "03-04-selected-synthetic-video-t+1s.png", "videoSource": {"input": "synthetic"},
           "frame_changed": True},
          {"shot": "05-06-selected-rtsp_1-video-t+1s.png", "videoSource": {"input": "rtsp"}, "frame_changed": False}]
    assert s.judge_gui_pick(st, "synthetic", "synthetic")[0] == "PASS"
    v, d = s.judge_gui_pick(st, "rtsp:1", "rtsp")
    assert v == "FAIL" and "не сменился" in d
    assert s.judge_gui_pick([], "rtsp:1", "rtsp")[0] == "ERROR"
    rej = st[:1] + [{"shot": "select-error", "select_error": {"state": "error", "code": "unavailable"}}]
    assert "unavailable" in s.judge_gui_pick(rej, "rtsp:1", "rtsp")[1]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, d):
        self.t += d


def test_wait_reboot():
    clk = Clock()
    seq = iter([(0, "old"), (255, ""), (124, ""), (255, ""), (0, "new")])
    w = s.wait_reboot("h", "old", 60, ssh=lambda h, c, t: next(seq), clock=clk, sleep=clk.sleep)
    assert w["boot_id"] == "new" and w["down_s"] == 2.0 and w["up_s"] == 8.0
    clk = Clock()
    w = s.wait_reboot("h", "old", 10, ssh=lambda h, c, t: (255, ""), clock=clk, sleep=clk.sleep)
    assert w["up_s"] is None and w["down_s"] == 0.0
    clk = Clock()                                                    # SSH так и не пропал, boot_id прежний
    w = s.wait_reboot("h", "old", 6, ssh=lambda h, c, t: (0, "old"), clock=clk, sleep=clk.sleep)
    assert w["up_s"] is None and w["down_s"] is None


def test_reboot_step_refuses_on_trial(monkeypatch):
    """В пробной загрузке P2-06 не перезагружает: ни одной команды reboot, вердикт BLOCKED."""
    sent = []
    monkeypatch.setattr(s.E, "out", lambda h, c, t=60: json.dumps(TRIAL) if "ota-client status" in c else "")
    monkeypatch.setattr(s, "ssh", lambda h, c, timeout=60: sent.append(c) or (0, ""))
    a = argparse.Namespace(console="c", remote="r", rtsp_id="rtsp:1", expect_version=V_PI1)
    (v, d), m = s.step_persist_reboot(a, None, CONF)
    assert v == "BLOCKED" and "вернула бы основной слот" in d and not any("reboot" in c for c in sent)


def test_videoselect_via_running_console(monkeypatch):
    sent = []
    monkeypatch.setattr(s, "ssh", lambda h, c, timeout=60: sent.append((h, c)) or
                        (0, 'videoselect: …\n{"result": "ok", "active": ["rtsp:1"]}'))
    a = argparse.Namespace(console="pi1", remote="pi2")
    code, d, _ = s.videoselect(a, "rtsp:1 --wait 20")
    host, cmd = sent[0]
    assert host == "pi1" and d["active"] == ["rtsp:1"] and code == 0
    assert "-m sheregesh.link.videoselect rtsp:1 --wait 20 --via console" in cmd and "PYTHONPATH=$C/software" in cmd


def test_commands_within_image_polkit_rules():
    """Действия sheregesh-ui в сценарии разрешены правилами образа (50-… dev, 51-… п. 7); enable — нет."""
    r50 = (ROOT / "os/layer/sheregesh-dev-access.d/50-sheregesh-dev.rules").read_text()
    r51 = (ROOT / "tools/lab-video/pi2/etc/51-sheregesh-lab-video.rules").read_text()
    assert '"sheregesh-remote-net.service"' in r50 and "org.freedesktop.login1.reboot" in r50
    for unit in (s.USBIP, s.VIVID, s.VIVID_FEED):
        assert f'"{unit}.service"' in r51
    assert 'verb == "start" || verb == "stop" || verb == "restart"' in r51
    assert "manage-unit-files" not in r50 + r51                    # enable не разрешён никем
    src = (ROOT / "tools/probe/s5_pi2_accept.py").read_text()
    assert "reset-failed" not in src and "sudo" not in src


def test_plan_without_ssh(capsys):
    assert s.main(["--plan"]) == 0
    out = capsys.readouterr().out
    assert all(k in out for k in ("P2-01", "P2-05", "P2-06", "restart sheregesh-remote-net")) and "P2-07" not in out
    assert "Перезагрузки нет" in out
    assert s.main(["--plan", "--phase", "committed"]) == 0
    out = capsys.readouterr().out
    assert "P2-07" in out and "systemctl reboot" in out
    with pytest.raises(SystemExit):
        s.main(["--phase", "committed", "--out", "/nonexistent-dir-x"])
