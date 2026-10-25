#!/usr/bin/env python3
"""S5 с реальной RTSP-камерой shcam: транспорт udp/tcp/auto, обрыв камеры (SIGKILL mediamtx — как пропало питание
камеры; штатный stop mediamtx с rpiCamera висит до тайм-аута systemd 90 с), возврат. --loss-only — только обрыв."""
import json
import subprocess
import sys
import time

P1, P2 = "sheregesh-ui@192.168.1.92", "sheregesh-ui@192.168.1.171"
CAM = ["-o", "UserKnownHostsFile=~/.ssh/known_hosts.shcam", "cam@192.168.1.193"]
URL = "rtsp://192.168.1.193:8554/cam"
VI = "~/.config/sheregesh/video-input.json"


def ssh(host, cmd):
    args = ["ssh", *(host if isinstance(host, list) else [host]), cmd]
    return subprocess.run(args, capture_output=True, text=True, timeout=60).stdout.strip()


def remote():
    out = ssh(P2, "cat /run/sheregesh-remote/link-status.json")
    return (json.loads(out or "{}").get("video") or {}).get("source") or {}


def console():
    out = ssh(P1, "cat /run/sheregesh-ui/ui-state.json")
    return json.loads(out or "{}").get("videoSource") or {}


def sockets():
    """Транспорт rtspsrc по сокетам gst-launch блока: UDP (порты RTP) или только TCP к :8554."""
    return ssh(P2, "for p in $(pgrep -u sheregesh-ui -f '[r]tspsrc'); do ls -l /proc/$p/fd 2>/dev/null; done | "
                   "grep -o 'socket:\\[[0-9]*\\]' | tr -d 'socket:[]' | sort -u > /tmp/s; "
                   "u=$(awk 'NR>1{print $10}' /proc/net/udp /proc/net/udp6 | grep -cxFf /tmp/s); "
                   "t=$(awk 'NR>1{print $10}' /proc/net/tcp /proc/net/tcp6 | grep -cxFf /tmp/s); echo udp=$u tcp=$t")


def set_spec(transport):
    spec = json.dumps({"source": "rtsp", "url": URL, "codec": "h264", "transport": transport})
    ssh(P2, f"printf '%s' '{spec}' > {VI}.tmp && mv {VI}.tmp {VI} && "
            "kill -HUP $(systemctl show -p MainPID --value sheregesh-remote-net)")


def wait(pred, timeout, step=0.25):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return round(time.monotonic() - t0, 2)
        time.sleep(step)
    return None


res = {"transport": [], "loss": None}
orig = ssh(P2, f"cat {VI}")
try:
    for tr in (() if "--loss-only" in sys.argv else ("udp", "tcp", "auto")):
        set_spec(tr)
        t = wait(lambda: (r := remote()).get("transport_mode") == tr and r.get("state") == "streaming", 20)
        time.sleep(4)
        r, c = remote(), console()
        row = {"transport": tr, "ready_s": t, "state": r.get("state"), "status_transport": r.get("transport"),
               "sockets": sockets(), "size": f"{r.get('stream_width')}x{r.get('stream_height')}",
               "fps_actual": r.get("fps_actual"), "console": {k: c.get(k) for k in ("display", "live", "input")}}
        print(json.dumps(row, ensure_ascii=False), flush=True)
        res["transport"].append(row)
    # обрыв камеры при auto: mediamtx убит, через 6 с запущен
    set_spec("auto")
    wait(lambda: remote().get("transport_mode") == "auto" and remote().get("state") == "streaming", 20)
    time.sleep(3)
    if remote().get("state") != "streaming" or not console().get("live"):
        raise SystemExit("до обрыва нет потока с камеры (блок/пульт) — замер не имеет смысла")
    t0 = time.monotonic()
    ssh(CAM, "sudo systemctl kill -s KILL mediamtx")
    off = wait(lambda: not console().get("live"), 10)
    states = set()
    while time.monotonic() - t0 < 6:
        states.add(remote().get("state"))
        time.sleep(0.3)
    t1 = time.monotonic()
    ssh(CAM, "sudo systemctl start mediamtx")
    back = wait(lambda: console().get("live") and remote().get("state") == "streaming", 30)
    res["loss"] = {"outage_s": 6, "console_off_s": off, "remote_states": sorted(s for s in states if s),
                   "console_back_s_after_start": back, "camera_restart_overhead_s": round(time.monotonic() - t1, 2)}
    print(json.dumps(res["loss"], ensure_ascii=False), flush=True)
    res["pi2_top"] = ssh(P2, "top -bn1 | sed -n '8,12p'")
finally:
    ssh(P2, f"printf '%s' '{orig}' > {VI}.tmp && mv {VI}.tmp {VI} && "
            "kill -HUP $(systemctl show -p MainPID --value sheregesh-remote-net)")
json.dump(res, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
