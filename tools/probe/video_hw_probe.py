#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Аппаратная приёмка устройств видеозахвата (QA, docs/acceptance/video-ingest/HW-PLAN.md) — замена v4l2-ctl, которого
в образе нет (docs/architecture/S3.3-universal-video-input.md). Только стандартная библиотека Python и gst-launch-1.0
из образа; запускается НА плате от sheregesh-ui (группа video), копией в /tmp:

  scp tools/probe/video_hw_probe.py sheregesh-ui@PI:/tmp/ && ssh sheregesh-ui@PI python3 /tmp/video_hw_probe.py usb

  usb                       USB-устройства: VID:PID, скорость, драйверы интерфейсов, гипотеза класса по VID:PID
  nodes [--no-probe]        узлы video4linux: драйвер, USB-родитель; для узлов на USB (любой драйвер, не только
                            uvcvideo) — QUERYCAP, форматы, размеры, интервалы, входы, ТВ-стандарты. Узлы платформы
                            (bcm2835-codec/isp, rpi-hevc-dec, unicam) не открываются никогда.
  frames --device /dev/videoN [--caps CAPS] [--seconds 20] [--script "10:снять сигнал,25:вернуть сигнал"]
                            реальный поток: кадры уменьшаются до GRAY8 64×36 (gst-launch … ! fdsink), по каждому —
                            время прихода, средняя яркость, разброс, отличие от предыдущего. Итог: частота, интервалы
                            p50/p95/max, время до первого кадра, участки live / blank / frozen / no-frames.
                            --script печатает подсказки оператору в заданные секунды и отмечает их на шкале.

