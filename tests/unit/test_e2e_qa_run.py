# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Инструмент E2E обрыв→восстановление (tools/probe/e2e_run.py): суждение и безопасность без плат."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "probe"))
import e2e_run as e  # noqa: E402


def ui(t, link="CONNECTED", video="playing", session="s1", fps=30.0, written=None):
    return (t, 1000.0 + t, {"time": 1000.0 + (t if written is None else written), "peerLink": link,
                            "video": {"mode": "remote-srtp" if video == "playing" else "off", "state": video,
                                      "decoded_fps_5s": fps if video == "playing" else 0.0,
                                      "link": {"session": session if video == "playing" else ""}}})


def rows_outage():
    """Отказ в t=0: связь пропала на 3,5 с, видео — на 3,0 с; восстановление в 10 → связь 11,5, видео 13."""
    r = [ui(0.0), ui(1.0)]
    r += [ui(3.0, video="off"), ui(3.5, link="RECONNECTING", video="off")]
    r += [ui(t, link="RECONNECTING", video="off") for t in (5.0, 8.0, 10.0)]
    r += [ui(11.5, video="off"), ui(13.0, session="s2")]
    return r


def snap(nrestarts=0, failed=0, gst=0, orphans=0):
    return {"nrestarts": nrestarts, "failed": failed, "gst_count": gst, "gst_orphans": ["x"] * orphans,
            "procs": [{"pid": "1", "rss_kb": 1000, "threads": 5, "fds": 20}]}


def test_measure_counts_from_restore_and_requires_new_video_session():
    m = e.measure(rows_outage(), 0.0, 10.0, "s1")
    assert m["detect_s"] == 3.5 and m["video_off_s"] == 3.0
    assert m["link_back_s"] == 1.5 and m["video_back_s"] == 3.0
    # Без нового сеанса SRTP «играет» до отказа не засчитывается как восстановление.
    assert e.measure([ui(0.0), ui(1.0)], 0.0, 0.0, "s1")["video_back_s"] is None


def test_gui_staleness_measured_from_board_clock():
    rows = [ui(0.0), ui(5.0, written=1.0)]        # файл не обновлялся 4 с — цикл событий GUI стоял
    assert e.gui_stale_max(rows) == 4.0


def test_thresholds_come_from_srs_and_exceeding_is_fail():
    assert e.LIMITS["detect_s"][0] == 5.0 and "REQ-SYS-002" in e.LIMITS["detect_s"][1]
    assert e.LIMITS["video_off_s"][0] == 3.0 and "REQ-SW-002" in e.LIMITS["video_off_s"][1]
    ok = {"detect_s": 3.5, "link_back_s": 1.5, "video_off_s": 3.0, "video_back_s": 3.0}
    assert e.judge("pause", ok, {})["verdict"] == "PASS"
    slow = e.judge("pause", {**ok, "video_off_s": 3.3}, {})          # как ctl-loss S3.1-VIDEO: 3,31 с
    assert slow["verdict"] == "FAIL" and slow["criteria"]["video_off_s"]["result"] == "FAIL"


def test_not_observed_and_tool_errors_are_never_pass():
    r = e.judge("pause", {"detect_s": None, "link_back_s": None, "video_off_s": 1, "video_back_s": 2}, {})
    assert r["verdict"] == "ERROR" and r["criteria"]["detect_s"]["result"] == "NOT_OBSERVED"
    r = e.judge("restart", {"link_back_s": 2, "video_back_s": 2, "errors": ["отказ не внесён"]}, {})
    assert r["verdict"] == "ERROR"
    # FAIL важнее ERROR: найденный дефект не прячется за неполнотой.
    r = e.judge("pause", {"detect_s": 9, "link_back_s": None, "video_off_s": 1, "video_back_s": 2}, {})
    assert r["verdict"] == "FAIL"


def test_restart_judges_only_applicable_limits_and_sender_kill_is_gone():
    assert set(e.APPLICABLE["restart"]) == {"link_back_s", "video_back_s"}
    assert "sender-kill" not in e.FAULTS and "sender-kill" not in e.APPLICABLE    # генератор TLS-видео снят
    r = e.judge("restart", {"detect_s": None, "link_back_s": 1.0, "video_back_s": 2.4}, {})
    assert r["verdict"] == "PASS" and "detect_s" not in r["criteria"]


