# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Пробы этапа S2 на Raspberry Pi 4 (ADR-0002/0003/0006/0007). Регламент — docs/acceptance/s2/PROBES.md.

  python3 tools/probe/s2probe.py env  --out DIR             факты платформы (ОС, ядро, EEPROM, Qt, GStreamer, дисплей)
  python3 tools/probe/s2probe.py h264 --out DIR             S2-4а: кодирование и декодирование H.264 720p30 без GUI
  python3 tools/probe/s2probe.py gui  --out DIR [--seconds 300]
                                                            S2-4б: пульт 1920×1200 + видео 720p30: к/с и загрузка CPU
  python3 tools/probe/s2probe.py boot --out DIR --mark FILE S2-2: от загрузки ядра до первого кадра пульта ≤ 15 с
  python3 tools/probe/s2probe.py link --out DIR --peer IP   канал между платами: адрес стенда, скорость порта, ping

Каждая проба пишет DIR/<проба>.json: исходные данные, критерий, вердикт, коммит и платформу.
Пробы только читают состояние системы и запускают синтетические конвейеры; настроек ОС не меняют.
Вердикт пробы не меняет статусы реестра: статус Must меняется только по испытаниям L4 из TEST-SPEC.
"""
import argparse
import ipaddress
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in ("software", "software/emulators", "software/console", "software/remote"):
    sys.path.insert(0, str(ROOT / p))

LAB_NET = ipaddress.ip_network("192.168.77.0/24")      # ADR-0007: изолированный стенд, без шлюза
H264_FPS_MIN, CPU_MAX_PCT, BOOT_MAX_S = 29.0, 60.0, 15.0


def sh(*cmd, timeout=30):
    """Вывод команды или None, если её нет или она завершилась ошибкой."""
    if not shutil.which(cmd[0]):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def read(path):
    try:
        return Path(path).read_text(errors="replace").strip("\x00\n ")
    except OSError:
        return None


def git_rev():
    return sh("git", "-C", str(ROOT), "describe", "--always", "--dirty")


def cpu_times():
    """(занято, всего) в тиках по всем ядрам из /proc/stat; None вне Linux."""
    line = read("/proc/stat")
    if not line:
        return None
    v = [int(x) for x in line.splitlines()[0].split()[1:]]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    return sum(v) - idle, sum(v)


def cpu_percent(a, b):
    return None if not a or not b or b[1] == a[1] else round(100.0 * (b[0] - a[0]) / (b[1] - a[1]), 1)


def save(out, name, data):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    data = {"probe": name, "commit": git_rev(), "host": platform.node(), "machine": platform.machine(),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **data}
    (out / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
    print(json.dumps({k: v for k, v in data.items() if k not in ("raw", "samples")}, ensure_ascii=False, indent=1))
    return 0 if data.get("verdict") in ("PASS", "INFO") else 1


# ---------------- env ----------------
def probe_env(a):
    from sheregesh import video
    try:
        import PySide6
        pyside = PySide6.__version__
    except ImportError:
        pyside = None
    elements = {e: video.has_element(e) for e in ("v4l2h264enc", "v4l2h264dec", "qml6glsink", "openh264enc",
                                                  "avdec_h264", "jpegenc", "videotestsrc")}
    drm = {p.parent.name: read(p).splitlines()[:3] for p in Path("/sys/class/drm").glob("card*-*/modes") if read(p)}
    data = {
        "model": read("/proc/device-tree/model"), "os_release": read("/etc/os-release"),
        "kernel": platform.release(), "python": platform.python_version(), "pyside6": pyside,
        "eeprom": sh("rpi-eeprom-update"), "firmware": sh("vcgencmd", "version"),
        "throttled": sh("vcgencmd", "get_throttled"), "temp": sh("vcgencmd", "measure_temp"),
        "gstreamer": (sh("gst-launch-1.0", "--version") or "").splitlines()[:1],
        "gst_elements": elements, "h264_pair": video.describe("h264") if video.h264_elements() else None,
        "codec_choice": video.describe(video.choose_codec()),
        "video_devices": sorted(str(p) for p in Path("/dev").glob("video*")),
        "drm_modes": drm, "cage": shutil.which("cage"), "wlopm": shutil.which("wlopm"),
        "meminfo": (read("/proc/meminfo") or "").splitlines()[:3],
        "verdict": "INFO",
    }
    return save(a.out, "env", data)


# ---------------- h264 (S2-4а) ----------------
def probe_h264(a):
    """Кодирование и декодирование 720p30 в одном конвейере: пропускная способность и CPU без GUI."""
    from sheregesh import video
    pair = video.h264_elements()
    if not pair:
        return save(a.out, "h264", {"verdict": "FAIL", "reason": "нет работающей связки H.264", "codec_choice":
                                    video.describe(video.choose_codec())})
    enc, dec = pair
    src = ["videotestsrc", f"num-buffers={a.frames}", "is-live=true", "!",
           "video/x-raw,width=1280,height=720,framerate=30/1", "!", "videoconvert", "!", "video/x-raw,format=I420", "!"]
    cmd = ["gst-launch-1.0", "-q", *src, *enc, "!", "h264parse", "!", dec, "!", "fakesink", "sync=true"]
    c0, t0 = cpu_times(), time.monotonic()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=a.frames / 10 + 60)
    wall, c1 = time.monotonic() - t0, cpu_times()
    fps = round(a.frames / wall, 1) if r.returncode == 0 else 0.0
    cpu = cpu_percent(c0, c1)
    ok = r.returncode == 0 and fps >= H264_FPS_MIN and (cpu is None or cpu <= CPU_MAX_PCT)
    return save(a.out, "h264", {
        "pair": video.describe("h264"), "hardware": enc[0].startswith("v4l2") and dec.startswith("v4l2"),
        "frames": a.frames, "wall_s": round(wall, 2), "fps": fps, "cpu_total_pct": cpu,
        "criterion": f"≥ {H264_FPS_MIN} к/с, CPU ≤ {CPU_MAX_PCT} % (ADR-0003, S2-4)",
        "command": cmd, "stderr": r.stderr[-2000:], "verdict": "PASS" if ok else "FAIL"})


# ---------------- gui (S2-4б) ----------------
def probe_gui(a):
    """Пульт на реальном дисплее (под cage на Pi), отдельный процесс блока с видео 720p30 H.264, страница «Видео»."""
    import socket
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQuickControls2 import QQuickStyle

    from sheregesh import video
    from sheregesh_console.app import PAGE_NAMES, Ui, compile_translations
    from sheregesh_console.backend import Backend
    from sheregesh_console.qtwait import pump, wait_until
    import tempfile

    def free(kind):
        with socket.socket(socket.AF_INET, kind) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
    port, vport = free(socket.SOCK_STREAM), free(socket.SOCK_DGRAM)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(str(ROOT / p) for p in
                                                       ("software", "software/emulators", "software/remote"))}
    agent = subprocess.Popen([sys.executable, "-m", "sheregesh_remote", "--port", str(port), "--video",
                              "--video-port", str(vport), "--video-codec", "h264"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    agent.stdout.readline()
    QQuickStyle.setStyle("Fusion")
    app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
    compile_translations()
    ui = Ui(Backend(tempfile.mkdtemp(prefix="sheregesh-probe-"), autostart=True, peer=("127.0.0.1", port)))
    samples = []
    try:
        ui.window.setProperty("currentPage", PAGE_NAMES.index("video"))
        playing = wait_until(lambda: ui.backend.video.state == "playing", 20)
        pump(3000)                                                   # разогрев
        end = time.monotonic() + a.seconds
        while time.monotonic() < end:
            c0 = cpu_times()
            pump(1000)
            samples.append({"fps": ui.backend.video.fps, "cpu_total_pct": cpu_percent(c0, cpu_times()),
                            "state": ui.backend.video.state})
        size = (ui.window.width(), ui.window.height())
        platform_name = QGuiApplication.platformName()
    finally:
        ui.close()
        os.killpg(agent.pid, 15)
        agent.wait(5)
    fps = [s["fps"] for s in samples]
    cpu = [s["cpu_total_pct"] for s in samples if s["cpu_total_pct"] is not None]
    med_fps = statistics.median(fps) if fps else 0.0
    med_cpu = statistics.median(cpu) if cpu else None
    ok = playing and size == (1920, 1200) and med_fps >= H264_FPS_MIN and med_cpu is not None and med_cpu <= CPU_MAX_PCT
    return save(a.out, "gui", {
        "codec": video.describe(video.choose_codec()), "qpa": platform_name, "window": size, "playing": playing,
        "seconds": a.seconds, "fps_median": med_fps, "fps_min": min(fps, default=0.0),
        "cpu_total_median_pct": med_cpu, "cpu_total_max_pct": max(cpu, default=None),
        "criterion": f"окно 1920×1200, медиана ≥ {H264_FPS_MIN} к/с, CPU ≤ {CPU_MAX_PCT} % (ADR-0003, S2-4)",
        "note": "CPU не измерен вне Linux — вердикт FAIL" if med_cpu is None else "",
        "samples": samples, "verdict": "PASS" if ok else "FAIL"})


# ---------------- boot (S2-2) ----------------
def probe_boot(a):
    """Метку пишет пульт при SHEREGESH_FIRST_FRAME_MARK=FILE (CLOCK_BOOTTIME первого показанного кадра)."""
    mark = json.loads(read(a.mark) or "{}")
    t = mark.get("seconds") if mark.get("clock") == "boottime" else None
    ok = t is not None and t <= BOOT_MAX_S
    return save(a.out, "boot", {
        "first_frame_boottime_s": t, "mark": mark, "systemd_analyze": sh("systemd-analyze"),
        "blame": (sh("systemd-analyze", "blame") or "").splitlines()[:25],
        "critical_chain": sh("systemd-analyze", "critical-chain"),
        "criterion": f"первый кадр GUI ≤ {BOOT_MAX_S} с от старта ядра (P-012, Should; ADR-0002, S2-2)",
        "verdict": "PASS" if ok else "FAIL"})


# ---------------- link ----------------
def probe_link(a):
    """Только изолированный стенд ADR-0007: адрес пира в 192.168.77.0/24, без шлюза по умолчанию через этот порт."""
    peer = ipaddress.ip_address(a.peer)
    if peer not in LAB_NET:
        print(f"отказ: {peer} вне сети стенда {LAB_NET} (ADR-0007)", file=sys.stderr)
        return 2
    route = sh("ip", "route", "get", str(peer)) or ""
    dev = (re.search(r"\bdev (\S+)", route) or [None, None])[1]
    via_gateway = " via " in route
    ping = sh("ping", "-c", str(a.count), "-i", "0.2", "-q", str(peer), timeout=a.count + 30) or ""
    loss = re.search(r"([\d.]+)% packet loss", ping)
    rtt = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", ping)
    speed = read(f"/sys/class/net/{dev}/speed") if dev else None
    duplex = read(f"/sys/class/net/{dev}/duplex") if dev else None
    loss_pct = float(loss[1]) if loss else None
    ok = not via_gateway and loss_pct == 0.0 and speed == "1000" and duplex == "full"
    return save(a.out, "link", {
        "peer": str(peer), "route": route, "device": dev, "via_gateway": via_gateway, "speed_mbit": speed,
        "duplex": duplex, "ping_loss_pct": loss_pct, "rtt_ms": dict(zip(("min", "avg", "max", "mdev"),
                                                                      map(float, rtt.groups()))) if rtt else None,
        "criterion": "прямой путь без шлюза, 1000 Мбит/с full duplex, потерь 0 % (ADR-0007)",
        "raw": ping, "verdict": "PASS" if ok else "FAIL"})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="probe", required=True)
    for name in ("env", "h264", "gui", "boot", "link"):
        p = sub.add_parser(name)
        p.add_argument("--out", required=True)
        if name == "h264":
            p.add_argument("--frames", type=int, default=900)
        if name == "gui":
            p.add_argument("--seconds", type=float, default=300)
        if name == "boot":
            p.add_argument("--mark", required=True)
        if name == "link":
            p.add_argument("--peer", required=True)
            p.add_argument("--count", type=int, default=100)
    a = ap.parse_args(argv)
    return {"env": probe_env, "h264": probe_h264, "gui": probe_gui, "boot": probe_boot, "link": probe_link}[a.probe](a)


if __name__ == "__main__":
    sys.exit(main())
