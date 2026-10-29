# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S8 (ADR-0014, раздел 3.1): эмулятор ELRS TX — ответы только после верного кадра хоста, device info на ping,
синхронизация и link statistics раз в 200 мс, каналы и взведение, uart-disconnected без кадров > 1 с, сценарии
silent/garbage/crc/heal, детерминизм; связка с CrsfOutput в одном процессе (LoopbackPort) и дочерний процесс на pty."""
import json
import select
import subprocess
import sys

from sheregesh.crsf import frames as F
from sheregesh.crsf.output import CrsfOutput
from sheregesh.crsf.port import PtyPort
from sheregesh.module.lab import child_env
from sheregesh_emulators import elrs_tx as E
from sheregesh_emulators.devices import SimClock

CH = [250, -250, 1000, -1000, 0, 0, 0, 1]


def emu(**kw):
    c = SimClock(10.0)
    return c, E.ElrsTxEmulator(c, **kw)


def host_frames(e, parser=None):
    p = parser or F.Parser()
    return p.feed(e.drain_tx())


def channels(armed=False):
    return F.pack_channels(CH + [0] * 8, armed)


def test_replies_only_after_valid_host_frame():
    c, e = emu()
    assert e.drain_tx() == b"" and e.state()["host"] == "uart-disconnected"
    bad = bytearray(F.ping())
    bad[-1] ^= 1
    e.feed(bytes(bad))
    assert e.drain_tx() == b"" and e.parser.crc_errors == 1 and e.state()["host"] == "uart-disconnected"
    e.feed(F.ping())
    got = host_frames(e)
    assert [fr.type for fr in got] == [F.T_DEVICE_INFO, F.T_RADIO]          # device info первым, затем sync
    info = F.device_info(got[0].payload)
    assert info.name == "RM Nomad X-Band (emu)" and info.sw == "4.1.0" and info.is_elrs
    assert F.timing(got[1].payload).interval_us == 20000
    assert e.step() == [{"ev": "host", "state": "connected"}]
    c.advance(0.1)
    e.feed(channels())
    assert [fr.type for fr in host_frames(e)] == [F.T_LINK_STATS]           # stats сдвинуты на 100 мс


def test_sync_and_stats_every_200ms_and_channels():
    c, e = emu(period_us=10000)
    p = F.Parser()
    seen = []
    for i in range(100):                                                    # 1 с кадров по 10 мс
        e.feed(channels(armed=i >= 50))
        seen += [(round(c() - 10.0, 3), fr.type) for fr in p.feed(e.drain_tx())]
        c.advance(0.01)
    syncs = [t for t, k in seen if k == F.T_RADIO]
    stats = [t for t, k in seen if k == F.T_LINK_STATS]
    assert syncs == [0.0, 0.2, 0.4, 0.6, 0.8] and stats == [0.1, 0.3, 0.5, 0.7, 0.9]
    s = e.state()
    assert s["ch"] == [F.axis_to_crsf(v) for v in CH] + [992] * 8 and s["axes"] == CH
    assert s["armed"] is True and s["frame_len"] == 25 and s["rc_frames"] == 100
    assert s["rate_hz"] == 100.0 and s["interval_ms"] == 10.0 and s["jitter_us_p99"] == 0
    e.feed(F.pack_channels([0] * 16))
    assert e.state()["armed"] is None and e.state()["frame_len"] == 24


def test_uart_disconnected_after_1s_and_reconnect():
    c, e = emu()
    e.feed(channels())
    e.drain_tx()
    e.step()
    c.advance(1.0)
    assert e.step() == []
    c.advance(0.01)
    assert e.step() == [{"ev": "host", "state": "uart-disconnected"}]
    assert e.state()["host"] == "uart-disconnected" and e.drain_tx() == b""
    e.feed(channels())
    assert e.step() == [{"ev": "host", "state": "connected"}]
    assert [fr.type for fr in host_frames(e)] == [F.T_RADIO]                  # sync сразу после подключения


def test_scenarios_silent_crc_garbage_heal():
    c, e = emu()
    assert e.lab("silent") and not e.lab("explode")
    e.feed(F.ping())
    assert e.drain_tx() == b"" and e.state()["host"] == "connected" and e.state()["pings"] == 1
    e.lab("crc")
    c.advance(0.2)
    e.feed(F.ping())
    p = F.Parser()
    assert p.feed(e.drain_tx()) == [] and p.crc_errors >= 2
    e.lab("garbage")
    outs = []
    for seed in (5, 5, 6):
        c2, e2 = emu(seed=seed)
        e2.lab("garbage")
        e2.feed(F.ping())
        outs.append(e2.drain_tx())
    assert outs[0] == outs[1] != outs[2]                                      # детерминизм по seed
    p = F.Parser()
    assert [fr.type for fr in p.feed(outs[0])] == [F.T_DEVICE_INFO, F.T_RADIO] and p.garbage_bytes >= 4
    e.lab("heal")
    c.advance(0.2)
    e.feed(F.ping())
    assert [fr.type for fr in host_frames(e)][0] == F.T_DEVICE_INFO and e.state()["scenario"] == "normal"


def test_reply_budget():
    c, e = emu(max_reply=30)
    e.feed(F.ping())                                                          # info (40 байт) + sync — по одному
    assert [fr.type for fr in host_frames(e)] == [F.T_DEVICE_INFO]
    e.feed(channels())
    assert [fr.type for fr in host_frames(e)] == [F.T_RADIO]


def test_deterministic_same_input_same_bytes():
    def run():
        c, e = emu(seed=3)
        out = b""
        for i in range(60):
            if i == 20:
                e.lab("garbage")
            e.feed(channels() if i % 7 else F.ping())
            out += e.drain_tx()
            c.advance(0.0137)
        return out, e.state()

    assert run() == run()


def test_commands():
    c, e = emu()
    assert E.handle_command(e, {"op": "keepalive"}) == [{"ev": "alive"}]
    assert E.handle_command(e, {"op": "state"})[0]["ev"] == "state"
    r = E.handle_command(e, {"op": "lab", "scenario": "crc"})
    assert r[0] == {"ev": "lab", "scenario": "crc", "ok": True, "error": ""} and r[1]["scenario"] == "crc"
    assert E.handle_command(e, {"op": "lab", "scenario": "arm"})[0]["ok"] is False
    assert E.handle_command(e, {"op": "period", "us": 6666})[0] == {"ev": "period", "us": 6666, "ok": True}
    assert E.handle_command(e, {"op": "period", "us": 10})[0]["ok"] is False
    assert E.handle_command(e, {"op": "nope"}) == [] and E.handle_command(e, "junk") == []


def test_crsf_output_with_emulator_in_process():
    c = SimClock(100.0)
    e = E.ElrsTxEmulator(c, period_us=20000)
    mode = ["neutral"]
    out = CrsfOutput(E.LoopbackPort(e),        # live — свежий срок на каждое чтение, как от сторожа с потоком RC
                     lambda: (mode[0], CH if mode[0] == "live" else [0] * 8, 0, c() + 0.5), clock=c,
                     wait=lambda d: c.advance(max(0.0, d - c())))
    out.run(30)
    s = out.snapshot()
    assert s["synced"] and s["period_us"] == 20000 and s["device"]["name"] == "RM Nomad X-Band (emu)"
    assert e.state()["axes"] == [0] * 8 and e.state()["armed"] is False
    out.run(100)                                                              # 2 с: фаза сошлась
    st = e.state()
    assert abs(st["offset_us"]) < 1 and st["rate_hz"] == 50.0 and st["jitter_us_p99"] == 0
    mode[0] = "live"
    out.run(2)
    st = e.state()
    assert st["axes"] == CH and st["armed"] is True
    assert out.snapshot()["link"]["lq"] == 100 and out.snapshot()["crc_errors"] == 0
    e.lab("silent")
    out.run(150)                                       # молчание модуля: 100 × 20 мс до протухания sync, затем по 4 мс
    s = out.snapshot()
    assert s["last_rx_ms"] >= 2000 and s["device"] is None and not s["synced"] and s["period_us"] == 4000


def readline(proc, timeout=5.0):
    r, _, _ = select.select([proc.stdout], [], [], timeout)
    assert r, "no line from emulator"
    return json.loads(proc.stdout.readline())


def test_subprocess_on_pty():
    port = PtyPort()
    proc = subprocess.Popen([sys.executable, "-s", "-m", "sheregesh_emulators.elrs_tx", "--tty", port.path,
                             "--state-ms", "100"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=child_env())
    try:
        hello = readline(proc)
        assert hello["ev"] == "hello" and hello["tty"] == port.path and hello["period_us"] == 20000
        port.write(F.ping())
        p = F.Parser()
        got = []
        for _ in range(50):
            select.select([port.fileno()], [], [], 0.1)
            got += p.feed(port.read())
            if got:
                break
        assert got and got[0].type == F.T_DEVICE_INFO
        proc.stdin.write(b'{"op":"lab","scenario":"silent"}\n{"op":"state"}\n')
        proc.stdin.flush()
        msgs = [readline(proc) for _ in range(6)]
        lab = [m for m in msgs if m["ev"] == "lab"]
        assert lab == [{"ev": "lab", "scenario": "silent", "ok": True, "error": ""}]
        assert any(m["ev"] == "state" and m["scenario"] == "silent" and m["pings"] == 1 for m in msgs)
        proc.stdin.close()
        assert proc.wait(5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        port.close()