Захват `frames` занимает устройство: службу, которая его держит, на время замера останавливают (HW-PLAN).
Вывод — текст или --json. Это средство наблюдения, а не критерий: пороги — в HW-PLAN/MATRIX.
"""
import argparse
import errno
import fcntl
import json
import os
import re
import statistics
import struct
import subprocess
import sys
import time
from pathlib import Path

# ---------- V4L2 (linux/videodev2.h) ----------
CAP_VIDEO_CAPTURE, CAP_META_CAPTURE, CAP_STREAMING, CAP_READWRITE = 0x1, 0x00800000, 0x04000000, 0x01000000
CAP_DEVICE_CAPS = 0x80000000
BUF_TYPE_VIDEO_CAPTURE = 1
FRMSIZE_DISCRETE, FRMSIZE_CONTINUOUS, FRMSIZE_STEPWISE = 1, 2, 3
FRMIVAL_DISCRETE = 1
INPUT_TYPE = {1: "tuner", 2: "camera", 3: "touch"}

CAPABILITY = struct.Struct("<16s32s32sIII12x")           # v4l2_capability, 104
FMTDESC = struct.Struct("<III32sII12x")                   # v4l2_fmtdesc, 64
FRMSIZE = struct.Struct("<III6I8x")                       # v4l2_frmsizeenum, 44
FRMIVAL = struct.Struct("<IIIII6I8x")                     # v4l2_frmivalenum, 52
INPUT = struct.Struct("<I32sIIIQII16x")                   # v4l2_input, 80 (std — u64 на смещении 48)
STANDARD = struct.Struct("<I4xQ24sIII16x4x")              # v4l2_standard, 72


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("V") << 8) | nr


_R, _RW = 2, 3
VIDIOC_QUERYCAP = _ioc(_R, 0, CAPABILITY.size)
VIDIOC_ENUM_FMT = _ioc(_RW, 2, FMTDESC.size)
VIDIOC_G_STD = _ioc(_R, 23, 8)
VIDIOC_ENUMSTD = _ioc(_RW, 25, STANDARD.size)
VIDIOC_ENUMINPUT = _ioc(_RW, 26, INPUT.size)
VIDIOC_G_INPUT = _ioc(_R, 38, 4)
VIDIOC_ENUM_FRAMESIZES = _ioc(_RW, 74, FRMSIZE.size)
VIDIOC_ENUM_FRAMEINTERVALS = _ioc(_RW, 75, FRMIVAL.size)

# Гипотеза класса по VID:PID — открытые источники (таблицы id драйверов ядра, проекты PiKVM и т. п.), не результат
# испытания. Одинаковая коробка «EasyCAP» встречается на разных микросхемах — определяет только VID:PID на месте.
KNOWN_USB = {
    "534d:2109": ("MacroSilicon MS2109", "HDMI→USB 2.0, UVC", "uvcvideo"),
    "345f:2130": ("MacroSilicon MS2130", "HDMI→USB 3.0, UVC", "uvcvideo"),
    "534d:0021": ("MacroSilicon MS210x (AV TO USB2.0)", "CVBS/S-Video→USB 2.0, UVC", "uvcvideo"),
    "1b71:3002": ("Fushicai UTV007", "CVBS/S-Video «EasyCAP»", "usbtv"),
    "1f71:3301": ("Fushicai UTV007", "CVBS/S-Video «EasyCAP»", "usbtv"),
    "1f71:3306": ("Fushicai UTV007", "CVBS/S-Video «EasyCAP»", "usbtv"),
    "05e1:0408": ("Syntek STK1160", "CVBS/S-Video «EasyCAP»", "stk1160"),
    "1c88:0007": ("Somagic SMI2021 (загрузчик, без прошивки)", "CVBS/S-Video «EasyCAP»", "нет драйвера в mainline"),
    "1c88:003c": ("Somagic SMI2021", "CVBS/S-Video «EasyCAP»", "нет драйвера в mainline"),
    "1c88:003d": ("Somagic SMI2021", "CVBS/S-Video «EasyCAP»", "нет драйвера в mainline"),
    "1c88:003e": ("Somagic SMI2021", "CVBS/S-Video «EasyCAP»", "нет драйвера в mainline"),
    "1c88:003f": ("Somagic SMI2021", "CVBS/S-Video «EasyCAP»", "нет драйвера в mainline"),
    "eb1a:2861": ("Empia EM2860/EM2861", "CVBS/S-Video «EasyCAP»", "em28xx"),
}
PLATFORM_DRIVERS = ("bcm2835-codec", "bcm2835-isp", "rpi-hevc-dec", "unicam", "bcm2835-unicam", "rp1-cfe")


def fourcc_str(v: int) -> str:
    return struct.pack("<I", v).decode("ascii", errors="replace").rstrip("\0 ")


def _dec(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


# ---------- USB ----------
def usb_devices(sys_root="/sys"):
    """Устройства /sys/bus/usb/devices (без концентраторов-корней): VID:PID, имена, скорость, драйверы интерфейсов."""
    base = Path(sys_root) / "bus" / "usb" / "devices"
    out = []
    try:
        entries = sorted(base.iterdir(), key=lambda p: p.name)
    except OSError:
        return out
    for d in entries:
        if ":" in d.name or not (d / "idVendor").exists():
            continue
        vid, pid = _read(d / "idVendor"), _read(d / "idProduct")
        if _read(d / "bDeviceClass") == "09":           # концентратор
            continue
        ifaces = {}
        for i in sorted(d.glob(f"{d.name}:*")):
            try:
                drv = os.path.basename(os.readlink(i / "driver"))
            except OSError:
                drv = ""
            ifaces[i.name] = {"class": _read(i / "bInterfaceClass"), "driver": drv}
        key = f"{vid}:{pid}"
        chip, kind, expected = KNOWN_USB.get(key, ("", "", ""))
        out.append({"port": d.name, "usb_id": key, "manufacturer": _read(d / "manufacturer"),
                    "product": _read(d / "product"), "serial": _read(d / "serial"),
                    "speed_mbps": _read(d / "speed"), "interfaces": ifaces,
                    "hypothesis": {"chip": chip, "class": kind, "expected_driver": expected} if chip else None})
    return out


# ---------- video4linux ----------
def _enum(ioctl, fd, request, st, *head, limit=256):
    """index = 0, 1, … до EINVAL. Возвращает (записи, ошибка): ENOTTY — драйвер не поддерживает перечисление."""
    blank = st.unpack(bytes(st.size))[1 + len(head):]
    out = []
    for index in range(limit):
        try:
            out.append(st.unpack(ioctl(fd, request, st.pack(index, *head, *blank))))
        except OSError as e:
            if e.errno == errno.EINVAL:
                return out, None
            return out, errno.errorcode.get(e.errno, str(e.errno))
    return out, None


def _rate(num, den):
    return f"{den}/{num}" if num else "?"            # интервал num/den с → частота den/num к/с


def probe_fd(fd, ioctl=fcntl.ioctl):
    """Всё, что узел сообщает о себе; ни одной записи в устройство (только чтение и перечисление)."""
    info = {}
    drv, card, bus, _ver, caps, dcaps = CAPABILITY.unpack(ioctl(fd, VIDIOC_QUERYCAP, bytes(CAPABILITY.size)))
    caps = dcaps if caps & CAP_DEVICE_CAPS else caps
    info.update(driver=_dec(drv), card=_dec(card), bus=_dec(bus), caps=f"0x{caps:08x}",
                video_capture=bool(caps & CAP_VIDEO_CAPTURE), meta=bool(caps & CAP_META_CAPTURE),
                streaming=bool(caps & CAP_STREAMING), readwrite=bool(caps & CAP_READWRITE))
    if not caps & CAP_VIDEO_CAPTURE:
        return info
    fmts, err = _enum(ioctl, fd, VIDIOC_ENUM_FMT, FMTDESC, BUF_TYPE_VIDEO_CAPTURE)
    info["formats"], info["formats_error"] = [], err
    for _i, _t, flags, desc, pixfmt, *_ in fmts:
        f = {"fourcc": fourcc_str(pixfmt), "description": _dec(desc), "compressed": bool(flags & 1), "sizes": []}
        sizes, f["sizes_error"] = _enum(ioctl, fd, VIDIOC_ENUM_FRAMESIZES, FRMSIZE, pixfmt)
        for _i, _pf, kind, *u in sizes:
            if kind == FRMSIZE_DISCRETE:
                s = {"size": f"{u[0]}x{u[1]}"}
                ivals, s["rates_error"] = _enum(ioctl, fd, VIDIOC_ENUM_FRAMEINTERVALS, FRMIVAL, pixfmt, u[0], u[1])
                s["rates"] = []
                for _i, _pf, _w, _h, ikind, *iv in ivals:
                    if ikind == FRMIVAL_DISCRETE:
                        s["rates"].append(_rate(iv[0], iv[1]))
                    else:
                        s["rates"].append(f"{_rate(iv[2], iv[3])}…{_rate(iv[0], iv[1])} (range)")
                        break
                f["sizes"].append(s)
            else:
                f["sizes"].append({"size": f"{u[0]}..{u[1]} step {u[2]} x {u[3]}..{u[4]} step {u[5]}",
                                   "kind": "stepwise" if kind == FRMSIZE_STEPWISE else "continuous"})
                break
        info["formats"].append(f)
    inputs, info["inputs_error"] = _enum(ioctl, fd, VIDIOC_ENUMINPUT, INPUT, limit=16)
    info["inputs"] = [{"index": i, "name": _dec(n), "type": INPUT_TYPE.get(t, str(t)), "std": f"0x{std:x}",
                       "status": f"0x{st:x}"} for i, n, t, _a, _tu, std, st, _c in inputs]
    stds, info["standards_error"] = _enum(ioctl, fd, VIDIOC_ENUMSTD, STANDARD, limit=64)
    info["standards"] = [f"{_dec(n)} {_rate(fp_n, fp_d)}" for _i, _id, n, fp_n, fp_d, _l in stds]
    for name, req, fmt in (("current_input", VIDIOC_G_INPUT, "<i"), ("current_std", VIDIOC_G_STD, "<Q")):
        try:
            v = struct.unpack(fmt, ioctl(fd, req, bytes(struct.calcsize(fmt))))[0]
            info[name] = v if name == "current_input" else f"0x{v:x}"
        except OSError as e:
            info[name] = errno.errorcode.get(e.errno, str(e.errno))
    return info


def video_nodes(sys_root="/sys", dev_root="/dev", probe=True, ioctl=fcntl.ioctl, opener=os.open):
    base = Path(sys_root) / "class" / "video4linux"
    try:
        names = sorted((p.name for p in base.iterdir() if re.fullmatch(r"video\d+", p.name)), key=lambda n: int(n[5:]))
    except OSError:
        return []
    out = []
    for name in names:
        d = base / name
        try:
            driver = os.path.basename(os.readlink(d / "device" / "driver"))
        except OSError:
            driver = ""
        node = {"node": name, "driver": driver, "name": _read(d / "name"), "index": _read(d / "index"),
                "usb_id": "", "usb_port": ""}
        usb = (d / "device").resolve().parent if (d / "device").exists() else None
        if usb is not None and (usb / "idVendor").exists():
            node["usb_id"] = f"{_read(usb / 'idVendor')}:{_read(usb / 'idProduct')}"
            node["usb_port"] = usb.name
        out.append(node)
        if not node["usb_id"] or driver in PLATFORM_DRIVERS:
            node["skipped"] = "platform node, never opened"
            continue
        if not probe:
            continue
        try:
            fd = opener(str(Path(dev_root) / name), os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        except OSError as e:
            node["error"] = f"open: {e.strerror or e}"
            continue
        try:
            node.update(probe_fd(fd, ioctl))
        except OSError as e:
            node["error"] = f"ioctl: {e.strerror or e}"
        finally:
            os.close(fd)
    return out


# ---------- реальный поток ----------
W, H = 64, 36
FRAME = W * H


def frame_stats(buf: bytes, prev: bytes | None):
    n = len(buf)
    mean = sum(buf) / n
    std = (sum((b - mean) ** 2 for b in buf) / n) ** 0.5
    diff = sum(abs(a - b) for a, b in zip(buf, prev)) / n if prev is not None else None
    return round(mean, 1), round(std, 1), None if diff is None else round(diff, 2)


def classify(frames, t_end, blank_std=4.0, frozen_diff=0.05, gap_s=0.5, min_s=0.25):
    """Участки шкалы: live — кадры идут и меняются; blank — равномерное поле (разброс < blank_std); frozen —
    кадры идут, но не меняются (отличие < frozen_diff); no-frames — нет кадров дольше gap_s. frames: [(t, mean,
    std, diff)]. Границы — по времени кадров (с), шкала начинается с 0 (запуск конвейера). «frozen» — практически
    тождественные кадры (повтор буфера); неподвижная сцена тоже даст «frozen», поэтому источник показывает движение."""
    segs = []

    def put(state, t0, t1):
        if segs and segs[-1][0] == state:
            segs[-1][2] = t1
        else:
            segs.append([state, t0, t1])

    last = 0.0
    for t, _mean, std, diff in frames:
        start = last
        if t - last > gap_s:
            put("no-frames", last, t)
            start = t
        state = "blank" if std < blank_std else ("frozen" if diff is not None and diff < frozen_diff else "live")
        put(state, start, t)
        last = t
    if t_end - last > gap_s:
        put("no-frames", last, t_end)
    merged = []                                  # всплески blank/frozen короче min_s — не потеря сигнала, а сцена
    for state, a, b in segs:
        if state in ("blank", "frozen") and b - a < min_s:
            state = "live"
        if merged and merged[-1][0] == state:
            merged[-1][2] = b
        else:
            merged.append([state, a, b])
    return [{"state": s, "from_s": round(a, 2), "to_s": round(b, 2)} for s, a, b in merged]


def summarize(frames, t_end, marks, **thresholds):
    times = [f[0] for f in frames]
    iv = [b - a for a, b in zip(times, times[1:])]
    q = (lambda p: round(sorted(iv)[min(len(iv) - 1, int(p * len(iv)))] * 1000, 1)) if iv else (lambda p: None)
    span = times[-1] - times[0] if len(times) > 1 else 0
    return {"frames": len(frames), "first_frame_s": round(times[0], 3) if times else None,
            "fps_avg": round((len(times) - 1) / span, 2) if span else None,
            "interval_ms": {"p50": q(0.5), "p95": q(0.95), "max": round(max(iv) * 1000, 1) if iv else None},
            "luma_mean": round(statistics.fmean(f[1] for f in frames), 1) if frames else None,
            "segments": classify(frames, t_end, **thresholds), "marks": marks}


def gst_command(device, caps):
    cmd = ["gst-launch-1.0", "-q", "v4l2src", f"device={device}", "do-timestamp=true", "!"]
    if caps:
        cmd += [caps, "!"]
    return cmd + ["decodebin", "!", "videoconvert", "!", "videoscale", "!",
                  f"video/x-raw,format=GRAY8,width={W},height={H}", "!", "fdsink", "fd=1", "sync=false"]


def parse_script(text):
    out = []
    for part in filter(None, (p.strip() for p in (text or "").split(","))):
        sec, _, what = part.partition(":")
        out.append((float(sec), what.strip() or "действие"))
    return sorted(out)


def run_frames(cmd, seconds, script=(), say=lambda s: print(s, file=sys.stderr, flush=True), clock=time.monotonic):
    """Читает кадры GRAY8 64×36 из stdout процесса cmd в течение seconds. Возвращает (кадры, шкала_конца, отметки,
    код_выхода, stderr). Процесс завершается сам или по истечении времени (SIGTERM, затем SIGKILL)."""
    pending = list(script)
    marks, frames = [], []
    t0 = clock()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)
    buf, prev = b"", None
    try:
        while clock() - t0 < seconds:
            now = clock() - t0
            while pending and pending[0][0] <= now:
                sec, what = pending.pop(0)
                say(f">>> t={now:5.1f} с: {what}")
                marks.append({"t_s": round(now, 2), "planned_s": sec, "action": what, "wall": time.time()})
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                chunk = None
            if chunk == b"":
                break                                    # процесс закрыл вывод (устройство пропало, ошибка)
            if chunk:
                buf += chunk
                while len(buf) >= FRAME:
                    fr, buf = buf[:FRAME], buf[FRAME:]
                    frames.append((round(clock() - t0, 4), *frame_stats(fr, prev)))
                    prev = fr
            else:
                time.sleep(0.005)
    finally:
        t_end = clock() - t0
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        err = proc.stderr.read().decode("utf-8", errors="replace")[-2000:]
    return frames, t_end, marks, proc.returncode, err


# ---------- CLI ----------
def _print_usb(devs):
    for d in devs:
        ifs = ", ".join(f"{k.split(':')[-1]}:{v['class']}→{v['driver'] or '(нет драйвера)'}" for k, v in d["interfaces"].items())
        print(f"{d['port']:10} {d['usb_id']}  {d['speed_mbps']:>5} Мбит/с  {d['manufacturer']} {d['product']}  [{ifs}]")
        if d["hypothesis"]:
            h = d["hypothesis"]
            print(f"           гипотеза по VID:PID: {h['chip']} — {h['class']}; ожидаемый драйвер: {h['expected_driver']}")


def _print_nodes(nodes):
    for n in nodes:
        head = f"{n['node']:8} {n['driver'] or '-':16} {n['usb_id'] or '-':10} {n['name']}"
        if "skipped" in n:
            print(f"{head}  ({n['skipped']})")
            continue
        print(head + (f"  ОШИБКА {n['error']}" if "error" in n else ""))
        if "caps" in n:
            print(f"         caps {n['caps']} capture={n['video_capture']} meta={n['meta']} streaming={n['streaming']}"
                  f" read={n['readwrite']} card='{n['card']}' bus='{n['bus']}'")
        for f in n.get("formats", []):
            print(f"         {f['fourcc']:5} {f['description']}{' (ENUM_FRAMESIZES: ' + f['sizes_error'] + ')' if f['sizes_error'] else ''}")
            for s in f["sizes"]:
                rates = ", ".join(s.get("rates", [])) or s.get("rates_error") or s.get("kind", "")
                print(f"               {s['size']:>12} @ {rates}")
        if n.get("inputs") or n.get("inputs_error"):
            print("         входы: " + ("; ".join(f"{i['index']} {i['name']} ({i['type']}, std {i['std']}, status {i['status']})"
                                                for i in n["inputs"]) or n["inputs_error"]))
        if n.get("standards") or n.get("standards_error"):
            print(f"         стандарты: {', '.join(n['standards']) or n['standards_error']}; текущий {n.get('current_std')}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="video_hw_probe.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--sys-root", default="/sys", help=argparse.SUPPRESS)
    ap.add_argument("--dev-root", default="/dev", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("usb")
    pn = sub.add_parser("nodes")
    pn.add_argument("--no-probe", action="store_true", help="только sysfs, узлы не открывать")
    pf = sub.add_parser("frames")
    pf.add_argument("--device", required=True)
    pf.add_argument("--caps", default="", help="например image/jpeg,width=1280,height=720,framerate=30/1")
    pf.add_argument("--seconds", type=float, default=20.0)
    pf.add_argument("--script", default="", help="«сек:действие,…» — подсказки оператору")
    pf.add_argument("--blank-std", type=float, default=4.0, help="разброс яркости ниже — «blank» (равномерное поле)")
    pf.add_argument("--frozen-diff", type=float, default=0.05, help="отличие от предыдущего ниже — «frozen»")
    pf.add_argument("--command", nargs=argparse.REMAINDER, default=None,
                    help=argparse.SUPPRESS)   # испытания и сверка: подмена конвейера (последним аргументом)
    a = ap.parse_args(argv)
    if a.cmd == "usb":
        res = usb_devices(a.sys_root)
        print(json.dumps(res, ensure_ascii=False, indent=1)) if a.json else _print_usb(res)
        return 0
    if a.cmd == "nodes":
        res = video_nodes(a.sys_root, a.dev_root, probe=not a.no_probe)
        print(json.dumps(res, ensure_ascii=False, indent=1)) if a.json else _print_nodes(res)
        return 0
    cmd = a.command or gst_command(a.device, a.caps)
    frames, t_end, marks, rc, err = run_frames(cmd, a.seconds, parse_script(a.script))
    res = {"device": a.device, "caps": a.caps, "command": cmd, "seconds": round(t_end, 2), "exit_code": rc,
           "stderr_tail": err, **summarize(frames, t_end, marks, blank_std=a.blank_std, frozen_diff=a.frozen_diff)}
    if a.json:
        res["timeline"] = [{"t_s": t, "mean": m, "std": s, "diff": d} for t, m, s, d in frames]
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print(f"кадров {res['frames']}, первый через {res['first_frame_s']} с, средняя частота {res['fps_avg']} к/с, "
              f"интервал p50/p95/max {res['interval_ms']['p50']}/{res['interval_ms']['p95']}/{res['interval_ms']['max']} мс, "
              f"яркость {res['luma_mean']}, код выхода {rc}")
        for s in res["segments"]:
            print(f"  {s['from_s']:7.2f}–{s['to_s']:7.2f} с  {s['state']}")
        for m in marks:
            print(f"  отметка {m['t_s']:7.2f} с: {m['action']}")
        if rc not in (0, None, -15) and err:
            print("stderr: " + err.strip().splitlines()[-1])
    return 0 if frames else 1


if __name__ == "__main__":
    raise SystemExit(main())
