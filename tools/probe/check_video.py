# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S2-BOOT: синтетическое видео в штатных службах на плате (по SSH от sheregesh-ui).

  python3 tools/probe/check_video.py --host 192.168.1.88 --board pi1 --tag service --out DIR [--restart]

Проверяет без изменения системы (кроме --restart — штатного перезапуска sheregesh-remote-emu, разрешённого polkit):
- журнал службы эмулятора: «VIDEO codec=…», нет «VIDEO unavailable», нет предупреждений/ошибок службы;
- процессы: отправитель gst-launch (videotestsrc → кодер → udpsink 127.0.0.1) и приёмник пульта (udpsrc → декодер);
- поток (счётчики ядра за 5 с; /proc/<pid>/io процессов пульта из SSH-сеанса недоступен): UDP-датаграммы на loopback,
  очередь и потери сокета 127.0.0.1:5600, CPU процесса пульта (приём кадров); кадры/с на экране не измеряются;
- UDP 5600 привязан только к 127.0.0.1 (ADR-0007, loopback);
- отрицательные пробы на плате: заведомо сломанные конвейеры (нет элемента, недопустимый формат для v4l2h264enc,
  заглушка/неверные параметры openh264) должны отбраковываться video._pipeline_ok — исправление предупреждений GLib
  не скрывает настоящие ошибки.
Это не проверка GUI: дисплея нет, видимость кадров на экране не проверяется.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REMOTE = r'''
import json, os, re, subprocess, sys, time
sys.path.insert(0, "/usr/lib/sheregesh/software")
from sheregesh import video
out = {}
j = subprocess.run(["journalctl", "-b", "-u", "sheregesh-remote-emu", "-o", "cat", "--no-pager"], capture_output=True, text=True).stdout
out["video_lines"] = [l for l in j.splitlines() if l.startswith("VIDEO")]
out["restarts"] = subprocess.run(["systemctl", "show", "-p", "NRestarts", "--value", "sheregesh-remote-emu", "sheregesh-console"],
                                 capture_output=True, text=True).stdout.split()
out["unit_warnings"] = subprocess.run(["journalctl", "-b", "-p", "warning", "-u", "sheregesh-remote-emu", "-u", "sheregesh-console",
                                       "-o", "cat", "--no-pager"], capture_output=True, text=True).stdout.strip().splitlines()[-20:]
procs = []
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace").strip()
        st = os.stat(f"/proc/{pid}")
    except OSError:
        continue
    if cmd.startswith("gst-launch-1.0"):
        procs.append({"pid": int(pid), "uid": st.st_uid, "cmd": cmd})
out["gst"] = procs
def udp_in():
    rows = [l.split() for l in open("/proc/net/snmp") if l.startswith("Udp:")]
    return int(rows[1][rows[0].index("InDatagrams")])
def sock5600():
    for l in open("/proc/net/udp").read().splitlines()[1:]:
        f = l.split()
        if f[1].endswith(":15E0"):
            return int(f[4].split(":")[1], 16), int(f[-1])
    return None, None
def console_ticks():
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            if open(f"/proc/{pid}/cmdline", "rb").read().startswith(b"/usr/bin/python3\0-m\0sheregesh_console"):
                f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
                return int(f[11]) + int(f[12])
        except OSError:
            pass
    return None
i1, (q1, d1), c1, t0 = udp_in(), sock5600(), console_ticks(), time.time()
time.sleep(5)
i2, (q2, d2), c2, t1 = udp_in(), sock5600(), console_ticks(), time.time()
dt = t1 - t0
out["flow"] = {"udp_datagrams_per_s": round((i2 - i1) / dt, 1), "sock5600_rx_queue": q2,
               "sock5600_drops": [d1, d2], "console_cpu_pct": None if c1 is None or c2 is None else
               round((c2 - c1) / os.sysconf("SC_CLK_TCK") / dt * 100, 1)}
udp = []
for l in open("/proc/net/udp").read().splitlines()[1:]:
    a = l.split()[1]
    ip, port = a.split(":")
    if int(port, 16) == 5600:
        udp.append(".".join(str(int(ip[i:i + 2], 16)) for i in (6, 4, 2, 0)))
out["udp5600"] = udp
v = video
neg = {
  "missing_element": ["nosuchelement_sheregesh"],
  "v4l2_bad_format": ["videoconvert", "!", "video/x-raw,format=RGB", "!", "v4l2h264enc", "!", "fakesink"],
  "v4l2_bad_level": ["v4l2h264enc", "!", "video/x-h264,level=(string)9", "!", "h264parse"],
}
if v.has_element("openh264enc"):
    neg["openh264_bad_bitrate"] = ["openh264enc", "bitrate=2000000"]
v.PROBE_FAILURES.clear()
out["negative"] = {k: v._pipeline_ok(e) for k, e in neg.items()}
out["negative_reasons"] = list(v.PROBE_FAILURES)
v.PROBE_FAILURES.clear()
out["positive"] = {"v4l2_pair": v._pipeline_ok([*v.V4L2_ENC, "!", "h264parse", "!", "v4l2h264dec"]),
                   "jpeg": v._pipeline_ok([*v.ENCODERS["jpeg"], "!", "rtpjpegdepay", "!", "jpegdec"])}
out["positive_reasons"] = list(v.PROBE_FAILURES)
print(json.dumps(out))
'''


