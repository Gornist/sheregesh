# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Стенд нагрузки интерфейса на плате без дисплея: отдельный экземпляр пульта под cage с виртуальным выходом
(WLR_BACKENDS=headless, рендер на GPU платы), локальное синтетическое видео, без удалённого блока.
Рабочий пульт и его сокеты не затрагиваются (свой XDG_RUNTIME_DIR и каталог данных во временном каталоге).

На pi1 (код — из установленного DEV-выпуска):
  python3 tools/probe/ui_bench.py --host 192.168.1.92 --layout minimal --seconds 40 [--navigate] [--size 1280x800]
  python3 tools/probe/ui_bench.py --host 192.168.1.92 --layout classic --page video --seconds 40

Итог (JSON): CPU процесса пульта (доля ядра, все потоки) и gst-генератора, RSS, кадры окна в секунду (frameSwapped),
показанные кадры видео; при --navigate — открытие разделов по кругу каждые 2 с, время открытия до первого кадра
и минимальные FPS окна/видео во время навигации (GUI-12).
"""
import argparse
import json
import os
import shlex
import subprocess
import sys

HARNESS = r'''
import json, os, resource, sys, tempfile, time
for sub in ("software", "software/console", "software/emulators", "software/remote"):
    sys.path.insert(0, os.path.join(CODE_ROOT, sub))
from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuickControls2 import QQuickStyle
from sheregesh_console.app import Ui, compile_translations
from sheregesh_console.backend import Backend
QQuickStyle.setStyle("Fusion")
app = QGuiApplication(["ui-bench"])
compile_translations()
import sheregesh_console.video as V
from PySide6.QtGui import QImage
FW, FH = __FRAME_SIZE__
V.FRAME_W, V.FRAME_H, V.FRAME_WAKE = FW, FH, __WAKE__
V.FRAME_FORMAT = QImage.Format_RGB32 if __FMT__ == "BGRx" else QImage.Format_RGBX8888
if RTSP_URL:      # кадры камеры напрямую (rtspsrc → аппаратный декодер), строка состояния — как от блока с RTSP
    caps = f"video/x-raw,format={__FMT__},width={FW},height={FH}"
    conv = (["v4l2convert", "!", caps] if __CONV__ == "hw" else
            ["videoscale", "!", f"video/x-raw,width={FW},height={FH}", "!", "videoconvert", "!", caps])
    cmd = ["gst-launch-1.0", "-q", "rtspsrc", "location=" + RTSP_URL, "latency=50", "protocols=tcp", "!",
           "rtph264depay", "!", "h264parse", "!", "v4l2h264dec", "!", *conv, "!", "fdsink", "fd=1", "sync=false"]
    b = Backend(tempfile.mkdtemp(prefix="ui-bench-"), autostart=True, video_command=cmd)
    rep = {"mode": "remote", "link": "CONNECTED", "session_info": {"codec": "h264", "width": 1280, "height": 720, "fps": 30},
           "telemetry_video": {"enabled": True, "active": True, "codec": "h264", "source": {
               "input": "rtsp", "state": "streaming", "device": "shcam Pi5 IMX477", "width": 1280, "height": 720, "fps": 30}}}
    b._video_report = lambda: rep
    b.video.configure("h264", 5600)
else:
    b = Backend(tempfile.mkdtemp(prefix="ui-bench-"), autostart=True, local_video=True)
ui = Ui(b, LAYOUT)
w = ui.window
w.setProperty("visibility", 5)                      # полноэкранно на виртуальном выходе
if LAYOUT == "classic":
    w.setProperty("currentPage", PAGE)
ui.count_frames()
LAT, SEEN = [], {"seq": -1}
def on_swap():
    rx = ui.backend.videoReceiver
    if state["t0"] and rx._shown_seq != SEEN["seq"] and rx._last_frame_t:
        SEEN["seq"] = rx._shown_seq
        LAT.append((time.monotonic() - rx._last_frame_t) * 1000.0)
w.frameSwapped.connect(on_swap)
SECTIONS = ["video", "control", "module", "diagnostics", "system", ""]
res = {"opens": [], "rendered": [], "shown": [], "sec": []}
state = {"i": 0, "t0": None, "c0": None}

def cpu():
    r = resource.getrusage(resource.RUSAGE_SELF); c = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime, c.ru_utime + c.ru_stime

def gst_cpu():
    tot = 0.0
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                if int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[1]) == os.getpid():
                    f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
                    tot += (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")
            except OSError:
                pass
    return tot

PROF = None
def start():
    global PROF
    if DO_PROFILE:
        import cProfile
        PROF = cProfile.Profile(); PROF.enable()
    state["t0"], state["c0"], state["g0"] = time.monotonic(), cpu(), gst_cpu()

def sample():
    p = ui.perf()
    res["rendered"].append(p["rendered_fps_5s"]); res["shown"].append(round(backend_fps(), 1))
    res["sec"].append(p.get("section") or "-")
    if p["last_open_ms"] and p["last_open_ms"] > 0 and NAVIGATE:
        res["opens"].append(p["last_open_ms"]); w.setProperty("lastOpenMs", -1)

def backend_fps():
    return ui.backend.videoReceiver.property("fps") or 0.0

def nav():
    sec = SECTIONS[state["i"] % len(SECTIONS)]; state["i"] += 1
    if sec:
        w.setProperty("openLoaded", False)
        w.setProperty("openStartedMs", time.time() * 1000.0)
    w.setProperty("section", sec)

def grab_all():
    for sec in SECTIONS:
        w.setProperty("section", sec)
        n0 = len(ui._swaps)                          # снимок — после двух кадров уже с новым разделом
        t = time.monotonic()
        while (len(ui._swaps) - n0 < 3 or time.monotonic() - t < 0.6) and time.monotonic() - t < 5:
            app.processEvents()
        ui.grab(os.path.join(GRAB_DIR, "pi1-%s.png" % (sec or "video")))

def finish():
    if GRAB_DIR:
        grab_all()
    dt = time.monotonic() - state["t0"]
    if PROF is not None:
        import io, pstats
        PROF.disable(); out = io.StringIO()
        pstats.Stats(PROF, stream=out).sort_stats("tottime").print_stats(22)
        print("PROFILE " + json.dumps(out.getvalue()), flush=True)
    (s1, _c1), g1 = cpu(), gst_cpu()
    rss = int(open("/proc/self/status").read().split("VmRSS:")[1].split()[0]) // 1024
    ren = [x for x in res["rendered"][1:] if x is not None]
    print("RESULT " + json.dumps({"layout": LAYOUT, "page": PAGE, "navigate": NAVIGATE, "seconds": round(dt, 1),
        "window": [w.width(), w.height()],
        "cpu_console_pct": round(100 * (s1 - state["c0"][0]) / dt, 1), "cpu_gst_pct": round(100 * (g1 - state["g0"]) / dt, 1),
        "rss_mb": rss, "rendered_fps_min": min(ren) if ren else None, "rendered_fps_max": max(ren) if ren else None,
        "shown_fps_min": min(res["shown"][1:]) if len(res["shown"]) > 1 else None,
        "open_ms": sorted(res["opens"]),
        "lat_ms": {"p50": round(sorted(LAT)[len(LAT) // 2], 1), "p95": round(sorted(LAT)[int(len(LAT) * 0.95)], 1),
                   "n": len(LAT)} if LAT else None,
        "variant": {"size": f"{FW}x{FH}", "fmt": __FMT__, "conv": __CONV__, "wake": __WAKE__}, "shown_series": res["shown"][1:], "section_series": res["sec"][1:],
        "video_state": ui.backend.videoReceiver.property("state")}), flush=True)
    app.quit()

QTimer.singleShot(WARMUP * 1000, start)
s = QTimer(); s.timeout.connect(lambda: state["t0"] and sample()); s.start(1000)
if NAVIGATE:
    n = QTimer(); n.timeout.connect(lambda: state["t0"] and nav()); n.start(2000)
QTimer.singleShot((WARMUP + SECONDS) * 1000, finish)
app.exec()
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="192.168.1.92")
    ap.add_argument("--user", default="sheregesh-ui")
    ap.add_argument("--layout", choices=("minimal", "classic"), default="minimal")
    ap.add_argument("--page", default="video", help="classic: страница (dashboard, video, …)")
    ap.add_argument("--seconds", type=int, default=40)
    ap.add_argument("--warmup", type=int, default=12)
    ap.add_argument("--navigate", action="store_true")
    ap.add_argument("--size", default="1280x800")
    ap.add_argument("--profile", action="store_true", help="cProfile потока интерфейса за окно замера (топ по tottime)")
    ap.add_argument("--out")
    ap.add_argument("--frame", default="640x360", help="размер кадра на пульте (выход gst)")
    ap.add_argument("--fmt", choices=("RGBx", "BGRx"), default="RGBx")
    ap.add_argument("--conv", choices=("sw", "hw"), default="sw", help="масштаб/конвертация: videoconvert или v4l2convert")
    ap.add_argument("--wake", action="store_true", help="показ кадра по приходу, а не по таймеру 33 мс")
    ap.add_argument("--rtsp", default="", help="видео — RTSP камеры напрямую на pi1 (rtsp://…), а не локальная синтетика")
    ap.add_argument("--grab", metavar="DIR", help="в конце снять окно с каждым разделом (PNG с GPU платы) в DIR")
    a = ap.parse_args()
    pages = ["dashboard", "video", "diagnostics", "power", "logs", "settings", "about", "module", "sticks"]
    code = (HARNESS.replace("LAYOUT", repr(a.layout)).replace("PAGE", str(pages.index(a.page)))
            .replace("NAVIGATE", str(a.navigate)).replace("DO_PROFILE", str(a.profile)).replace("GRAB_DIR", "os.getcwd()" if a.grab else "''").replace("RTSP_URL", repr(a.rtsp)).replace("__FRAME_SIZE__", repr(tuple(int(x) for x in a.frame.split("x"))))
            .replace("__FMT__", repr(a.fmt)).replace("__CONV__", repr(a.conv)).replace("__WAKE__", str(a.wake)).replace("WARMUP", str(a.warmup)).replace("SECONDS", str(a.seconds)))
    root = "$(python3 -c 'import json,subprocess;print(json.loads(subprocess.run([\"sheregesh-deployd\",\"status\"]," \
           "capture_output=True,text=True).stdout)[\"code_root\"])')"
    w, h = a.size.split("x")
    remote = (f"set -e; R={root}; D=$(mktemp -d); chmod 700 $D; cd $D; "
              f"printf %s {shlex.quote(code)} | sed \"s#CODE_ROOT#'$R'#\" > bench.py; "
              f"XDG_RUNTIME_DIR=$D WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 WLR_HEADLESS_OUTPUTS=1 "
              f"QT_QPA_PLATFORM=wayland SHEREGESH_HEADLESS_SIZE={w}x{h} "
              f"timeout {a.seconds + a.warmup + 60} cage -- sh -c 'wlr-randr >/dev/null 2>&1; python3 bench.py' 2>$D/err.log "
              f"| grep -E '^(RESULT|PROFILE)' || tail -20 $D/err.log; "
              + ("tar -C $D -cf - $(cd $D && ls pi1-*.png 2>/dev/null) | base64 -w0 | sed 's/^/GRABS /'; echo; " if a.grab else "")
              + "rm -rf $D")
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", f"{a.user}@{a.host}", remote], capture_output=True, text=True,
                       timeout=a.seconds + a.warmup + 120)
    line = next((x for x in r.stdout.splitlines() if x.startswith("RESULT ")), None)
    if line is None:
        print(r.stdout[-3000:], r.stderr[-2000:], file=sys.stderr)
        return 1
    d = json.loads(line[7:])
    prof = next((x for x in r.stdout.splitlines() if x.startswith("PROFILE ")), None)
    if prof:
        print(json.loads(prof[8:]))
    print(json.dumps(d, ensure_ascii=False))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    grabs = next((x for x in r.stdout.splitlines() if x.startswith("GRABS ")), None)
    if a.grab and grabs:
        import base64, io, tarfile
        os.makedirs(a.grab, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(grabs[6:]))) as t:
            for m in t.getmembers():
                if m.isfile() and m.name.startswith("pi1-") and m.name.endswith(".png") and "/" not in m.name:
                    with open(os.path.join(a.grab, m.name), "wb") as f:
                        f.write(t.extractfile(m).read())
                    print(os.path.join(a.grab, m.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
