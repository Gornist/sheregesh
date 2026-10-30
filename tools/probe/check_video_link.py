# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Испытания видео pi2 → pi1 на физических платах: SRTP через службы sheregesh-video-tx/rx (ADR-0016; TLS-видео
ADR-0011 удалено). С машины владельца по SSH.

  python3 tools/probe/check_video_link.py --console 192.168.1.92 --remote 192.168.1.171 --out DIR [--only A,B]

Сценарии (DIR/<сценарий>.json, итог DIR/summary.json; PASS | FAIL | SKIP | ERROR). Режим видео пульта обязан быть
remote-srtp (ui-state.json), состояние служб — state.json rx на пульте и tx на блоке:
  session      канал управления mTLS, rx и tx streaming по SRTP друг к другу, один сеанс, поколение и SSRC
  stream       пакеты tx и rx, потери и разрывы rx, кадры GUI, разрешение источника tx, выход 640×360
  latency      «захват → декодирован» (метка захвата pi2 → выход декодера pi1, смещение часов set-clock-offset)
  resources    60 с: CPU (одно ядро = 100 %; вся система из 4 ядер), RAM, температура, троттлинг обеих плат и процессов
               видео; сравнение с режимом без видео (pi1 3,0 % / 297 МБ, pi2 0,5 % / 149 МБ)
  telemetry    канал управления под нагрузкой видео: heartbeat/с, события, RTT, разрывы seq
  no-plain     на платах нет TCP-видеосервера (порт 7444 не слушается) и UDP прежних незашифрованных портов видео
  ctl-loss     пауза службы блока 8 с: «нет видео» ≤ 3 с, затем потеря управления → видео выключено; после —
               видео снова ≤ 10 с в новом сеансе SRTP (REQ-SW-002)
  restart      штатный перезапуск sheregesh-remote-net: управление и видео (новый сеанс SRTP) восстанавливаются
  leftovers    после испытаний: службы rx и tx в одном сеансе, не больше одного декодера GUI на пульте, сирот нет,
               на блоке нет gst-launch прежнего видеоканала
Только синтетический videotestsrc; удалённых команд через лабораторный канал нет. Сигналы — процессам sheregesh-ui.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "software"))
sys.path.insert(0, str(ROOT / "tools/probe"))
UI = "/run/sheregesh-ui/ui-state.json"
C_STATUS = "/run/sheregesh-ui/link-status.json"
R_STATUS = "/run/sheregesh-remote/link-status.json"
VSTATE = "/run/sheregesh-video/state.json"       # служба видео платы (контракт видео §4): rx на пульте, tx на блоке
VERDICTS = ("PASS", "FAIL", "SKIP", "ERROR")
BASELINE = {"console": {"cpu_busy_pct": 3.0, "mem_used_mb": 297}, "remote": {"cpu_busy_pct": 0.5, "mem_used_mb": 149}}


def ssh(host, cmd, timeout=120):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"sheregesh-ui@{host}", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


def jread(host, path):
    try:
        return json.loads(ssh(host, f"cat {path}") or "{}")
    except ValueError:
        return {}


def wait(pred, timeout, step=0.5):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return round(time.monotonic() - t0, 2)
        time.sleep(step)
    return None


def cv(ch):
    return jread(ch, UI).get("video") or {}


def vs(host):
    """Поток main службы видео платы (state.json) или {}."""
    return (jread(host, VSTATE).get("streams") or {}).get("main") or {}


def srtp(c):
    return c.get("mode") == "remote-srtp"


def playing(ch):
    return lambda: (lambda v: srtp(v) and v.get("state") == "playing")(cv(ch))


def journal(host, unit, since, pattern):
    return ssh(host, f"journalctl -u {unit} --since @{int(since)} -o cat --no-pager | grep -E '{pattern}' | tail -8"
               ).splitlines()


