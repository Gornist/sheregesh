#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S5: автоматическая диагностика одного USB-адаптера видеозахвата на удалённом блоке (pi2) — с Mac одной командой,
≤ 5 мин, без root (на образе нет v4l2-ctl; sudo не используется). docs/acceptance/s5-video/ACCEPT.md, шаг S5-10.

  python3 tools/probe/s5_capture_diag.py --plan                                  # шаги, без SSH
  python3 tools/probe/s5_capture_diag.py --remote 192.168.1.171 --plug [--name ИМЯ] [--kind auto|cvbs|hdmi]
                                         [--top 4] [--seconds 6] [--free]
  python3 tools/probe/s5_capture_diag.py --remote 192.168.1.171 --usb-id 534d:2109   # адаптер уже подключён
  python3 tools/probe/s5_capture_diag.py --fake [FIXTURE.json] --out DIR             # без плат (модульные тесты)

Шаги (всё — чтение, кроме пробных захватов; захват занимает только сам адаптер):
  D0  SSH, пользователь не root, статус блока: выбран ли адаптер службой (тогда узел занят — --free временно
      выбирает synthetic через video-input.json + SIGHUP главному процессу и возвращает исходный файл в finally)
  D1  lsusb + video_hw_probe.py usb «до»; --plug: подсказка ЧЕЛОВЕКУ подключить адаптер; lsusb + usb «после» →
      новые VID:PID, скорость по sysfs (12 — USB 1.1, 480 — USB 2.0, 5000+ — USB 3.x), драйверы интерфейсов
      из /sys/bus/usb/devices/*/driver (нет драйвера — дальше не идём: NO-DRIVER)
  D2  журнал ядра: dmesg (если читается без root), иначе journalctl -k; оба недоступны — отмечается; ошибки USB
      (error -71/-110, reset, disconnect, over-current, bandwidth)
  D3  узлы: ls /dev/video* /dev/v4l/by-id /dev/v4l/by-path; video_hw_probe.py --json nodes — форматы,
      FRAMESIZES/FRAMEINTERVALS, входы (флаги статуса), стандарты, QUERYSTD
  D4  загрузка CPU pi2 в покое (/proc/stat, 3 с) — база для замеров
  D5  режимы: CVBS — PAL и NTSC по очереди (как videoinput.std_modes: 720×576 / 720×480, v4l2src norm=…);
      HDMI/UVC — фактические режимы драйвера, верхние --top по размеру×частоте (каждый FourCC представлен)
      для каждого: (а) gst-launch-1.0 v4l2src … num-buffers=… ! CAPS ! fakesink — фактическая частота по меткам
      PTS буферов и CPU pi2 по /proc/stat до/после; (б) video_hw_probe.py frames — есть ли изображение: live
      (сигнал), blank (равномерное поле: нет сигнала / «синее поле»), frozen, no-frames
Итог — docs/acceptance/s5-video/hw/<имя>/diag.json и diag.txt (без секретов: учётных данных в выводе нет).
Вердикт диагностики (не приёмки): OK | NO-SIGNAL | NO-NODE | NO-DRIVER | NO-DEVICE | BUSY | ERROR.
"""
import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/probe"))

PROBE = "/tmp/video_hw_probe.py"
REMOTE_STATUS = "/run/sheregesh-remote/link-status.json"
VCONF = "~/.config/sheregesh/video-input.json"
REMOTE_UNIT = "sheregesh-remote-net"
HW_DIR = ROOT / "docs/acceptance/s5-video/hw"
ANALOG = {"PAL": (720, 576, 25.0), "NTSC": (720, 480, 29.97)}      # videoinput.STANDARDS
CAPS = {"YUYV": "video/x-raw,format=YUY2", "UYVY": "video/x-raw,format=UYVY", "NV12": "video/x-raw,format=NV12",
        "MJPG": "image/jpeg", "JPEG": "image/jpeg", "H264": "video/x-h264,stream-format=byte-stream"}
RAW_FOURCC = ("YUYV", "UYVY", "NV12")
USB_ERR = re.compile(r"error -(71|110|32)|usb.*(fail|reset|disconnect|over-current|bandwidth)|not enough bandwidth",
                     re.I)
BUDGET_S = 300


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- чистые функции (тесты: test_s5_capture_diag.py)


def speed_label(mbps):
    try:
        v = float(mbps)
    except (TypeError, ValueError):
        return "?"
    return ("USB 1.x" if v <= 12 else "USB 2.0" if v <= 480 else "USB 3.x") + f" ({mbps} Мбит/с)"


def lsusb_ids(text):
    return sorted({m.group(1).lower() for m in re.finditer(r"\bID ([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\b", text or "")})


def new_devices(before, after):
    """Устройства «после», которых не было «до» (по порту и VID:PID)."""
    seen = {(d.get("port"), d.get("usb_id")) for d in before or []}
    return [d for d in after or [] if (d.get("port"), d.get("usb_id")) not in seen]


def pick_target(after, new, usb_id=None, nodes=()):
    """Испытуемое устройство: --usb-id, иначе единственное новое, иначе единственное USB-устройство с узлом захвата."""
    if usb_id:
        cand = [d for d in after if d.get("usb_id", "").lower() == usb_id.lower()]
    elif new:
        cand = list(new)
    else:
        ids = {n.get("usb_id") for n in nodes if n.get("usb_id") and n.get("video_capture")}
        cand = [d for d in after if d.get("usb_id") in ids]
    return cand[0] if len(cand) == 1 else None, cand


def unbound(dev):
    """Интерфейсы без драйвера (класс 0e — видео, ff — vendor): «несовместимо — нет драйвера»."""
    return [k for k, v in (dev.get("interfaces") or {}).items() if not v.get("driver")]


def kernel_log(text):
    """Вывод D2: строка «@SRC dmesg|journalctl|none», затем строки журнала."""
    lines = (text or "").splitlines()
    src = lines[0].split(" ", 1)[1].strip() if lines and lines[0].startswith("@SRC") else "none"
    body = lines[1:] if lines and lines[0].startswith("@SRC") else lines
    return {"source": src, "available": src != "none", "tail": body[-60:],
            "usb_errors": [ln for ln in body if USB_ERR.search(ln)]}


def v4l_links(lines):
    """Разделы «/dev/v4l/by-id:» и «/dev/v4l/by-path:» вывода D3 → имена ссылок (нет каталога — пусто)."""
    out, sec = {"by-id": [], "by-path": []}, None
    for ln in lines:
        m = re.fullmatch(r"/dev/v4l/(by-id|by-path):", ln.strip())
        if m:
            sec = m.group(1)
        elif sec and ln.strip() and "No such file" not in ln and not ln.startswith("ls:"):
            out[sec].append(ln.strip())
    return out


def capture_nodes(nodes, usb_id):
    return [n for n in nodes if n.get("usb_id") == usb_id and n.get("video_capture")]


def is_analog(node, kind="auto"):
    if kind != "auto":
        return kind == "cvbs"
    return bool(node.get("standards")) or any(i.get("type") == "tuner" for i in node.get("inputs") or [])


def _fps(rate):
    num, _, den = str(rate).partition("/")
    try:
        return round(float(num) / float(den or 1), 3)
    except (ValueError, ZeroDivisionError):
        return 0.0


def plan_modes(node, kind="auto", top=4):
    """Режимы пробного захвата. CVBS: PAL и NTSC (raw-формат узла, размер стандарта). HDMI/UVC: дискретные режимы
    драйвера, верхние top по размеру×частоте, но каждый FourCC хотя бы одним (лучшим) режимом."""
    fmts = [f for f in node.get("formats") or [] if f.get("fourcc") in CAPS]
    if is_analog(node, kind):
        raw = next((f["fourcc"] for f in fmts if f["fourcc"] in RAW_FOURCC), None) or \
            next((f["fourcc"] for f in fmts), "YUYV")
        out = []
        for std, (w, h, fps) in ANALOG.items():
            out.append({"key": f"{std.lower()}-{raw.lower()}-{w}x{h}", "std": std, "fourcc": raw, "size": f"{w}x{h}",
                        "rate": "", "nominal_fps": fps, "caps": f"{CAPS[raw]},width={w},height={h}", "norm": std})
        return out
    cands = []
    for f in fmts:
        for s in f.get("sizes") or []:
            if not re.fullmatch(r"\d+x\d+", s.get("size", "")):
                continue
            w, h = map(int, s["size"].split("x"))
            for r in s.get("rates") or []:
                if "range" in r or "/" not in r:
                    continue
                fps = _fps(r)
                cands.append((w * h * fps, w * h, fps, f["fourcc"], w, h, r))
    cands.sort(key=lambda c: (-c[0], -c[1], c[3]))
    chosen, fourccs = [], {c[3] for c in cands}
    for fc in sorted(fourccs):                                  # лучший режим каждого FourCC
        chosen.append(next(c for c in cands if c[3] == fc))
    for c in cands:
        if len(chosen) >= max(top, len(fourccs)):
            break
        if c not in chosen:
            chosen.append(c)
    chosen.sort(key=lambda c: (-c[0], -c[1], c[3]))
    return [{"key": f"{fc.lower()}-{w}x{h}@{fps:g}", "std": "", "fourcc": fc, "size": f"{w}x{h}", "rate": r,
             "nominal_fps": fps, "caps": f"{CAPS[fc]},width={w},height={h},framerate={r}", "norm": ""}
            for _p, _a, fps, fc, w, h, r in chosen[:max(top, len(fourccs))]]


def cpu_delta(a, b, ncpu=4):
    """Строки «cpu …» /proc/stat до/после → загрузка всей платы, % (100 % = все ядра) и в ядрах."""
    try:
        x = [int(v) for v in a.split()[1:]]
        y = [int(v) for v in b.split()[1:]]
    except (AttributeError, ValueError):
        return None
    d = [q - p for p, q in zip(x, y)]
    total = sum(d[:8]) if len(d) >= 8 else sum(d)
    if total <= 0:
        return None
    idle = d[3] + (d[4] if len(d) > 4 else 0)
    pct = round((total - idle) * 100.0 / total, 1)
    return {"busy_pct": pct, "cores": round(pct / 100.0 * ncpu, 2), "ncpu": ncpu}


PTS = re.compile(r"(\d+):(\d\d):(\d\d)\.(\d+)")


def parse_marked(text):
    """Вывод удалённой команды с метками «@X значение» → {X: [значения]}."""
    out = {}
    for ln in (text or "").splitlines():
        if ln.startswith("@") and " " in ln:
            k, v = ln[1:].split(" ", 1)
            out.setdefault(k, []).append(v)
        elif ln.startswith("@"):
            out.setdefault(ln[1:], []).append("")
    return out


def fps_from_pts(pts):
    secs = []
    for p in pts:
        m = PTS.search(p)
        if m:
            h, mi, s, frac = m.groups()
            secs.append(int(h) * 3600 + int(mi) * 60 + int(s) + float("0." + frac))
    secs.sort()
    if len(secs) < 2 or secs[-1] <= secs[0]:
        return None, len(secs)
    # без первых буферов: время до установления потока не частота источника
    skip = min(len(secs) // 10, 5)
    s = secs[skip:]
    return (round((len(s) - 1) / (s[-1] - s[0]), 2) if len(s) > 1 and s[-1] > s[0] else None), len(secs)


def parse_raw(text):
    m = parse_marked(text)
    ncpu = int((m.get("N") or ["4"])[0] or 4)
    fps, n = fps_from_pts(m.get("P", []))
    rc = (m.get("RC") or ["?"])[0]
    try:
        wall = round(float(m["T1"][0]) - float(m["T0"][0]), 2)
    except (KeyError, ValueError, IndexError):
        wall = None
    return {"exit_code": int(rc) if rc.lstrip("-").isdigit() else rc, "timeout": rc == "124", "buffers": n,
            "fps_actual": fps, "wall_s": wall, "cpu": cpu_delta((m.get("A") or [""])[0], (m.get("B") or [""])[0], ncpu),
            "errors": m.get("E", [])[-5:]}


def signal_of(frames):
    """Итог video_hw_probe frames: доля времени по состояниям и метка: signal | blank | frozen | no-frames."""
    if not frames or not frames.get("frames"):
        return {"label": "no-frames", "shares": {}, "luma_mean": None}
    dur = {}
    for s in frames.get("segments") or []:
        dur[s["state"]] = dur.get(s["state"], 0.0) + max(0.0, s["to_s"] - s["from_s"])
    total = sum(dur.values()) or 1.0
    shares = {k: round(v / total, 2) for k, v in dur.items()}
    best = max(shares, key=shares.get) if shares else "no-frames"
    label = {"live": "signal"}.get(best, best)
    return {"label": label, "shares": shares, "luma_mean": frames.get("luma_mean"),
            "fps_decoded": frames.get("fps_avg"), "first_frame_s": frames.get("first_frame_s")}


def mode_note(m):
    raw, sig = m.get("raw") or {}, m.get("signal") or {}
    notes = []
    if raw.get("timeout") and not raw.get("buffers"):
        notes.append("буферов нет за окно захвата (нет сигнала или режим не поддержан)")
    fa, nom = raw.get("fps_actual"), m.get("nominal_fps")
    if fa and nom and fa < 0.8 * nom:
        notes.append(f"фактическая частота {fa} < 80 % заявленной {nom}")
    if sig.get("label") == "blank":
        notes.append("равномерное поле: нет сигнала либо адаптер шлёт «синее/чёрное поле»")
    if raw.get("errors"):
        notes.append("gst: " + raw["errors"][-1][:160])
    return notes


def overall(rep):
    if rep.get("error"):
        return "ERROR"
    if rep.get("busy"):
        return "BUSY"
    if not rep.get("target"):
        return "NO-DEVICE"
    if rep.get("unbound"):
        return "NO-DRIVER"
    if not rep.get("node"):
        return "NO-NODE"
    return "OK" if any((m.get("signal") or {}).get("label") == "signal" for m in rep.get("modes", [])) else "NO-SIGNAL"


def default_name(target, node):
    if not target:
        return "unknown"
    drv = (node or {}).get("driver") or next((v.get("driver") for v in (target.get("interfaces") or {}).values()
                                               if v.get("driver")), "") or "nodriver"
    return f"{target['usb_id'].replace(':', '-')}-{drv}"


def render_text(rep):
    t = rep.get("target") or {}
    lines = [f"S5 диагностика адаптера: {rep.get('name')} — {rep.get('verdict')} ({rep.get('duration_s')} с, "
             f"{rep.get('time')})",
             f"устройство: {t.get('usb_id', '-')} {t.get('manufacturer', '')} {t.get('product', '')}".rstrip(),
             f"порт {t.get('port', '-')}, {speed_label(t.get('speed_mbps'))}; новые VID:PID: "
             f"{', '.join(rep.get('new_usb_ids') or []) or '—'}"]
    if t:
        ifs = ", ".join(f"{k.split(':')[-1]}:{v.get('class')}→{v.get('driver') or 'нет драйвера'}"
                        for k, v in (t.get("interfaces") or {}).items())
        lines.append(f"интерфейсы: {ifs or '—'}")
        if t.get("hypothesis"):
            h = t["hypothesis"]
            lines.append(f"гипотеза по VID:PID (не результат): {h.get('chip')} — {h.get('class')}")
    k = rep.get("kernel_log") or {}
    lines.append(f"журнал ядра: {k.get('source', 'none')}" + (f", ошибок USB {len(k.get('usb_errors') or [])}"
                                                            if k.get("available") else " — недоступен без root"))
    n = rep.get("node") or {}
    if n:
        lines.append(f"узел {n.get('node')} драйвер {n.get('driver')} карта '{n.get('card', '')}' by-id: "
                     f"{', '.join(rep.get('by_id') or []) or '—'}")
        for f in n.get("formats") or []:
            sizes = "; ".join(f"{s['size']}@{','.join(s.get('rates') or []) or s.get('kind', '?')}"
                              for s in f.get("sizes") or [])
            lines.append(f"  {f['fourcc']}: {sizes}")
        if n.get("inputs"):
            lines.append("  входы: " + "; ".join(f"{i['index']} {i['name']} ({i['type']}, статус "
                                                 f"{','.join(i.get('status_flags') or []) or 'ок'})" for i in n["inputs"]))
        if n.get("standards"):
            lines.append(f"  стандарты: {', '.join(n['standards'])}; текущий {n.get('current_std')}; QUERYSTD "
                         f"{n.get('query_std', '-')} {n.get('query_std_family') or ''}".rstrip())
    base = rep.get("cpu_idle") or {}
    lines.append(f"CPU pi2 в покое: {base.get('busy_pct', '?')} % ({base.get('cores', '?')} ядра)")
    for m in rep.get("modes") or []:
        raw, sig = m.get("raw") or {}, m.get("signal") or {}
        cpu = raw.get("cpu") or {}
        lines.append(f"режим {m['key']}{' norm=' + m['norm'] if m.get('norm') else ''}: частота {raw.get('fps_actual')}"
                     f" (заявлена {m.get('nominal_fps')}), буферов {raw.get('buffers')}, CPU {cpu.get('busy_pct', '?')} %"
                     f" ({cpu.get('cores', '?')} яд.); изображение: {sig.get('label')} {sig.get('shares', {})}, "
                     f"яркость {sig.get('luma_mean')}")
        lines += [f"    · {x}" for x in m.get("notes") or []]
    lines += [f"примечание: {x}" for x in rep.get("notes") or []]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- исполнители: плата и подделка


class Remote:
    """SSH к pi2 от sheregesh-ui (как e2e_run.ssh); stage() — только для подделки."""

    def __init__(self, host):
        self.host = host

    def sh(self, tag, cmd, timeout=60):
        try:
            r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", f"sheregesh-ui@{self.host}",
                                cmd], capture_output=True, text=True, timeout=timeout)
            return r.returncode, r.stdout.strip()
        except subprocess.TimeoutExpired:
            return 124, ""

    def put(self, local, remote):
        return subprocess.run(["scp", "-q", str(local), f"sheregesh-ui@{self.host}:{remote}"]).returncode == 0

    def stage(self, name):
        pass


class Fake:
    """Ответы по тегам шагов: fixture[tag@stage] или fixture[tag] → строка (rc 0) или [rc, строка]."""

    def __init__(self, fixture):
        self.fx, self.st, self.calls = fixture, "before", []

    def sh(self, tag, cmd, timeout=60):
        self.calls.append((tag, cmd))
        v = self.fx.get(f"{tag}@{self.st}", self.fx.get(tag, self.fx.get(tag.split(":")[0], [127, ""])))
        return (v[0], v[1]) if isinstance(v, list) else (0, v)

    def put(self, local, remote):
        self.calls.append(("put", remote))
        return True

    def stage(self, name):
        self.st = name


def _usbtv_fixture():
    """Встроенная подделка: аналоговый захват usbtv (CVBS) подключается по подсказке; PAL есть, NTSC — поле."""
    before = [{"port": "1-1", "usb_id": "2109:3431", "manufacturer": "", "product": "USB2.0 Hub", "serial": "",
               "speed_mbps": "480", "interfaces": {"1-1:1.0": {"class": "09", "driver": "hub"}}, "hypothesis": None}]
    dev = {"port": "1-1.3", "usb_id": "1b71:3002", "manufacturer": "fushicai", "product": "usbtv007", "serial": "",
           "speed_mbps": "480", "interfaces": {"1-1.3:1.0": {"class": "ff", "driver": "usbtv"}},
           "hypothesis": {"chip": "Fushicai UTV007", "class": "CVBS/S-Video «EasyCAP»", "expected_driver": "usbtv"}}
    node = {"node": "video0", "driver": "usbtv", "name": "usbtv", "index": "0", "usb_id": "1b71:3002",
            "usb_port": "1-1.3", "card": "usbtv", "video_capture": True,
            "formats": [{"fourcc": "YUYV", "description": "16 bpp YUY2, 4:2:2, packed", "compressed": False,
                         "sizes": [{"size": "720x576", "rates": ["25/1"]}, {"size": "720x480", "rates": ["30000/1001"]}]}],
            "inputs": [{"index": 0, "name": "Composite", "type": "camera", "std": "0xb0ff", "status": "0x0",
                        "status_flags": []}],
            "standards": ["PAL 25/1", "NTSC 30000/1001"], "current_std": "0xff", "current_input": 0,
            "query_std": "0xff", "query_std_family": "PAL"}

    def raw(fps, n, rc=0):
        pts = "\n".join(f"@P pts: 0:00:{i / fps:09.6f}" for i in range(n))
        return (f"@A cpu 1000 0 500 8000 100 0 10 0 0 0\n@T0 100.0\n@RC {rc}\n@T1 106.2\n"
                f"@B cpu 1150 0 560 8800 110 0 14 0 0 0\n@N 4\n{pts}")

    def frames(state, mean):
        j = json.dumps({"frames": 150, "fps_avg": 25.0, "first_frame_s": 0.4, "luma_mean": mean, "exit_code": -15,
                        "segments": [{"state": state, "from_s": 0.0, "to_s": 6.0}]})
        return f"@A cpu 1000 0 500 8000 100 0 10 0 0 0\n@J {j}\n@B cpu 1400 0 600 8800 100 0 20 0 0 0\n@N 4"
    return {
        "whoami": "sheregesh-ui\nsheregesh-ui adm video",
        "status": json.dumps({"video": {"source": {"type": "synthetic", "state": "streaming"}}}),
        "lsusb@before": "Bus 001 Device 002: ID 2109:3431 VIA Labs, Inc. Hub",
        "lsusb@after": "Bus 001 Device 002: ID 2109:3431 VIA Labs, Inc. Hub\nBus 001 Device 003: ID 1b71:3002 fushicai usbtv007",
        "usb@before": json.dumps(before), "usb@after": json.dumps(before + [dev]),
        "klog": "@SRC dmesg\n[Sun Oct 25 12:00:01 2026] usb 1-1.3: new high-speed USB device number 3 using xhci_hcd\n"
                "[Sun Oct 25 12:00:01 2026] usbtv 1-1.3:1.0: Fushicai USBTV007 Audio-Video Grabber",
        "devls": "crw-rw---- 1 root video 81, 0 Oct 25 12:00 /dev/video0\n/dev/v4l/by-id:\nusb-fushicai_usbtv007-video-index0",
        "nodes": json.dumps([node]),
        "cpu-idle": "@A cpu 1000 0 500 8000 100 0 10 0 0 0\n@B cpu 1003 0 502 8300 100 0 10 0 0 0\n@N 4",
        "raw:pal-yuyv-720x576": raw(25.0, 150), "frames:pal-yuyv-720x576": frames("live", 92.0),
        "raw:ntsc-yuyv-720x480": raw(29.97, 150), "frames:ntsc-yuyv-720x480": frames("blank", 16.0),
    }


# ---------------------------------------------------------------- шаги


def _ask(text):
    import e2e_run
    return e2e_run.fresh_input(text)


def _jlist(text):
    try:
        v = json.loads(text or "[]")
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def _jdict(text):
    try:
        v = json.loads(text or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


CPU_CMD = "echo \"@A $(head -1 /proc/stat)\"; {body} echo \"@B $(head -1 /proc/stat)\"; echo \"@N $(nproc)\""


def raw_cmd(dev, m, seconds):
    n = max(10, int(round((m.get("nominal_fps") or 25) * seconds)))
    norm = f" norm={m['norm']}" if m.get("norm") else ""
    gst = (f"timeout -s INT {int(seconds + 6)} gst-launch-1.0 -v v4l2src device={dev}{norm} do-timestamp=true "
           f"num-buffers={n} ! {shlex.quote(m['caps'])} ! fakesink sync=false silent=false >/tmp/s5-raw.log 2>&1;")
    body = (f"echo \"@T0 $(date +%s.%N)\"; {gst} echo \"@RC $?\"; echo \"@T1 $(date +%s.%N)\";")
    tail = ("; grep -o 'pts: [0-9:.]*' /tmp/s5-raw.log | sed 's/^/@P /'; "
            "grep -E 'ERROR|WARN|erroneous|not-negotiated' /tmp/s5-raw.log | tail -5 | sed 's/^/@E /'; rm -f /tmp/s5-raw.log")
    return CPU_CMD.format(body=body) + tail


def frames_cmd(dev, m, seconds):
    norm = f" --norm {m['norm']}" if m.get("norm") else ""
    return (f"timeout {int(seconds + 8)} python3 {PROBE} --json frames --device {dev} --caps {shlex.quote(m['caps'])}"
            f"{norm} --seconds {seconds:g} | python3 -c 'import json,sys; d=json.load(sys.stdin); d.pop(\"timeline\",None); "
            f"print(json.dumps(d))'")


KLOG_CMD = ("if dmesg -T >/dev/null 2>&1; then echo '@SRC dmesg'; dmesg -T | tail -n 150; "
            "elif journalctl -k -b -q --no-pager -n 1 >/dev/null 2>&1; then echo '@SRC journalctl'; "
            "journalctl -k -b -q --no-pager -o short-iso | tail -n 150; else echo '@SRC none'; fi")


class Diag:
    def __init__(self, a, r, prompt=None):
        self.a, self.r = a, r
        self.prompt = prompt or _ask
        self.rep = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "remote": a.remote, "notes": [], "modes": []}
        self.orig_conf = None
        self.t0 = time.monotonic()

    def step(self, name):
        log(f"{name} (+{time.monotonic() - self.t0:.0f} с)")

    def run(self):
        rep, r, a = self.rep, self.r, self.a
        self.step("D0 SSH, пользователь, статус блока")
        code, who = r.sh("whoami", "id -un; id -nG")
        if code != 0:
            rep["error"] = f"SSH к {a.remote}: код {code}"
            return self.finish()
        rep["user"] = who.splitlines()[0] if who else ""
        if rep["user"] == "root":
            rep["error"] = "диагностика выполняется без root (sheregesh-ui)"
            return self.finish()
        src = (_jdict(r.sh("status", f"cat {REMOTE_STATUS}")[1]).get("video") or {}).get("source") or {}
        rep["service_source"] = {k: src.get(k) for k in ("type", "input", "id", "device", "state")}
        if not r.put(ROOT / "tools/probe/video_hw_probe.py", PROBE):
            rep["error"] = "scp video_hw_probe.py не удался"
            return self.finish()
        if str(src.get("type", "")).lower() in ("v4l2", "uvc"):
            if not a.free:
                rep["busy"] = True
                rep["notes"].append("служба блока использует захват V4L2 — узел занят; повторить с --free "
                                    "(временно synthetic, исходный video-input.json возвращается)")
                return self.finish()
            self.free()
        try:
            self.inventory()
            if rep.get("target") and not rep.get("unbound") and rep.get("node"):
                self.trials()
        finally:
            self.restore()
        return self.finish()

    def free(self):
        self.step("D0 --free: источник блока → synthetic на время замеров")
        self.orig_conf = self.r.sh("vconf", f"cat {VCONF} 2>/dev/null || echo __ABSENT__")[1]
        cur = _jdict(self.orig_conf) if self.orig_conf.strip() != "__ABSENT__" else {}
        body = json.dumps({**{k: v for k, v in cur.items() if k == "rtsp_urls"}, "source": "synthetic"})
        self.r.sh("vconf-set", f"umask 077; printf '%s' {shlex.quote(body)} > {VCONF}.tmp && mv {VCONF}.tmp {VCONF} && "
                               f"kill -HUP $(systemctl show -p MainPID --value {REMOTE_UNIT})")
        self.rep["notes"].append("источник блока временно переключён на synthetic (--free)")
        time.sleep(0 if isinstance(self.r, Fake) else 4)

    def restore(self):
        if self.orig_conf is None:
            return
        hup = f"kill -HUP $(systemctl show -p MainPID --value {REMOTE_UNIT})"
        if self.orig_conf.strip() == "__ABSENT__":
            self.r.sh("vconf-restore", f"rm -f {VCONF}; true")
            self.rep["notes"].append("video-input.json не было: удалён; источник блока остаётся synthetic до перезапуска")
        else:
            self.r.sh("vconf-restore", f"umask 077; printf '%s' {shlex.quote(self.orig_conf)} > {VCONF}.tmp && "
                                       f"mv {VCONF}.tmp {VCONF} && {hup}")
            self.rep["notes"].append("исходный video-input.json возвращён, SIGHUP службе блока")

    def inventory(self):
        rep, r, a = self.rep, self.r, self.a
        self.step("D1 lsusb/usb «до»")
        before_txt = r.sh("lsusb", "lsusb 2>&1")[1]
        before = _jlist(r.sh("usb", f"python3 {PROBE} --json usb")[1])
        if a.plug:
            self.prompt("ПОДКЛЮЧИТЕ адаптер к USB pi2 (источник сигнала — тоже) и нажмите Enter… ")
            r.stage("after")
            time.sleep(0 if isinstance(r, Fake) else a.settle)
            self.step("D1 lsusb/usb «после»")
            after_txt = r.sh("lsusb", "lsusb 2>&1")[1]
            after = _jlist(r.sh("usb", f"python3 {PROBE} --json usb")[1])
        else:
            after_txt, after = before_txt, before
        rep["lsusb_before"], rep["lsusb_after"] = before_txt.splitlines(), after_txt.splitlines()
        rep["new_usb_ids"] = sorted(set(lsusb_ids(after_txt)) - set(lsusb_ids(before_txt))) if a.plug else []
        new = new_devices(before, after) if a.plug else []
        self.step("D2 журнал ядра")
        rep["kernel_log"] = kernel_log(r.sh("klog", KLOG_CMD, 60)[1])
        if not rep["kernel_log"]["available"]:
            rep["notes"].append("журнал ядра недоступен без root: ни dmesg, ни journalctl -k")
        self.step("D3 узлы video4linux")
        rep["dev_listing"] = r.sh("devls", "ls -l /dev/video* 2>&1; for d in by-id by-path; do echo /dev/v4l/$d:; "
                                           "ls /dev/v4l/$d 2>&1; done")[1].splitlines()
        nodes = _jlist(r.sh("nodes", f"python3 {PROBE} --json nodes", 60)[1])
        rep["nodes_usb"] = [n for n in nodes if n.get("usb_id")]
        target, cand = pick_target(after, new, a.usb_id, nodes)
        rep["target"], rep["candidates"] = target, [c.get("usb_id") for c in cand]
        if target is None:
            rep["notes"].append("испытуемое устройство не определено: " +
                                (f"несколько кандидатов {rep['candidates']} — укажите --usb-id" if cand else
                                 "нового USB-устройства нет (--plug) / --usb-id не найден"))
            return
        rep["speed"] = speed_label(target.get("speed_mbps"))
        rep["unbound"] = unbound(target)
        if rep["unbound"]:
            rep["notes"].append(f"интерфейсы без драйвера {rep['unbound']}: несовместимо (нет драйвера) — захват не пробуется")
            return
        caps = capture_nodes(nodes, target["usb_id"])
        rep["node"] = caps[0] if caps else None
        rep["capture_nodes"] = [n["node"] for n in caps]
        links = v4l_links(rep["dev_listing"])
        rep["by_id"], rep["by_path"] = links["by-id"], links["by-path"]
        if not rep["node"]:
            rep["notes"].append("драйвер есть, узла захвата V4L2 нет")

    def trials(self):
        rep, r, a = self.rep, self.r, self.a
        dev = f"/dev/{rep['node']['node']}"
        self.step("D4 CPU в покое (3 с)")
        m = parse_marked(r.sh("cpu-idle", CPU_CMD.format(body="sleep 3;"))[1])
        rep["cpu_idle"] = cpu_delta((m.get("A") or [""])[0], (m.get("B") or [""])[0], int((m.get("N") or ["4"])[0] or 4))
        rep["kind"] = "cvbs" if is_analog(rep["node"], a.kind) else "hdmi/uvc"
        modes = plan_modes(rep["node"], a.kind, a.top)
        if not modes:
            rep["notes"].append("у узла нет дискретных режимов в поддерживаемых форматах — захват не пробуется")
        for mode in modes:
            if time.monotonic() - self.t0 > a.budget - 2 * a.seconds - 20:
                rep["notes"].append(f"бюджет {a.budget} с исчерпан: режим {mode['key']} и далее не испытаны")
                break
            self.step(f"D5 {mode['key']}{' norm=' + mode['norm'] if mode['norm'] else ''}: захват → fakesink")
            res = dict(mode)
            res["raw"] = parse_raw(r.sh(f"raw:{mode['key']}", raw_cmd(dev, mode, a.seconds), a.seconds + 30)[1])
            self.step(f"D5 {mode['key']}: изображение (frames)")
            fm = parse_marked(r.sh(f"frames:{mode['key']}", CPU_CMD.format(
                body=f"printf '@J %s\\n' \"$({frames_cmd(dev, mode, a.seconds)})\";"), a.seconds + 30)[1])
            fj = _jdict((fm.get("J") or ["{}"])[0])
            res["signal"] = signal_of(fj)
            res["cpu_decode"] = cpu_delta((fm.get("A") or [""])[0], (fm.get("B") or [""])[0],
                                          int((fm.get("N") or ["4"])[0] or 4))
            res["frames_exit_code"] = fj.get("exit_code")
            res["notes"] = mode_note(res)
            rep["modes"].append(res)

    def finish(self):
        rep = self.rep
        rep["duration_s"] = round(time.monotonic() - self.t0, 1)
        rep["verdict"] = overall(rep)
        rep["name"] = self.a.name or default_name(rep.get("target"), rep.get("node"))
        out = Path(self.a.out) if self.a.out else HW_DIR / rep["name"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "diag.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1))
        (out / "diag.txt").write_text(render_text(rep))
        log(f"ИТОГ {rep['verdict']} за {rep['duration_s']} с → {out}/diag.json, diag.txt")
        self.out = out
        return {"OK": 0, "NO-SIGNAL": 1, "ERROR": 2, "BUSY": 2}.get(rep["verdict"], 1)


def plan(a):
    return "\n".join([
        f"S5 диагностика адаптера на {a.remote} (≤ {a.budget} с, без root):",
        "  D0  SSH, id (не root), link-status блока: адаптер выбран службой? → BUSY или --free (synthetic на время)",
        "  D1  lsusb + video_hw_probe usb «до»" + (" → подсказка «подключите адаптер» → «после»: новые VID:PID"
                                                  if a.plug else f" (адаптер уже подключён; --usb-id {a.usb_id or '—'})"),
        "      скорость USB по sysfs, драйверы интерфейсов (/sys/bus/usb/devices/*/driver); нет драйвера → NO-DRIVER",
        "  D2  dmesg -T (без root) | иначе journalctl -k -b | иначе «недоступен»; ошибки USB",
        "  D3  ls /dev/video* /dev/v4l/by-id,by-path; video_hw_probe --json nodes (форматы, размеры, интервалы, входы, "
        "стандарты, QUERYSTD)",
        "  D4  CPU pi2 в покое 3 с (/proc/stat)",
        f"  D5  CVBS: PAL, затем NTSC (norm=…, 720×576 / 720×480); HDMI/UVC: верхние {a.top} режимов (каждый FourCC)",
        f"      каждый: gst-launch v4l2src num-buffers ≈ {a.seconds:g} с ! CAPS ! fakesink — частота по PTS, CPU; "
        f"video_hw_probe frames {a.seconds:g} с — сигнал / поле / нет кадров",
        "  итог: docs/acceptance/s5-video/hw/<имя>/diag.json + diag.txt",
    ])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--remote", default="auto")
    ap.add_argument("--name", help="имя каталога hw/<имя> (по умолчанию VID-PID-драйвер)")
    ap.add_argument("--out", help="каталог итога вместо docs/acceptance/s5-video/hw/<имя>")
    ap.add_argument("--plug", action="store_true", help="снимок «до», подсказка подключить адаптер, снимок «после»")
    ap.add_argument("--usb-id", help="VID:PID испытуемого (если адаптер уже подключён или кандидатов несколько)")
    ap.add_argument("--kind", choices=("auto", "cvbs", "hdmi"), default="auto")
    ap.add_argument("--top", type=int, default=4, help="HDMI/UVC: сколько режимов испытать")
    ap.add_argument("--seconds", type=float, default=6.0, help="длительность каждого пробного захвата")
    ap.add_argument("--settle", type=float, default=5.0, help="пауза после подключения (перечисление, драйвер)")
    ap.add_argument("--budget", type=float, default=BUDGET_S)
    ap.add_argument("--free", action="store_true", help="если служба блока держит захват — временно synthetic")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--fake", nargs="?", const="", default=None, metavar="FIXTURE.json",
                    help="без плат: ответы из файла (по тегам шагов) или встроенный пример usbtv")
    a = ap.parse_args(argv)
    if a.plan:
        print(plan(a))
        return 0
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # tools/: адреса плат по серийному номеру
    from stand import boards
    boards.resolve_args(a, {'remote': 'pi2'})
    if a.fake is not None:
        if not a.out:
            ap.error("--fake: нужен --out (в docs/ подделка не пишется)")
        fx = json.loads(Path(a.fake).read_text()) if a.fake else _usbtv_fixture()
        a.plug = a.plug or not a.usb_id
        return Diag(a, Fake(fx), prompt=lambda text: None).run()
    return Diag(a, Remote(a.remote)).run()


if __name__ == "__main__":
    sys.exit(main())
