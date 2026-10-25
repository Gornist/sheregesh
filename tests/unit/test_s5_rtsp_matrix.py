# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S5 RTSP-матрица (tools/probe/s5_rtsp_matrix.py): суждения без плат — ячейки и rtsp_urls, готовность контракта
NETWORK, фактический транспорт (поле статуса / сокеты /proc), ячейки «транспорт недоступен», GUI, план."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "probe"))
sys.path.insert(0, str(ROOT / "tools" / "deploy" / "ota-check"))
import s5_rtsp_matrix as mx  # noqa: E402

READY = {"server_transports": True, "server_codec": "mjpeg", "board_transport": True}
CS = mx.cells("192.168.1.92")


def test_cells_cover_matrix_with_distinct_urls():
    pos = {(c["transport"], c["codec"]) for c in CS[:6]}
    assert pos == {(t, k) for t in ("udp", "tcp", "auto") for k in ("mjpeg", "h264")}
    assert len({c["url"] for c in CS}) == len(CS)                   # active_id ищет по адресу
    assert [c["expect"] for c in CS[6:]] == ["tcp", "udp", "fail", "fail"]
    assert all(c["id"] == f"rtsp:{n}" for n, c in enumerate(CS))
    assert CS[3]["url"] == "rtsp://192.168.1.92:8602/s5-udp-h264" and CS[3]["server_transports"] == "tcp,udp"


def test_config_keeps_other_keys_and_sets_matrix_urls():
    orig = json.dumps({"source": "synthetic", "rtsp_urls": ["rtsp://192.168.1.92:8554/cam"], "note": "x"})
    out = json.loads(mx.config_text(orig, mx.spec_of(CS[0]), CS))
    assert out["source"] == "rtsp" and out["transport"] == "udp" and out["codec"] == "mjpeg"
    assert out["note"] == "x" and len(out["rtsp_urls"]) == len(CS)
    assert out["rtsp_urls"][8] == {"url": CS[8]["url"], "codec": "mjpeg", "transport": "udp", "name": CS[8]["name"]}
    syn = json.loads(mx.config_text("__ABSENT__", {"source": "synthetic"}, CS))
    assert syn["source"] == "synthetic" and "url" not in syn and len(syn["rtsp_urls"]) == 10


def test_readiness_and_blockers():
    s4_help = "usage: … [--codec {jpeg,h264}] [--outage PERIOD:DURATION]"
    r = mx.readiness(s4_help, "", {"type": "rtsp", "state": "streaming"})
    assert r == {"server_transports": False, "server_codec": "jpeg", "board_transport": False}
    r5 = mx.readiness("--transports TRANSPORTS --codec {mjpeg,h264}", "/x/videosources.py", {})
    assert r5 == READY
    assert mx.readiness("", "", {"transport": "udp"})["board_transport"]
    assert all(mx.cell_blocker(c, READY) is None for c in CS)
    # S4-сервер и блок без transport: доказательна только auto@tcp,udp (транспорт tcp interleaved)
    ok = [c["id"] for c in CS if mx.cell_blocker(c, r) is None]
    assert ok == ["rtsp:2", "rtsp:5"]
    assert "сервер без --transports" in mx.cell_blocker(CS[6], r)
    half = dict(READY, board_transport=False)
    assert "блок не знает" in mx.cell_blocker(CS[0], half) and mx.cell_blocker(CS[6], half) is None


SOCKS = """PID 4242
ARG protocols=udp
INO 1001 1002 1003 77
UDP
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops
  12: 00000000:C350 00000000:0000 07 00000000:00000000 00:00000000 00000000  1000        0 1001 2 0 0
  13: 00000000:C351 00000000:0000 07 00000000:00000000 00:00000000 00000000  1000        0 1002 2 0 0
  14: 00000000:0277 00000000:0000 07 00000000:00000000 00:00000000 00000000  1000        0 9999 2 0 0
TCP
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: AB01A8C0:D3A2 5C01A8C0:2199 01 00000000:00000000 00:00000000 00000000  1000        0 1003 1 0 20 4 30 10 -1
   1: AB01A8C0:D3A4 5C01A8C0:1D1C 01 00000000:00000000 00:00000000 00000000  1000        0 5555 1 0 20 4 30 10 -1
"""


def test_parse_sockets_udp_and_tcp():
    s = mx.parse_sockets(SOCKS, 8601)                  # 0x2199 = 8601
    assert s == {"pid": "4242", "protocols": "protocols=udp", "udp": 2, "tcp_server": 1}
    tcp_only = SOCKS.replace("INO 1001 1002 1003 77", "INO 1003")
    assert mx.parse_sockets(tcp_only, 8601) == {"pid": "4242", "protocols": "protocols=udp", "udp": 0, "tcp_server": 1}
    assert mx.parse_sockets(tcp_only, 8602)["tcp_server"] == 0
    assert mx.parse_sockets("NOPROC", 8601) is None and mx.parse_sockets("", 8601) is None