def judge_session_srtp(link_status, rx, tx, ch, rh):
    """SRTP (ADR-0016): сеанс управления mTLS и службы видео обеих плат в одном сеансе, поколении и SSRC, адреса —
    друг друга."""
    keys = ("state", "transport", "session", "gen", "ssrc", "peer")
    res = {"control": {k: link_status.get(k) for k in ("phase", "tls", "peer_uri", "host")},
           "rx": {k: rx.get(k) for k in keys}, "tx": {k: tx.get(k) for k in keys}}
    ok = (link_status.get("phase") == "connected" and link_status.get("tls") == "TLSv1.3"
          and (link_status.get("peer_uri") or "").startswith("urn:sheregesh:remote:")
          and rx.get("state") == tx.get("state") == "streaming" and rx.get("transport") == tx.get("transport") == "srtp"
          and rx.get("peer") == rh and tx.get("peer") == ch and bool(rx.get("session"))
          and all(rx.get(k) == tx.get(k) for k in ("session", "gen", "ssrc")))
    return {**res, "verdict": "PASS" if ok else "FAIL"}


def sc_session(ch, rh):
    if not wait(playing(ch), 90):
        return {"verdict": "FAIL", "reason": "видео пульта не remote-srtp playing за 90 с", "console": cv(ch),
                "remote": vs(rh)}
    return judge_session_srtp(jread(ch, C_STATUS), vs(ch), vs(rh), ch, rh)


def sc_stream(ch, rh, window=20.0):
    if not wait(playing(ch), 60):
        return {"verdict": "FAIL", "reason": "нет видео", "console": cv(ch)}
    c0, x0, t0 = cv(ch), vs(rh), time.time()
    time.sleep(window)
    c1, x1, t1 = cv(ch), vs(rh), time.time()
    return judge_stream_srtp(c0, c1, x0, x1, t1 - t0)


def delta(a, b, k):
    """Прирост счётчика за окно; None — счётчик не измерен в начале или в конце окна (контракт видео v9, §4:
    null не равен 0)."""
    x, y = a.get(k), b.get(k)
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)) or isinstance(x, bool) or isinstance(y, bool):
        return None
    return y - x


def _rate(v, dt):
    return None if v is None or dt <= 0 else round(v / dt, 2)


def judge_stream_srtp(c0, c1, x0, x1, dt):
    """Окно живого потока SRTP: link пульта (videorx: пакеты, разрывы, потери rx), кадры GUI, служба tx блока (пакеты,
    источник). «Ноль ошибок» — только измеренный ноль; reordered/auth_fail/replay_fail конвейер не измеряет (контракт
    v9) — в окне не требуются и показываются как есть."""
    l0, l1 = c0.get("link") or {}, c1.get("link") or {}
    same = (l0.get("gen") == l1.get("gen") and x0.get("session") == x1.get("session") and bool(x1.get("session"))
            and l1.get("gen") == x1.get("gen"))
    m = {"window_s": round(dt, 1), "same_session": bool(same),
         "tx_packets": delta(x0.get("rtp") or {}, x1.get("rtp") or {}, "packets"), "rx_packets": delta(l0, l1, "packets"),
         "decoded_frames": delta(c0, c1, "frames_decoded"), "seq_gaps": delta(l0, l1, "seq_gaps"),
         "lost_packets": delta(l0, l1, "lost_packets"), "decoder_restarts": delta(c0, c1, "restarts"),
         "jitter_ms": l1.get("jitter_ms"), "reordered": l1.get("reordered"), "auth_fail": l1.get("auth_fail"),
         "replay_fail": l1.get("replay_fail")}
    m["decoded_fps"] = _rate(m["decoded_frames"], dt)
    m["received_fps_5s"] = l1.get("received_fps_5s")
    src = x1.get("source") or {}
    res = {"transport": "srtp", "metrics": m, "source": {k: src.get(k) for k in ("kind", "encoder", "resolution", "fps")},
           "decoder_output": c1.get("output"), "codec": c1.get("codec"), "console_state": c1.get("state")}
    need = ("tx_packets", "rx_packets", "decoded_frames", "seq_gaps", "lost_packets", "decoder_restarts")
    unmeasured = [k for k in need if m[k] is None]
    if unmeasured:
        res["unmeasured"] = unmeasured
    ok = (not unmeasured and same and m["tx_packets"] > 0 and m["rx_packets"] > 0 and m["decoded_frames"] > 0
          and m["seq_gaps"] == 0 and m["lost_packets"] == 0 and m["decoder_restarts"] == 0
          and src.get("resolution") == "1280x720" and c1.get("codec") == "h264" and c1.get("output") == "640x360")
    dec = m["decoded_fps"]
    res["fps_target_30"] = {"decoded": dec, "meets_29": dec is not None and dec >= 29.0}
    return {**res, "verdict": "PASS" if ok else "FAIL"}