def test_invariants_watchdog_in_pause_is_error_restart_of_console_is_fail():
    b = {"console": snap(), "remote": snap()}
    inv = e.invariants("pause", b, {"console": snap(), "remote": snap(nrestarts=1)}, ["session from 1.2.3.4: urn"])
    assert inv["remote.nrestarts"][0] is False and inv["remote.nrestarts"][2] == "ERROR"
    inv = e.invariants("stop-start", b, {"console": snap(nrestarts=1), "remote": snap()}, ["session from x"])
    assert inv["console.nrestarts"] == (False, "0 → 1", "FAIL")
    inv = e.invariants("pause", b, {"console": snap(gst=2), "remote": snap(orphans=1)}, [])
    assert not inv["console.gst_pipelines"][0] and not inv["remote.gst_pipelines"][0]
    assert not inv["remote.journal_session"][0]
    # SRTP: на пульте не больше одного декодера GUI (путь GL — ни одного), на блоке gst-launch вне службы tx нет
    ok = e.invariants("pause", b, {"console": snap(gst=1), "remote": snap()}, ["session from x"])
    assert ok["console.gst_pipelines"][0] and ok["remote.gst_pipelines"][0]
    assert not e.invariants("pause", b, {"console": snap(), "remote": snap(gst=1)}, [])["remote.gst_pipelines"][0]


def test_summary_order_fail_error_pass_and_resource_growth_is_measured_only():
    c = lambda v: {"verdict": v, "measured": {"detect_s": 3.0}}                     # noqa: E731
    assert e.summarize([c("PASS"), c("ERROR")], [])["verdict"] == "ERROR"
    assert e.summarize([c("PASS"), c("ERROR"), c("FAIL")], [])["verdict"] == "FAIL"
    assert e.summarize([], [])["verdict"] == "ERROR"                                  # ни одного цикла — не PASS
    s0 = {"console": snap(), "remote": snap()}
    s1 = {"console": {**snap(), "procs": [{"pid": "1", "rss_kb": 9000, "threads": 9, "fds": 40}]}, "remote": snap()}
    s = e.summarize([c("PASS")], [s0, s1])
    assert s["verdict"] == "PASS" and s["resource_growth"]["console"]["rss_kb"] == 8000


class FakeSSH:
    def __init__(self, replies=None):
        self.calls, self.replies = [], replies or {}

    def __call__(self, host, cmd, timeout=60):
        self.calls.append((host, cmd))
        for key, val in self.replies.items():
            if key in cmd:
                return val
        return 0, ""


def test_pause_arms_board_side_resume_before_stop_and_always_resumes(monkeypatch):
    f = FakeSSH({"MainPID": (0, "4242"), "kill -CONT": (0, "resumed")})
    monkeypatch.setattr(e, "ssh", f)
    fault = e.Fault("pause", "pi2", 8.0)
    assert fault.inject()[0]
    cmds = [c for _h, c in f.calls]
    arm = next(i for i, c in enumerate(cmds) if "nohup" in c and "kill -CONT 4242" in c)
    stop = next(i for i, c in enumerate(cmds) if c == "kill -STOP 4242")
    assert arm < stop                                    # страховка взведена раньше заморозки
    assert fault.restore() == (True, "resumed")
    assert all(h == "pi2" for h, _c in f.calls)          # пульт отказами не трогается


def test_pause_not_resumed_is_reported(monkeypatch):
    monkeypatch.setattr(e, "ssh", FakeSSH({"MainPID": (0, "7"), "kill -CONT": (0, "stuck")}))
    fault = e.Fault("pause", "pi2", 5)
    fault.inject()
    assert fault.restore() == (False, "stuck")