def test_actual_transport_status_first_sockets_crosscheck():
    udp = {"udp": 2, "tcp_server": 1}
    tcp = {"udp": 0, "tcp_server": 1}
    assert mx.actual_transport(udp) == ("udp", "sockets", None)
    assert mx.actual_transport(tcp, {"type": "rtsp"}) == ("tcp", "sockets", None)
    assert mx.actual_transport(tcp, {"transport": "TCP"}) == ("tcp", "status", None)
    tr, src, mism = mx.actual_transport(udp, {"transport": "tcp"})
    assert (tr, src) == ("tcp", "status") and "сокеты udp" in mism
    assert mx.actual_transport(None, {}) == (None, None, None)
    assert mx.actual_transport({"udp": 0, "tcp_server": 0}) == (None, None, None)


def good(**kw):
    m = {"streaming_s": 2.1, "console_live_s": 3.0, "frames_advancing": True, "frames": [100, 150],
         "transport": "udp", "transport_source": "sockets", "transport_mismatch": None, "remote_states_seen": ["streaming"]}
    m.update(kw)
    return m


def test_judge_positive_cells():
    assert mx.judge_cell(CS[0], good()) == ("PASS", [])
    v, p = mx.judge_cell(CS[0], good(transport="tcp"))
    assert v == "FAIL" and "ожидался udp" in p[0]
    assert mx.judge_cell(CS[2], good(transport="tcp"))[0] == "PASS"            # auto: любой
    assert mx.judge_cell(CS[6], good(transport="tcp"))[0] == "PASS"            # auto @ только tcp
    assert mx.judge_cell(CS[7], good(transport="tcp"))[0] == "FAIL"            # auto @ только udp, а ушёл в tcp
    assert mx.judge_cell(CS[1], good(transport=None, transport_source=None))[0] == "ERROR"
    v, p = mx.judge_cell(CS[1], good(streaming_s=None, console_live_s=None, remote_states_seen=["connect-error"]))
    assert v == "FAIL" and len(p) == 2
    assert mx.judge_cell(CS[4], good(frames_advancing=False, transport="tcp"))[0] == "FAIL"
    assert mx.judge_cell(CS[0], good(transport_mismatch="статус tcp, сокеты udp"))[0] == "FAIL"
    assert mx.judge_cell(CS[0], good(), blocker="контракт NETWORK не развёрнут: x") == \
        ("ERROR", ["контракт NETWORK не развёрнут: x"])


def test_judge_unavailable_transport_cells():
    ok = {"streaming_s": None, "remote_states_seen": ["connect-error", "starting"], "console_live_end": False,
          "recover": {"ok": True, "remote_s": 2.0, "console_s": 3.1}}
    assert mx.judge_cell(CS[8], ok) == ("PASS", [])
    hang = dict(ok, recover={"ok": False})
    v, p = mx.judge_cell(CS[8], hang)
    assert v == "FAIL" and "зависание" in p[0]
    streamed = dict(ok, streaming_s=3.0, remote_states_seen=["streaming"], console_live_end=True)
    v, p = mx.judge_cell(CS[9], streamed)
    assert v == "FAIL" and len(p) == 3
    assert mx.judge_cell(CS[8], dict(ok, remote_states_seen=["error"]))[0] == "PASS"


def test_gui_matrix_judgement():
    def st(n, changed=True, inp="rtsp"):
        return {"shot": f"{2 * n + 3:02d}-{2 * n + 4:02d}-selected-rtsp_{n}-video-t+1s.png", "frame_changed": changed,
                "frame_dark": 0.99, "videoSource": {"input": inp}}
    states = [{"shot": "01-02-stream-video-t+1s.png"}] + [st(n) for n in range(8)]
    assert mx.judge_gui_matrix(states, CS) == ("PASS", [])
    v, p = mx.judge_gui_matrix(states[:4] + [st(3, changed=False)] + states[5:], CS)
    assert v == "FAIL" and "rtsp:3" in p[0]
    v, p = mx.judge_gui_matrix(states[:3] + [{"shot": "select-error", "select_error": {"code": "unavailable"}}], CS)
    assert v == "FAIL" and "unavailable" in p[-1]
    assert mx.judge_gui_matrix([], CS)[0] == "ERROR"
    assert mx.judge_gui_matrix(states[:1] + [st(2), st(5)], [CS[2], CS[5]]) == ("PASS", [])


def test_remote_rtsp_predicate_checks_address():
    d = {"video": {"source": {"type": "rtsp", "state": "streaming", "device": CS[1]["url"]}}}
    assert mx.remote_rtsp(CS[1]["url"])(d) and not mx.remote_rtsp(CS[0]["url"])(d)
    d["video"]["source"]["state"] = "connect-error"
    assert not mx.remote_rtsp(CS[1]["url"])(d)


def test_sock_cmd_does_not_match_own_shell_and_no_root():
    assert "'[g]st-launch-1.0.*rtspsrc'" in mx.SOCK_CMD and "sudo" not in mx.SOCK_CMD
    assert "/proc/net/udp" in mx.SOCK_CMD and "/proc/net/tcp6" in mx.SOCK_CMD


def test_plan_without_ssh(capsys):
    assert mx.main(["--plan", "--gui"]) == 0
    out = capsys.readouterr().out
    for needle in ("S5-02", "S5-03", "S5-07", "8604 mjpeg udp", "rtsp:9", "error без зависания"):
        assert needle in out
