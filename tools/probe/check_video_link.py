# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Испытания защищённого видеоканала pi2 → pi1 на физических платах (S3.1-VIDEO, ADR-0011). С машины владельца по SSH.

  python3 tools/probe/check_video_link.py --console 192.168.1.92 --remote 192.168.1.171 --out DIR [--only A,B]

Сценарии (DIR/<сценарий>.json, итог DIR/summary.json; PASS | FAIL | SKIP | ERROR):
  session      медиасеанс по TLS 1.3 с взаимной аутентификацией: обе стороны, URI, журнал блока
  unauth       посторонний узел (Mac) на порт видео: TCP без TLS, TLS без сертификата, чужой CA, CA стенда с чужой
               идентичностью — отказ у клиента + запись «video auth: reject» + нет «video: session from»
  stream       фактический H.264: пакеты, SPS (разрешение), источник; декодированные кадры на pi1; FPS источника,
               приёма и декодирования (измерены за окно), выход 640×360; потери (разрывы RTP seq, плохие пакеты)
  latency      транспорт pi2→pi1 (живой поток, смещение часов по PING/PONG) + конвейер кодирования pi2 и декодирования
               pi1 (трассировщик latency GStreamer, отдельные прогоны) → оценка сквозной задержки
  resources    60 с: CPU (одно ядро = 100 %; вся система из 4 ядер), RAM, температура, троттлинг обеих плат и процессов
               видео; сравнение с режимом без видео (pi1 3,0 % / 297 МБ, pi2 0,5 % / 149 МБ)
  telemetry    канал управления под нагрузкой видео: heartbeat/с, события, RTT, разрывы seq
  no-plain     нет UDP вне loopback и видеопортов UDP; видео — TCP 7444 ESTABLISHED между платами
  ctl-loss     пауза службы блока 8 с: пульт теряет управление → видео выключено; после — сеанс и видео снова
  sender-kill  SIGKILL генератора на pi2: видеосеанс кончается, пульт переподключается, видео восстановлено
  restart      штатный перезапуск sheregesh-remote-net: управление и видео восстанавливаются
  leftovers    после испытаний: на каждой плате ровно один конвейер видео текущего сеанса, сирот нет