def sc_latency(ch, rh):
    """Измеренные на живом потоке части тракта. Трассировщик latency GStreamer на конвейере с v4l2h264enc записей не
    выдаёт (проверено на pi2) — кодирование на pi2 не измеряется и не подменяется оценкой."""
    if not wait(playing(ch), 60):
        return {"verdict": "FAIL", "reason": "нет видео"}
    time.sleep(20)
    return judge_latency(cv(ch).get("link") or {})


def judge_latency(link):
    """Задержка «захват → декодирован» (capture_to_decoded_ms, контракт видео v9, §4)."""
    c2d = link.get("capture_to_decoded_ms") or {}
    res = {"transport": link.get("transport"), "capture_to_decoded_ms": c2d, "ping_rtt_ms": link.get("ping_rtt_ms"),
           "method": "метка захвата кадра на pi2 (rtphdrextntp64, PTS источника) → выход декодера на pi1, "
                     "смещение часов set-clock-offset; включает кодер, сеть, джиттер-буфер и декодер"}
    if link.get("transport") != "srtp":
        return {**res, "verdict": "FAIL", "reason": "транспорт видео не srtp"}
    ok = (c2d.get("samples") or 0) > 50 and c2d.get("p50") is not None
    # «захват → декодирован» включает кодер, сеть и декодер — не бывает ≤ 0. Такое значение — ошибка смещения
    # часов (стенд 2026-10-29: p50 −49 мс получал PASS), а не измерение.
    low = [k for k in ("p50", "p95", "max") if isinstance(c2d.get(k), (int, float)) and c2d[k] <= 0]
    if low:
        res["reason"] = f"задержка ≤ 0 мс ({', '.join(low)}): смещение часов неверно, метрика не доказательна"
    return {**res, "verdict": "PASS" if ok and not low else "FAIL"}


SAMPLE = ("head -1 /proc/stat; grep -E '^cpu[0-9]' /proc/stat; grep -E 'MemTotal|MemAvailable' /proc/meminfo; "
          "vcgencmd measure_temp; vcgencmd get_throttled; getconf CLK_TCK; "
          "for p in $(pgrep -u sheregesh-ui -f 'gst-launch|sheregesh_remote|sheregesh_console|cage'); do "
          "echo \"P $p $(awk '{print $14+$15}' /proc/$p/stat) $(awk '/VmRSS/{print $2}' /proc/$p/status) "
          "$(tr '\\0' ' ' < /proc/$p/cmdline | cut -c1-60)\"; done")


def _sample(host):
    lines = ssh(host, SAMPLE).splitlines()
    cpu = [int(x) for x in lines[0].split()[1:]]
    cores = [[int(x) for x in ln.split()[1:]] for ln in lines[1:5] if ln.startswith("cpu")]
    mem = {ln.split(":")[0]: int(ln.split()[1]) for ln in lines if ln.startswith(("MemTotal", "MemAvailable"))}
    temp = next(ln.split("=")[-1] for ln in lines if ln.startswith("temp="))
    thr = next(ln.split("=")[-1] for ln in lines if ln.startswith("throttled="))
    tck = int(next(ln for ln in lines if ln.isdigit()))
    procs = {}
    for ln in lines:
        if ln.startswith("P "):
            _, pid, ticks, rss, *cmd = ln.split()
            procs[pid] = {"ticks": int(ticks), "rss_kb": int(rss or 0), "cmd": " ".join(cmd)}
    return {"t": time.time(), "cpu": cpu, "cores": cores, "mem": mem, "temp": temp, "throttled": thr, "tck": tck,
            "procs": procs}


def _busy(a, b):
    d = [y - x for x, y in zip(a, b)]
    tot, idle = sum(d), d[3] + (d[4] if len(d) > 4 else 0)
    return round(100 * (tot - idle) / tot, 1) if tot else None