def ssh(host, cmd, inp=None, timeout=180):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"sheregesh-ui@{host}", cmd],
                          input=inp, capture_output=True, text=True, timeout=timeout)


def check(host):
    r = ssh(host, "python3 -", REMOTE)
    if r.returncode:
        raise SystemExit(f"проверка на плате не выполнилась: {r.stderr[-500:]}")
    d = json.loads(r.stdout.strip().splitlines()[-1])
    last = d["video_lines"][-1] if d["video_lines"] else ""
    tx = [p for p in d["gst"] if "videotestsrc" in p["cmd"]]
    rx = [p for p in d["gst"] if "udpsrc" in p["cmd"]]
    flow = d.get("flow", {})
    d["summary"] = {"video_line": last, "codec": (last.split("codec=")[1].split()[0] if "codec=" in last else None),
                    "sender": tx[0]["cmd"][:200] if tx else None, "receiver": rx[0]["cmd"][:200] if rx else None,
                    "flow": flow, "udp5600": d["udp5600"]}
    d["checks"] = {
        "codec_selected": last.startswith("VIDEO codec="),
        "no_unavailable": not any("unavailable" in x for x in d["video_lines"][-1:]),
        "sender_running": bool(tx) and tx[0]["uid"] != 1000,            # отправитель — в службе (DynamicUser), не в сеансе
        "sender_loopback": bool(tx) and "host=127.0.0.1" in tx[0]["cmd"],
        "receiver_running": bool(rx),
        "stream_flowing": (flow.get("udp_datagrams_per_s") or 0) >= 100 and flow.get("sock5600_drops", [0, 1])[0] ==
        flow.get("sock5600_drops", [0, 1])[1] and (flow.get("sock5600_rx_queue") or 0) < 65536 and
        (flow.get("console_cpu_pct") or 0) >= 5,
        "udp_loopback_only": bool(d["udp5600"]) and all(x.startswith("127.") for x in d["udp5600"]),
        "no_unit_warnings": not d["unit_warnings"],
        "negative_probes_rejected": bool(d["negative"]) and not any(d["negative"].values()),
        "positive_probes_pass": all(d["positive"].values()),
    }
    d["verdict"] = "PASS" if all(d["checks"].values()) else "FAIL"
    return d


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True)
    ap.add_argument("--board", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--restart", action="store_true", help="штатный перезапуск sheregesh-remote-emu перед проверкой")
    a = ap.parse_args(argv)
    meta = {"board": a.board, "tag": a.tag, "host": a.host, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    info = ssh(a.host, "dpkg-query -W -f='${Version}' sheregesh; echo; tr -d '\\0' </proc/device-tree/serial-number; echo")
    meta["package"], meta["serial"] = (info.stdout.splitlines() + ["", ""])[:2]
    if a.restart:
        t0 = time.time()
        r = ssh(a.host, "S=$(date +%s); systemctl restart sheregesh-remote-emu && for i in $(seq 1 60); do "
                        "journalctl -u sheregesh-remote-emu --since @$S -o cat --no-pager | grep -q '^VIDEO' && break; sleep 1; done; "
                        "journalctl -u sheregesh-remote-emu --since @$S -o cat --no-pager | grep -E '^(LISTEN|VIDEO)'; sleep 8")
        meta["restart"] = {"output": r.stdout.strip(), "rc": r.returncode, "seconds": round(time.time() - t0, 1)}
    d = {**meta, **check(a.host)}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{a.board}-video-{a.tag}.json"
    path.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    print(json.dumps({"file": str(path), "verdict": d["verdict"], "checks": d["checks"], **d["summary"],
                      "negative": d["negative"], "restart": meta.get("restart")}, ensure_ascii=False, indent=1))
    return 0 if d["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