def test_only_allowed_actions_are_sent(monkeypatch):
    """Каждая команда — сигнал процессу sheregesh-ui или systemctl над юнитом из правила polkit; без sudo/ip/nmcli."""
    for kind in e.FAULTS:
        f = FakeSSH({"MainPID": (0, "11"), "pgrep": (0, "12 13"), "kill -CONT": (0, "resumed")})
        monkeypatch.setattr(e, "ssh", f)
        fault = e.Fault(kind, "pi2", 5, prompt=lambda _m: "")
        fault.inject()
        fault.restore()
        for _h, cmd in f.calls:
            assert not any(w in cmd for w in ("sudo", "ip link", "nmcli", "networkctl", "iptables", "nft", "reboot",
                                              "poweroff", "sheregesh-ota-client")), cmd
            for verb in ("systemctl stop", "systemctl start", "systemctl restart"):
                if verb in cmd:
                    assert "sheregesh-remote-net" in cmd, cmd


def test_cli_limits_and_plan_without_ssh(monkeypatch, capsys):
    monkeypatch.setattr(e, "ssh", lambda *a, **k: pytest.fail("в режиме --plan SSH запрещён"))
    with pytest.raises(SystemExit):
        e.main(["--plan", "--fault", "pause", "--outage", "12"])     # пауза дольше 10 с — уже watchdog
    with pytest.raises(SystemExit):
        e.main(["--plan", "--fault", "stop-start", "--gap", "5"])
    assert e.main(["--plan", "--fault", "manual", "--cycles", "3"]) == 0
    assert "эквивалент" in capsys.readouterr().out


def test_preflight_wrong_serial_or_ota_in_progress_is_error_without_faults(monkeypatch, tmp_path):
    f = FakeSSH({"serial-number": (0, "deadbeef"), "sheregesh-ota-client status": (0, '{"state": "TRYBOOT"}'),
                 "sheregesh-health": (0, json.dumps({"verdict": "PASS", "checks": []}))})
    monkeypatch.setattr(e, "ssh", f)

    class NoSampler:
        def __init__(self, host):
            pass

        def start(self):
            return self

        def stop(self):
            pass

        def last(self):
            return None
    monkeypatch.setattr(e, "Sampler", NoSampler)
    rc = e.main(["--console", "c", "--remote", "r", "--out", str(tmp_path), "--fault", "pause", "--cycles", "2"])
    s = json.loads((tmp_path / "summary.json").read_text())
    assert rc == 1 and s["verdict"] == "ERROR" and s["cycles"] == []
    assert any("TRYBOOT" in p for p in s["errors"]) and any("серийный" in p for p in s["errors"])
    assert not any(w in c for _h, c in f.calls for w in ("kill", "systemctl stop", "systemctl restart"))


def test_board_snapshot_parsing(monkeypatch):
    text = ("P\n812 215000 23 41\nG\n 900 812 gst-launch-1.0 unixfdsrc ! x\n 901 1 gst-launch-1.0 unixfdsrc\n"
            "S\nNRestarts=2 ActiveState=active MainPID=700 \nF\n0\nM\n3100000\n")
    monkeypatch.setattr(e, "ssh", lambda h, c, timeout=60: (0, text))
    r = e.resources("pi1", "console")
    assert r["procs"] == [{"pid": "812", "rss_kb": 215000, "threads": 23, "fds": 41}]
    assert r["nrestarts"] == 2 and r["failed"] == 0 and r["mem_available_kb"] == 3100000
    assert r["gst_count"] == 2 and len(r["gst_orphans"]) == 1                # ppid 1 — сирота


def test_ota_state_reads_agent_status_and_refuses_unknown(monkeypatch):
    status = {"serial": "x", "slots": {"active": "B"}, "state": {"state": "COMMITTED", "attempt": 0}, "consistent": True}
    monkeypatch.setattr(e, "ssh", lambda h, c, timeout=60: (0, json.dumps(status)))
    assert e.ota_state("pi2") == {"state": "COMMITTED", "active": "B", "consistent": True}
    monkeypatch.setattr(e, "ssh", lambda h, c, timeout=60: (127, ""))
    assert e.ota_state("pi2") is None


def test_ota_rest_states_only():
    """Допуск: OTA в покое (в т. ч. ROLLED_BACK после отката, pi2 2026-10-25), но не во время записи или пробной загрузки."""
    assert set(e.OTA_OK) == {"IDLE", "COMMITTED", "ROLLED_BACK"}
    assert not {"VERIFIED", "INSTALLING", "INSTALLED", "TRYBOOT", "HEALTH_PENDING", "FAILED"} & set(e.OTA_OK)