def sc_resources(ch, rh, window=60.0):
    if not wait(playing(ch), 60):
        return {"verdict": "FAIL", "reason": "нет видео"}
    s0 = {"console": _sample(ch), "remote": _sample(rh)}
    time.sleep(window)
    s1 = {"console": _sample(ch), "remote": _sample(rh)}
    out = {}
    for k in s0:
        a, b = s0[k], s1[k]
        dt = b["t"] - a["t"]
        procs = {}
        for pid, p in b["procs"].items():
            if pid in a["procs"]:
                procs[pid] = {"cmd": p["cmd"], "cpu_one_core_pct": round(100 * (p["ticks"] - a["procs"][pid]["ticks"])
                                                                         / b["tck"] / dt, 1),
                              "rss_mb": round(p["rss_kb"] / 1024, 1)}
        out[k] = {"system_cpu_busy_pct_of_4_cores": _busy(a["cpu"], b["cpu"]),
                  "per_core_busy_pct": [_busy(x, y) for x, y in zip(a["cores"], b["cores"])],
                  "mem_used_mb": round((b["mem"]["MemTotal"] - b["mem"]["MemAvailable"]) / 1024),
                  "temp_start": a["temp"], "temp_end": b["temp"], "throttled": b["throttled"], "processes": procs,
                  "baseline_without_video": BASELINE[k]}
        out[k]["delta_vs_baseline"] = {"cpu_pct": round(out[k]["system_cpu_busy_pct_of_4_cores"] - BASELINE[k]["cpu_busy_pct"], 1),
                                       "mem_mb": out[k]["mem_used_mb"] - BASELINE[k]["mem_used_mb"]}
    ok = all(v["throttled"] == "0x0" for v in out.values()) and cv(ch).get("state") == "playing"
    return {"window_s": window, **out, "verdict": "PASS" if ok else "FAIL"}


def sc_telemetry(ch, rh, window=60.0):
    if not wait(playing(ch), 60):
        return {"verdict": "FAIL", "reason": "нет видео"}
    c0, r0, t0 = jread(ch, C_STATUS), jread(rh, R_STATUS), time.time()
    time.sleep(window)
    c1, r1, t1 = jread(ch, C_STATUS), jread(rh, R_STATUS), time.time()
    dt = t1 - t0
    s0, s1 = r0.get("stats", {}), r1.get("stats", {})
    m = {"seconds": round(dt, 1), "messages_per_s": round((c1["counters"]["received"] - c0["counters"]["received"]) / dt, 2),
         "seq_gaps": c1["counters"]["seq_gaps"] - c0["counters"]["seq_gaps"],
         "events_sent": s1["events_sent"] - s0["events_sent"], "events_acked": s1["events_acked"] - s0["events_acked"],
         "rtt_avg_ms": s1.get("rtt_avg_ms"), "rtt_max_ms": s1.get("rtt_max_ms"), "baseline_messages_per_s": 2.16}
    m["event_loss"] = m["events_sent"] - m["events_acked"]
    ok = (c1.get("phase") == "connected" and m["seq_gaps"] == 0 and m["events_sent"] > 0 and m["event_loss"] == 0
          and m["messages_per_s"] >= 0.9 * 2.16 and cv(ch).get("state") == "playing")
    return {"metrics": m, "video_state": cv(ch).get("state"), "verdict": "PASS" if ok else "FAIL"}


LISTEN = r"""for f in /proc/net/tcp /proc/net/tcp6 /proc/net/udp /proc/net/udp6; do
  awk -v f="${f##*/}" 'NR>1 {print f, $2, $3, $4}' "$f"; done"""