Только синтетический videotestsrc; удалённых команд через лабораторный канал нет. Сигналы — процессам sheregesh-ui.
"""
import argparse
import json
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "software"))
sys.path.insert(0, str(ROOT / "tools/probe"))
CA_DIR = Path("~/.sheregesh-lab-ca").expanduser()
UI = "/run/sheregesh-ui/ui-state.json"
C_STATUS = "/run/sheregesh-ui/link-status.json"
R_STATUS = "/run/sheregesh-remote/link-status.json"
VPORT = 7444
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


def rv(rh):
    return jread(rh, R_STATUS).get("video") or {}


def playing(ch):
    return lambda: (lambda v: v.get("mode") == "remote-tls" and v.get("state") == "playing")(cv(ch))


def journal(host, unit, since, pattern):
    return ssh(host, f"journalctl -u {unit} --since @{int(since)} -o cat --no-pager | grep -E '{pattern}' | tail -8"
               ).splitlines()


def sc_session(ch, rh):
    if not wait(playing(ch), 90):
        return {"verdict": "FAIL", "reason": "видео на пульте не в playing за 90 с", "console": cv(ch), "remote": rv(rh)}
    c, r = cv(ch), rv(rh)
    link = c.get("link") or {}
    log = ssh(rh, "journalctl -u sheregesh-remote-net -o cat --no-pager | grep -E 'video: session from' | tail -1")
    ok = (link.get("tls") == "TLSv1.3" and (link.get("peer_uri") or "").startswith("urn:sheregesh:remote:")
          and r.get("active") and r.get("peer") == ch and r.get("transport") == "tls1.3-mutual"
          and "urn:sheregesh:console:" in log)
    return {"console_link": {k: link.get(k) for k in ("connected", "tls", "peer_uri", "host", "port")},
            "remote_server": {k: r.get(k) for k in ("active", "peer", "transport", "port", "sessions", "codec")},
            "remote_log": log, "verdict": "PASS" if ok else "FAIL"}


def sc_unauth(ch, rh):
    from check_link import classify_client, judge_attempt
    from sheregesh.link import pki
    if not wait(playing(ch), 60):
        return {"verdict": "ERROR", "reason": "нет штатного видео до сценария"}
    try:
        with socket.create_connection((rh, VPORT), timeout=5) as s:
            me = s.getsockname()[0]
    except OSError as e:
        return {"verdict": "ERROR", "reason": f"видеопорт недоступен: {e}"}

    def jl(p):
        return [x for x in ssh(rh, f"journalctl -b -u sheregesh-remote-net -o cat --no-pager | grep -E '{p}'").splitlines() if x]

    def attempt(fn):
        a0, s0 = len(jl(f"video auth: .*{me}")), len(jl(f"video: session from {me}"))
        try:
            client = classify_client(data=fn())
        except Exception as e:                                   # noqa: BLE001 — классифицируется
            client = classify_client(exc=e)
        time.sleep(1.0)
        auth, sess = jl(f"video auth: .*{me}")[a0:], jl(f"video: session from {me}")[s0:]
        if client[0] == "unknown" and auth and not sess:       # закрыто после TLS без данных SHRG-LAB
            client = ("closed", client[1])
        return {"client": {"kind": client[0], "detail": client[1]}, "server_log": auth, "server_sessions": sess,
                "result": judge_attempt(client, auth, sess)}

    def plain():
        with socket.create_connection((rh, VPORT), timeout=5) as s:
            s.sendall(b"\x02\x00\x00\x00\x14" + bytes(8) + b"\x80\x60" + bytes(10))   # «RTP» без TLS
            s.settimeout(8)
            return s.recv(4096)

    def tls_try(cert=None, key=None):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        if cert:
            ctx.load_cert_chain(cert, key)
        with socket.create_connection((rh, VPORT), timeout=5) as raw, ctx.wrap_socket(raw) as s:
            s.settimeout(8)
            return s.recv(4096)
    res = {"plain_tcp": attempt(plain), "tls_no_cert": attempt(tls_try)}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pki.ca_init(tmp / "rogue", name="Rogue CA")
        d = tmp / "rogue-dev"
        pki.install(d, pki.ca_sign(tmp / "rogue", pki.keygen("console", "ROGUE", d), "console", "ROGUE"),
                    (tmp / "rogue" / "ca.crt").read_text(), "console", "ROGUE")
        res["tls_rogue_ca"] = attempt(lambda: tls_try(str(d / "device.crt"), str(d / "device.key")))
        if (CA_DIR / "ca.key").exists():
            d2 = tmp / "intruder"
            pki.install(d2, pki.ca_sign(CA_DIR, pki.keygen("console", "INTRUDER", d2), "console", "INTRUDER"),
                        (CA_DIR / "ca.crt").read_text(), "console", "INTRUDER")
            res["tls_lab_ca_wrong_identity"] = attempt(lambda: tls_try(str(d2 / "device.crt"), str(d2 / "device.key")))
        else:
            res["tls_lab_ca_wrong_identity"] = {"result": "SKIPPED", "reason": f"нет CA стенда в {CA_DIR}"}
    still = wait(playing(ch), 30)
    results = [v["result"] for v in res.values()]
    verdict = ("FAIL" if "ACCEPTED" in results else "ERROR" if "INCONCLUSIVE" in results
               else "SKIP" if "SKIPPED" in results else "PASS" if still is not None else "FAIL")
    return {"outsider_addr": me, "attempts": res, "console_video_after_s": still, "verdict": verdict}


def sc_stream(ch, rh, window=20.0):
    if not wait(playing(ch), 60):
        return {"verdict": "FAIL", "reason": "нет видео", "console": cv(ch)}
    c0, r0, t0 = cv(ch), rv(rh), time.time()
    time.sleep(window)
    c1, r1, t1 = cv(ch), rv(rh), time.time()
    dt = t1 - t0
    l0, l1 = c0.get("link") or {}, c1.get("link") or {}
    same = l0.get("connected") and c1.get("sessions") == c0.get("sessions")
    d = lambda a, b, k: (b.get(k) or 0) - (a.get(k) or 0)                       # noqa: E731
    m = {"window_s": round(dt, 1), "same_session": bool(same),
         "sender_frames": d(r0, r1, "frames"), "sender_fps": round(d(r0, r1, "frames") / dt, 2),
         "received_frames": d(l0, l1, "frames"), "received_fps": round(d(l0, l1, "frames") / dt, 2),
         "decoded_frames": d(c0, c1, "frames_decoded"), "decoded_fps": round(d(c0, c1, "frames_decoded") / dt, 2),
         "packets": d(l0, l1, "packets"), "bitrate_kbit_s": round(d(l0, l1, "bytes") * 8 / dt / 1000, 1),
         "seq_gaps": d(l0, l1, "seq_gaps"), "lost_packets": d(l0, l1, "lost_packets"), "bad_rtp": d(l0, l1, "bad_rtp"),
         "decoder_restarts": d(c0, c1, "restarts")}
    sps, src = l1.get("stream_sps") or {}, l1.get("source") or {}
    res = {"metrics": m, "stream_sps": sps, "source_announced": src, "decoder_output": c1.get("output"),
           "codec": c1.get("codec"), "console_state": c1.get("state")}
    ok = (same and m["received_frames"] > 0 and m["decoded_frames"] > 0 and sps.get("width") == 1280
          and sps.get("height") == 720 and c1.get("codec") == "h264" and c1.get("output") == "640x360"
          and m["seq_gaps"] == 0 and m["bad_rtp"] == 0 and m["decoder_restarts"] == 0
          and m["decoded_fps"] >= 0.9 * m["received_fps"] and m["sender_fps"] > 0)
    res["fps_target_30"] = {"sender": m["sender_fps"], "decoded": m["decoded_fps"], "meets_29": m["decoded_fps"] >= 29.0}
    return {**res, "verdict": "PASS" if ok else "FAIL"}


TRACE = ("GST_DEBUG='GST_TRACER:7' GST_TRACERS='latency(flags=pipeline+element)' GST_DEBUG_NO_COLOR=1 "
         "timeout 20 gst-launch-1.0 -q {pipe} 2>&1 >/dev/null | grep -E '^.*(element-latency|: latency,)' | head -4000")
ENC = ("videotestsrc is-live=true num-buffers=240 pattern=smpte ! video/x-raw,width=1280,height=720,framerate=30/1 ! "
       "timeoverlay ! videoconvert ! video/x-raw,format=I420 ! v4l2h264enc "
       "extra-controls=controls,repeat_sequence_header=1,video_bitrate=2000000,h264_i_frame_period=30 ! "
       "video/x-h264,level=(string)4 ! h264parse ! rtph264pay config-interval=1 pt=96 ! rtpstreampay ! fakesink sync=false")
DEC = ("videotestsrc is-live=true num-buffers=240 pattern=smpte ! video/x-raw,width=1280,height=720,framerate=30/1 ! "
       "videoconvert ! video/x-raw,format=I420 ! v4l2h264enc "
       "extra-controls=controls,repeat_sequence_header=1,video_bitrate=2000000,h264_i_frame_period=30 ! "
       "video/x-h264,level=(string)4 ! h264parse ! rtph264pay config-interval=1 pt=96 ! rtpstreampay ! "
       "rtpstreamdepay ! rtpjitterbuffer latency=50 ! rtph264depay ! h264parse ! v4l2h264dec ! videoconvert ! "
       "videoscale ! video/x-raw,format=RGBx,width=640,height=360 ! fakesink sync=false")


def _trace(host, pipe):
    import re
    out = ssh(host, TRACE.format(pipe=pipe), timeout=60)
    pipe_lat, elem = [], {}
    for ln in out.splitlines():
        t = re.search(r"time=\(guint64\)(\d+)", ln)
        if not t:
            continue
        ms = int(t.group(1)) / 1e6
        if "element-latency" in ln:
            e = re.search(r"element=\(string\)([\w-]+)", ln)
            if e:
                elem.setdefault(re.sub(r"\d+$", "", e.group(1)), []).append(ms)
        else:
            pipe_lat.append(ms)
    med = lambda v: round(sorted(v)[len(v) // 2], 2) if v else None                # noqa: E731
    return {"pipeline_ms_p50": med(pipe_lat), "pipeline_ms_max": round(max(pipe_lat), 2) if pipe_lat else None,
            "samples": len(pipe_lat), "elements_ms_p50": {k: med(v) for k, v in sorted(elem.items())}}


def sc_latency(ch, rh):
    if not wait(playing(ch), 60):
        return {"verdict": "FAIL", "reason": "нет видео"}
    time.sleep(10)
    link = cv(ch).get("link") or {}
    tr = link.get("transport_latency_ms") or {}
    enc = _trace(rh, ENC)                            # pi2: источник → кодер → RTP (параллельно с живым сеансом)
    dec = _trace(ch, DEC)                            # pi1: тот же путь + приёмная часть; приёмная — по элементам
    recv_part = sum(v for k, v in (dec["elements_ms_p50"] or {}).items() if v is not None and k in
                    ("rtpstreamdepay", "rtpjitterbuffer", "rtph264depay", "h264parse", "v4l2h264dec", "videoconvert",
                     "videoscale")) if dec["elements_ms_p50"] else None
    est = None
    if enc["pipeline_ms_p50"] is not None and tr.get("p50") is not None and recv_part is not None:
        est = round(enc["pipeline_ms_p50"] + tr["p50"] + recv_part, 1)
    res = {"transport_pi2_to_pi1_ms": tr, "ping_rtt_ms": link.get("ping_rtt_ms"),
           "sender_pipeline_pi2": enc, "decode_pipeline_pi1": dec, "receiver_part_ms_p50_sum": recv_part,
           "end_to_end_estimate_ms_p50": est,
           "method": "транспорт — живой поток (время отправки каждого пакета, смещение часов по PING/PONG с мин. RTT); "
                     "конвейеры — трассировщик latency GStreamer в отдельных прогонах; сумма — оценка, не измерение "
                     "«стекло-стекло» (без дисплея и камеры)"}
    ok = tr.get("samples", 0) > 50 and tr.get("p50") is not None and enc["samples"] > 0 and dec["samples"] > 0
    return {**res, "verdict": "PASS" if ok else ("ERROR" if not enc["samples"] or not dec["samples"] else "FAIL")}


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


def sc_no_plain(ch, rh):
    from check_link import _decode
    if not wait(playing(ch), 60):
        return {"verdict": "FAIL", "reason": "нет видео"}
    out, bad = {}, []
    for role, h in (("console", ch), ("remote", rh)):
        rows = [ln.split() for ln in ssh(h, LISTEN).splitlines() if ln.strip()]
        udp = sorted({_decode(r[0], r[1]) for r in rows if r[0].startswith("udp")})
        udp_ext = [u for u in udp if not u.split()[1].startswith(("127.", "::1")) and not u.endswith((":68", ":546"))]
        est = sorted({f"{_decode(r[0], r[1]).split()[1]} -> {_decode(r[0], r[2]).split()[1]}" for r in rows
                      if r[0].startswith("tcp") and r[3] == "01" and (r[1].endswith(":1D14") or r[2].endswith(":1D14"))})
        out[role] = {"udp_sockets": udp, "udp_non_loopback_not_dhcp": udp_ext, "tcp_7444_established": est}
        bad += [f"{role}: {u}" for u in udp_ext]
        bad += [f"{role}: udp video port {u}" for u in udp if u.endswith((":5600", ":5004"))]
    ok = not bad and out["console"]["tcp_7444_established"] and out["remote"]["tcp_7444_established"]
    return {**out, "violations": bad, "note": "внешний захват трафика без root недоступен; доказательство — сокеты "
                                             "обеих плат (UDP видео нет, видео — одно TCP 7444 между платами) и отказы "
                                             "сценария unauth", "verdict": "PASS" if ok else "FAIL"}


def _sessions(ch):
    return cv(ch).get("sessions") or 0


def sc_ctl_loss(ch, rh, pause=8):
    if not wait(playing(ch), 60):
        return {"verdict": "ERROR", "reason": "нет видео до сценария"}
    pid = ssh(rh, "systemctl show -p MainPID --value sheregesh-remote-net")
    if not pid.isdigit() or pid == "0":
        return {"verdict": "ERROR", "reason": f"нет PID блока: {pid!r}"}
    n0 = _sessions(ch)
    t0 = time.monotonic()
    try:
        ssh(rh, f"kill -STOP {pid}")
        video_off = wait(lambda: cv(ch).get("mode") == "off", pause + 10, 0.5)
        during = cv(ch)
        time.sleep(max(0.0, pause - (time.monotonic() - t0)))
    finally:
        resumed = ssh(rh, f"kill -CONT {pid} 2>/dev/null && echo resumed || echo gone")
    back = wait(lambda: playing(ch)() and _sessions(ch) > n0, 90, 0.5)
    res = {"pause_s": pause, "video_off_after_s": video_off, "console_video_during": {k: during.get(k) for k in
           ("mode", "state", "error")}, "video_back_after_resume_s": back, "sessions_before": n0,
           "sessions_after": _sessions(ch), "final_cont": resumed,
           "remote_log": journal(rh, "sheregesh-remote-net", time.time() - 120, "video|session")}
    if resumed not in ("resumed", "gone"):
        return {**res, "verdict": "ERROR", "abort": True, "reason": "возобновление блока не подтверждено"}
    return {**res, "verdict": "PASS" if video_off is not None and back is not None else "FAIL"}


def sc_sender_kill(ch, rh):
    if not wait(playing(ch), 60):
        return {"verdict": "ERROR", "reason": "нет видео до сценария"}
    n0, t0 = _sessions(ch), time.time()
    pids = ssh(rh, "pgrep -u sheregesh-ui -f 'gst-launch-1.0.*rtpstreampay'").split()
    if not pids:
        return {"verdict": "ERROR", "reason": "нет генератора на pi2"}
    ssh(rh, f"kill -KILL {' '.join(pids)}")
    back = wait(lambda: playing(ch)() and _sessions(ch) > n0, 60, 0.5)
    left = [p for p in pids if ssh(rh, f"test -d /proc/{p} && echo y") == "y"]
    ctl = jread(ch, C_STATUS).get("phase")
    return {"killed": pids, "left": left, "video_back_s": back, "sessions": [n0, _sessions(ch)], "control_phase": ctl,
            "remote_log": journal(rh, "sheregesh-remote-net", t0, "video"),
            "verdict": "PASS" if back is not None and not left and ctl == "connected" else "FAIL"}


def sc_restart(ch, rh):
    if not wait(playing(ch), 60):
        return {"verdict": "ERROR", "reason": "нет видео до сценария"}
    n0 = _sessions(ch)
    t0 = time.monotonic()
    ssh(rh, "systemctl restart sheregesh-remote-net")
    ctl = wait(lambda: jread(ch, C_STATUS).get("phase") == "connected", 60)
    back = wait(lambda: playing(ch)() and _sessions(ch) > n0, 90, 0.5)
    return {"restart_cmd_and_ctl_s": round(time.monotonic() - t0, 1), "control_back_s": ctl, "video_back_s": back,
            "remote_service": ssh(rh, "systemctl show -p ActiveState,NRestarts --value sheregesh-remote-net").split(),
            "verdict": "PASS" if ctl is not None and back is not None else "FAIL"}


def sc_leftovers(ch, rh):
    if not wait(playing(ch), 60):
        return {"verdict": "ERROR", "reason": "нет видео"}
    out = {}
    for role, h, pat in (("console", ch, "gst-launch-1.0.*rtpstreamdepay"), ("remote", rh, "gst-launch-1.0.*rtpstreampay")):
        rows = ssh(h, f"ps -eo pid,ppid,pgid,stat,args --no-headers | grep -E '{pat}' | grep -v grep").splitlines()
        orphans = [r for r in rows if r.split()[1] == "1"]
        out[role] = {"video_pipelines": rows, "count": len(rows), "orphans": orphans}
    ok = all(v["count"] == 1 and not v["orphans"] for v in out.values())
    return {**out, "verdict": "PASS" if ok else "FAIL"}


SCENARIOS = {"session": sc_session, "unauth": sc_unauth, "stream": sc_stream, "latency": sc_latency,
             "resources": sc_resources, "telemetry": sc_telemetry, "no-plain": sc_no_plain, "ctl-loss": sc_ctl_loss,
             "sender-kill": sc_sender_kill, "restart": sc_restart, "leftovers": sc_leftovers}


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