def judge_no_plain(rows_by_role):
    """Сокеты плат (/proc/net): TCP 7444 никто не слушает (TLS-видеосервера нет) и нет UDP прежних незашифрованных
    портов видео (5600, 5004). Видео — UDP SRTP 7444/7445 служб rx и tx (фильтр nftables — VIDEO-NFT)."""
    from check_link import _decode
    out, bad = {}, []
    for role, rows in rows_by_role.items():
        udp = sorted({_decode(r[0], r[1]) for r in rows if r[0].startswith("udp")})
        listen = sorted({_decode(r[0], r[1]) for r in rows if r[0].startswith("tcp") and r[3] == "0A"
                         and r[1].endswith(":1D14")})
        out[role] = {"udp_sockets": udp, "tcp_7444_listen": listen}
        bad += [f"{role}: listen {x}" for x in listen]
        bad += [f"{role}: udp video port {u}" for u in udp if u.endswith((":5600", ":5004"))]
    return {**out, "violations": bad, "verdict": "PASS" if not bad else "FAIL"}


def sc_no_plain(ch, rh):
    if not wait(playing(ch), 60):
        return {"verdict": "FAIL", "reason": "нет видео"}
    return judge_no_plain({role: [ln.split() for ln in ssh(h, LISTEN).splitlines() if ln.strip()]
                           for role, h in (("console", ch), ("remote", rh))})


def _sessions(ch):
    """Сеанс SRTP службы rx пульта (новый сеанс управления — новый id); «» — нет."""
    return vs(ch).get("session") or ""


def sc_ctl_loss(ch, rh, pause=8):
    """REQ-SW-002 п. 3: «нет видео» в GUI ≤ 3 с после обрыва (состояние приёмника не «playing»), видео снова ≤ 10 с
    после возобновления. Выключение видео по потере управления (mode «off») наступает по тайм-ауту heartbeat канала
    управления (link.yaml, 3 с) и критерием «нет видео» не является — записывается отдельно.
    Замер — с момента, когда ssh с kill -STOP вернулся (до него — установление ssh до pi2), до первого чтения
    ui-state.json с не-«playing»; дамп пишется раз в 1 с, поэтому значение — верхняя оценка (+≤ 1 с)."""
    if not wait(playing(ch), 60):
        return {"verdict": "ERROR", "reason": "нет видео до сценария"}
    pid = ssh(rh, "systemctl show -p MainPID --value sheregesh-remote-net")
    if not pid.isdigit() or pid == "0":
        return {"verdict": "ERROR", "reason": f"нет PID блока: {pid!r}"}
    n0 = _sessions(ch)
    t0 = time.monotonic()
    try:
        ssh(rh, f"kill -STOP {pid}")
        t0 = time.monotonic()
        no_video = wait(lambda: cv(ch).get("state") not in (None, "playing"), pause, 0.2)
        video_off = wait(lambda: cv(ch).get("mode") == "off", pause + 10, 0.5)
        video_off = None if video_off is None else round(time.monotonic() - t0, 2)
        during = cv(ch)
        time.sleep(max(0.0, pause - (time.monotonic() - t0)))
    finally:
        resumed = ssh(rh, f"kill -CONT {pid} 2>/dev/null && echo resumed || echo gone")
    back = wait(lambda: playing(ch)() and _sessions(ch) not in ("", n0), 90, 0.5)
    res = {"pause_s": pause, "no_video_after_s": no_video, "video_off_after_s": video_off,
           "console_video_during": {k: during.get(k) for k in ("mode", "state", "error")},
           "video_back_after_resume_s": back, "sessions_before": n0, "sessions_after": _sessions(ch),
           "final_cont": resumed, "criteria": {"no_video_after_s": "<= 3.0 (REQ-SW-002)",
                                               "video_back_after_resume_s": "<= 10.0 (REQ-SW-002)"},
           "remote_log": journal(rh, "sheregesh-remote-net", time.time() - 120, "video|session")}
    if resumed not in ("resumed", "gone"):
        return {**res, "verdict": "ERROR", "abort": True, "reason": "возобновление блока не подтверждено"}
    ok = no_video is not None and no_video <= 3.0 and video_off is not None and back is not None and back <= 10.0
    return {**res, "verdict": "PASS" if ok else "FAIL"}


def sc_restart(ch, rh):
    if not wait(playing(ch), 60):
        return {"verdict": "ERROR", "reason": "нет видео до сценария"}
    n0 = _sessions(ch)
    t0 = time.monotonic()
    ssh(rh, "systemctl restart sheregesh-remote-net")
    ctl = wait(lambda: jread(ch, C_STATUS).get("phase") == "connected", 60)
    back = wait(lambda: playing(ch)() and _sessions(ch) not in ("", n0), 90, 0.5)
    return {"restart_cmd_and_ctl_s": round(time.monotonic() - t0, 1), "control_back_s": ctl, "video_back_s": back,
            "remote_service": ssh(rh, "systemctl show -p ActiveState,NRestarts --value sheregesh-remote-net").split(),
            "verdict": "PASS" if ctl is not None and back is not None else "FAIL"}


def judge_leftovers_srtp(console_rows, remote_rows, rx, tx):
    """SRTP: конвейеры видео — внутри служб rx и tx (один поток main каждая, в одном сеансе); на пульте — не больше
    одного декодера GUI (gst-launch unixfdsrc, путь RGBx без дисплея; GL — в процессе GUI), на блоке — ни одного
    gst-launch прежнего видеоканала (rtpstreampay); сирот (родитель 1) нет."""
    orphans = [r for r in console_rows + remote_rows if r.split()[1:2] == ["1"]]
    out = {"console": {"gui_decoders": console_rows, "count": len(console_rows)},
           "remote": {"stream_pipelines": remote_rows, "count": len(remote_rows)}, "orphans": orphans,
           "rx": {k: rx.get(k) for k in ("state", "session", "gen")}, "tx": {k: tx.get(k) for k in ("state", "session", "gen")}}
    ok = (len(console_rows) <= 1 and not remote_rows and not orphans and rx.get("state") == tx.get("state") == "streaming"
          and bool(rx.get("session")) and rx.get("session") == tx.get("session") and rx.get("gen") == tx.get("gen"))
    return {**out, "verdict": "PASS" if ok else "FAIL"}


def sc_leftovers(ch, rh):
    if not wait(playing(ch), 60):
        return {"verdict": "ERROR", "reason": "нет видео"}
    ps = "ps -eo pid,ppid,pgid,stat,args --no-headers | grep -E '{}' | grep -v grep"
    return judge_leftovers_srtp(ssh(ch, ps.format("gst-launch-1.0.*unixfdsrc")).splitlines(),
                                ssh(rh, ps.format("gst-launch-1.0.*rtpstreampay")).splitlines(), vs(ch), vs(rh))


SCENARIOS = {"session": sc_session, "stream": sc_stream, "latency": sc_latency, "resources": sc_resources,
             "telemetry": sc_telemetry, "no-plain": sc_no_plain, "ctl-loss": sc_ctl_loss, "restart": sc_restart,
             "leftovers": sc_leftovers}


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
                      "release": ssh(h, "readlink /var/lib/sheregesh-deploy/current")}
    summary = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "boards": meta, "results": {}}
    aborted = None
    for name in a.only.split(","):
        print(f"== {name}", flush=True)
        if aborted:
            r = {"scenario": name, "verdict": "SKIP", "reason": f"не выполнялся: стенд не восстановлен после {aborted}"}
        else:
            started, t0 = time.strftime("%Y-%m-%dT%H:%M:%S%z"), time.monotonic()
            try:
                r = SCENARIOS[name](a.console, a.remote)
            except Exception as e:                               # noqa: BLE001 — сбой стенда — ERROR, не PASS
                r = {"verdict": "ERROR", "error": f"{type(e).__name__}: {e}"}
            if r.get("verdict") not in VERDICTS:
                r = {**r, "verdict": "ERROR", "error": f"неизвестный вердикт {r.get('verdict')!r}"}
            r = {"scenario": name, "started": started, "duration_s": round(time.monotonic() - t0, 1), **r}
            if r.get("abort"):
                aborted = name
        (a.out / f"{name}.json").write_text(json.dumps({"boards": meta, **r}, ensure_ascii=False, indent=1))
        summary["results"][name] = r["verdict"]
        print(f"   {r['verdict']}" + (f" — {r.get('reason') or r.get('error') or ''}" if r["verdict"] != "PASS" else ""),
              flush=True)
    summary["counts"] = {v: sum(x == v for x in summary["results"].values()) for v in VERDICTS}
    (a.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(json.dumps(summary["results"], ensure_ascii=False), summary["counts"])
    return 0 if all(v == "PASS" for v in summary["results"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
