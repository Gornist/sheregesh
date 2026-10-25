#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Приёмка S5: матрица RTSP {udp, tcp, auto} × {mjpeg, h264} на двух платах за ≤ 10 мин (docs/acceptance/s5-video/
ACCEPT.md, S5-01…S5-07). Тестовые серверы — на pi1 (tools/probe/s4_rtsp_test_server.py, h264 — v4l2h264enc pi1),
источник — на pi2 (rtsp_urls в video-input.json + SIGHUP главному процессу блока), изображение — в GUI pi1.

  python3 tools/probe/s5_rtsp_matrix.py --plan
  python3 tools/probe/s5_rtsp_matrix.py --console 192.168.1.92 --remote 192.168.1.171 --out DIR [--gui] [--only …]

Контракт (агент NETWORK, S5): ключ `transport: auto|tcp|udp` в описании RTSP-источника и в элементах `rtsp_urls`;
сервер `--transports tcp,udp --codec mjpeg|h264`; фактический транспорт — `status.video.source.transport`, если блок
его даёт; иначе — по сокетам процесса конвейера на pi2 (/proc/PID/fd ↔ /proc/net/udp,tcp: ss и lsof на платах нет):
есть UDP-сокеты у rtspsrc → udp; только TCP-соединение с портом сервера → tcp (interleaved).
Если контракт ещё не развёрнут (сервер без --transports или блок не знает transport), сценарий всё равно
запускается: зависящие от него ячейки — ERROR «контракт NETWORK не развёрнут» (никогда не PASS), остальное судится.

Серверы pi1 (sheregesh-ui, копия в /tmp, PID-файлы /tmp/s5-rtsp-<порт>.pid; S4-сервер 8554 не трогается):
  8601 mjpeg tcp,udp   8602 h264 tcp,udp   8603 mjpeg только tcp   8604 mjpeg только udp

  S5-01  службы обеих плат active/running, failed нет, health PASS, пульт CONNECTED
  S5-02  {udp, tcp, auto} × {mjpeg, h264} (rtsp:0…5): блок streaming с нужным адресом ≤ 15 с, пульт live, счётчик
         декодированных кадров растёт; фактический транспорт = заданному (auto — любой, записывается)
  S5-03  один транспорт недоступен: auto@только-tcp → tcp; auto@только-udp → udp; udp@только-tcp и tcp@только-udp →
         блок error/connect-error без streaming, пульт «нет видео», затем synthetic ≤ 15 с (без зависания)
  S5-04  потеря сервера (kill на --outage с) и возврат: «нет видео» ≤ 3 с, connect-error/no-signal на блоке,
         изображение само ≤ 10 с после запуска сервера
  S5-05  переключения synthetic → rtsp:0 → rtsp:4 → rtsp:8 (отказ) → rtsp:2 → synthetic × --switches: без
         зависаний; ресурсы службы блока и пульта до/после (1 конвейер, fd ±3, потоки ±2, MainPID/NRestarts)
  S5-06  --gui: gui_capture --by select --select rtsp:0…7,synthetic — кадр меняется в GUI pi1 для каждой ячейки
  S5-07  журналы с начала прогона + check_video_link; slot_guard «до/после» относительно базовой линии платы
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/probe"))
sys.path.insert(0, str(ROOT / "tools/deploy/ota-check"))
import s5_accept as A  # noqa: E402

S4, E = A.S4, A.E
STEPS = ("S5-01", "S5-02", "S5-03", "S5-04", "S5-05", "S5-06", "S5-07")
SERVER = ROOT / "tools/probe/s4_rtsp_test_server.py"
SERVERS = {"both-mjpeg": (8601, "mjpeg", "tcp,udp"), "both-h264": (8602, "h264", "tcp,udp"),
           "tcp-mjpeg": (8603, "mjpeg", "tcp"), "udp-mjpeg": (8604, "mjpeg", "udp")}
# (сервер, транспорт источника, ожидание): udp | tcp | any (auto при обоих) | fail (транспорт недоступен)
CELLS = [("both-mjpeg", "udp", "udp"), ("both-mjpeg", "tcp", "tcp"), ("both-mjpeg", "auto", "any"),
         ("both-h264", "udp", "udp"), ("both-h264", "tcp", "tcp"), ("both-h264", "auto", "any"),
         ("tcp-mjpeg", "auto", "tcp"), ("udp-mjpeg", "auto", "udp"),
         ("tcp-mjpeg", "udp", "fail"), ("udp-mjpeg", "tcp", "fail")]
FAIL_STATES = ("error", "connect-error", "no-signal")
NEG_WINDOW_S = 15.0
SWITCH_SEQ = ("synthetic", 0, 4, 8, 2, "synthetic")
log = S4.log


# ---------------------------------------------------------------- чистые функции (тесты: test_s5_rtsp_matrix.py)


def cells(host):
    """Ячейки матрицы → элементы rtsp_urls (номер = индекс, rtsp:<n>). Пути различают ячейки одного сервера:
    videosources.active_id ищет источник по адресу, одинаковые адреса дали бы неверный id."""
    out = []
    for n, (srv, tr, expect) in enumerate(CELLS):
        port, codec, transports = SERVERS[srv]
        out.append({"n": n, "id": f"rtsp:{n}", "server": srv, "port": port, "codec": codec, "transport": tr,
                    "server_transports": transports, "expect": expect,
                    "url": f"rtsp://{host}:{port}/s5-{tr}-{codec}", "name": f"S5 {tr} {codec} @{transports}"})
    return out


def rtsp_urls(cs):
    return [{"url": c["url"], "codec": c["codec"], "transport": c["transport"], "name": c["name"]} for c in cs]


def spec_of(c):
    return {"source": "rtsp", "url": c["url"], "codec": c["codec"], "transport": c["transport"]}


def config_text(orig_text, spec, cs):
    """video-input.json прогона: описание источника + rtsp_urls матрицы; прочие ключи исходного файла сохраняются."""
    base = json.loads(A.merged_config(orig_text, spec))
    base["rtsp_urls"] = rtsp_urls(cs)
    return json.dumps(base, ensure_ascii=False)


def readiness(server_help, board_hits, status_src=None):
    """Развёрнут ли контракт NETWORK: сервер знает --transports (и mjpeg), блок знает transport (grep кода выпуска
    или поле статуса)."""
    return {"server_transports": "--transports" in (server_help or ""),
            "server_codec": "mjpeg" if re.search(r"\bmjpeg\b", server_help or "") else "jpeg",
            "board_transport": bool(board_hits) or "transport" in (status_src or {})}


def cell_blocker(c, ready):
    """Причина, по которой ячейка не доказательна при неполном контракте (None — можно судить)."""
    if c["server_transports"] != "tcp,udp" and not ready["server_transports"]:
        return f"контракт NETWORK не развёрнут: сервер без --transports (нужен только {c['server_transports']})"
    if (c["transport"] != "auto" or c["expect"] == "udp") and not ready["board_transport"]:
        return "контракт NETWORK не развёрнут: блок не знает ключ transport"
    if c["transport"] == "udp" and not ready["server_transports"]:
        return "контракт NETWORK не развёрнут: сервер без UDP (--transports)"
    return None


SOCK_CMD = ("P=$(pgrep -u sheregesh-ui -f '[g]st-launch-1.0.*rtspsrc' | head -1); [ -n \"$P\" ] || { echo NOPROC; exit 0; }; "
            "echo \"PID $P\"; tr '\\0' '\\n' </proc/$P/cmdline | grep -o '^protocols=.*' | sed 's/^/ARG /'; "
            "echo \"INO $(ls -l /proc/$P/fd 2>/dev/null | sed -n 's/.*socket:\\[\\([0-9]*\\)\\].*/\\1/p' | tr '\\n' ' ')\"; "
            "echo UDP; cat /proc/net/udp /proc/net/udp6 2>/dev/null; echo TCP; cat /proc/net/tcp /proc/net/tcp6 2>/dev/null")


def parse_sockets(text, server_port):
    """Вывод SOCK_CMD → {pid, protocols, udp (число UDP-сокетов процесса), tcp_server (TCP ESTABLISHED к порту
    сервера)}; None — процесса rtspsrc нет."""
    if not text or text.strip().startswith("NOPROC"):
        return None
    pid, proto, inodes, sec = None, None, set(), None
    udp = tcp = 0
    for ln in text.splitlines():
        if ln.startswith("PID "):
            pid = ln.split()[1]
        elif ln.startswith("ARG "):
            proto = ln[4:].strip()
        elif ln.startswith("INO"):
            inodes = set(ln.split()[1:])
        elif ln in ("UDP", "TCP"):
            sec = ln
        elif sec and ":" in ln and not ln.strip().startswith("sl"):
            f = ln.split()
            if len(f) < 10 or f[9] not in inodes:
                continue
            if sec == "UDP":
                udp += 1
            elif f[3] == "01" and int(f[2].rsplit(":", 1)[1], 16) == server_port:
                tcp += 1
    return {"pid": pid, "protocols": proto, "udp": udp, "tcp_server": tcp}


def actual_transport(sock, status_src=None):
    """(транспорт, откуда, расхождение). Поле статуса блока — главное; сокеты — сверка (или единственный источник)."""
    by_sock = None
    if sock:
        by_sock = "udp" if sock["udp"] > 0 else "tcp" if sock["tcp_server"] > 0 else None
    st = (status_src or {}).get("transport") or (status_src or {}).get("rtsp_transport")
    st = str(st).lower() if st else None
    if st in ("udp", "tcp"):
        return st, "status", (f"статус {st}, сокеты {by_sock}" if by_sock and by_sock != st else None)
    return by_sock, ("sockets" if by_sock else None), None


def judge_cell(c, m, blocker=None):
    """Ячейка матрицы → (вердикт, проблемы). m — измерения прогона ячейки."""
    if blocker:
        return "ERROR", [blocker]
    p = []
    if c["expect"] == "fail":
        if m.get("streaming_s") is not None:
            p.append(f"блок дошёл до streaming за {m['streaming_s']} с, хотя транспорт {c['transport']} у сервера "
                     f"{c['server_transports']} недоступен")
        if not set(m.get("remote_states_seen") or []) & set(FAIL_STATES):
            p.append(f"на блоке не наблюдалось {'/'.join(FAIL_STATES)}: {m.get('remote_states_seen')}")
        if m.get("console_live_end"):
            p.append("пульт показывает видео при недоступном транспорте")
        if not (m.get("recover") or {}).get("ok"):
            p.append(f"после отказа synthetic не вернулся за {S4.LIMITS['switch_s']} с (зависание?)")
        return ("FAIL" if p else "PASS"), p
    if m.get("streaming_s") is None:
        p.append(f"блок не дошёл до streaming за {S4.LIMITS['switch_s']} с: {m.get('remote_states_seen')}")
    if m.get("console_live_s") is None:
        p.append("пульт не показал видео")
    if m.get("console_live_s") is not None and not m.get("frames_advancing"):
        p.append(f"счётчик кадров пульта не растёт: {m.get('frames')}")
    tr, _src, mismatch = m.get("transport"), m.get("transport_source"), m.get("transport_mismatch")
    if mismatch:
        p.append(f"фактический транспорт не согласован: {mismatch}")
    if p:
        return "FAIL", p
    if tr is None:
        return "ERROR", ["фактический транспорт не определён (нет поля статуса и сокетов rtspsrc)"]
    if c["expect"] in ("udp", "tcp") and tr != c["expect"]:
        return "FAIL", [f"фактический транспорт {tr}, ожидался {c['expect']}"]
    return "PASS", []


def judge_gui_matrix(states, cs):
    """S5-06: для каждой ячейки rtsp:0…7 снимок «selected-rtsp_N»: источник rtsp, кадр меняется (таймкод сервера;
    поле кадра тестового сервера почти чёрное — доля тёмного здесь не критерий)."""
    if not states:
        return "ERROR", ["states.json нет"]
    err = next((s for s in states if s.get("select_error")), None)
    p = []
    for c in cs:
        if c["expect"] == "fail":
            continue
        s = next((x for x in states if str(x.get("shot", "")).endswith(f"-selected-rtsp_{c['n']}-video-t+1s.png")), None)
        if s is None:
            p.append(f"{c['id']} ({c['transport']}/{c['codec']}): снимка нет" + (f" — отказ выбора {err['select_error']}"
                                                                               if err else ""))
            continue
        inp = str((s.get("videoSource") or {}).get("input", "")).lower()
        if inp != "rtsp" or s.get("frame_changed") is not True:
            p.append(f"{c['id']}: источник {inp!r}, кадр меняется {s.get('frame_changed')!r}")
    return ("FAIL" if p else "PASS"), p


def agg(vs):
    return S4.agg(vs)


# ---------------------------------------------------------------- стенд


def remote_rtsp(url):
    def pred(d):
        src = S4.remote_source(d)
        dev = src.get("device") or ""
        return str(src.get("type", "")).lower() == "rtsp" and src.get("state") == "streaming" and (not dev or dev == url)
    return pred


class Matrix:
    def __init__(self, a):
        self.a = a
        self.cs = cells(a.console)
        self.st = S4.Stand(a)            # выборки ui-state pi1 и link-status pi2, ожидание
        self.orig_conf = None
        self.ready = {}
        self.codec_arg = "mjpeg"

    # серверы на pi1
    def server_start(self, key):
        port, codec, transports = SERVERS[key]
        c = self.codec_arg if codec == "mjpeg" else "h264"
        tr = f" --transports {transports}" if self.ready.get("server_transports") else ""
        cmd = (f"nohup python3 /tmp/s4_rtsp_test_server.py --port {port} --codec {c}{tr} --label S5-{port} "
               f"--pidfile /tmp/s5-rtsp-{port}.pid >/tmp/s5-rtsp-{port}.log 2>&1 &")
        A.ssh(self.a.console, cmd)
        for _ in range(20):
            if A.ssh(self.a.console, f"kill -0 $(cat /tmp/s5-rtsp-{port}.pid 2>/dev/null) 2>/dev/null")[0] == 0:
                return True
            time.sleep(0.5)
        return False

    def server_stop(self, key):
        port = SERVERS[key][0]
        A.ssh(self.a.console, f"test -e /tmp/s5-rtsp-{port}.pid && kill $(cat /tmp/s5-rtsp-{port}.pid) 2>/dev/null; true")

    def select(self, target):
        """target: 'synthetic' | номер ячейки. Запись файла с rtsp_urls матрицы + SIGHUP."""
        spec = {"source": "synthetic"} if target == "synthetic" else spec_of(self.cs[target])
        return A.write_config(self.a.remote, config_text(self.orig_conf, spec, self.cs))

    def to_synthetic(self):
        t0 = time.monotonic()
        ok = self.select("synthetic")
        tr = self.st.wait(self.st.rs, lambda d: S4.remote_is(d, "synthetic"), t0, S4.LIMITS["switch_s"])
        tc = self.st.wait(self.st.ui, lambda d: S4.console_live(d, "synthetic"), t0, S4.LIMITS["switch_s"])
        return {"ok": bool(ok and tr is not None and tc is not None), "remote_s": tr, "console_s": tc}

    def frames(self, seconds=2.0):
        r0 = self.st.ui.last()
        f0 = E.vframes(r0[2]) if r0 else 0
        time.sleep(seconds)
        r1 = self.st.ui.last()
        f1 = E.vframes(r1[2]) if r1 else 0
        return [f0, f1], f1 > f0

    def run_cell(self, c):
        m = {"id": c["id"], "url": c["url"], "transport_cfg": c["transport"], "codec": c["codec"],
             "server": c["server"]}
        t0 = time.monotonic()
        if not self.select(c["n"]):
            return {**m, "error": "запись video-input.json/SIGHUP не удались"}
        if c["expect"] == "fail":
            end, fail_at = t0 + NEG_WINDOW_S, None
            while time.monotonic() < end:
                last = self.st.rs.last()
                d = last[2] if last and last[0] >= t0 else {}
                if remote_rtsp(c["url"])(d):
                    break
                if S4.remote_source(d).get("state") in FAIL_STATES:
                    fail_at = fail_at or time.monotonic()
                    if time.monotonic() - fail_at >= 6:           # отказ держится: два периода записи статуса блока
                        break
                time.sleep(0.3)
            rows = self.st.rs.since(t0)
            m["streaming_s"] = S4.first_at(rows, t0, remote_rtsp(c["url"]))
            m["remote_states_seen"] = sorted({S4.remote_source(d).get("state") for _m, _b, d in rows} - {None})
            last = self.st.ui.last()
            m["console_live_end"] = bool(last and S4.console_live(last[2]))
            m["remote_error"] = (S4.remote_source(rows[-1][2]).get("error") if rows else None)
            m["recover"] = self.to_synthetic()
            return m
        m["streaming_s"] = self.st.wait(self.st.rs, remote_rtsp(c["url"]), t0, S4.LIMITS["switch_s"])
        # пульт — от момента, когда блок сообщил новый адрес: до этого live мог относиться к прежней ячейке
        ts = t0 + (m["streaming_s"] or 0)
        tc = self.st.wait(self.st.ui, lambda d: S4.console_live(d, "rtsp"), ts, S4.LIMITS["switch_s"])
        m["console_live_s"] = None if tc is None else round(tc + (m["streaming_s"] or 0), 2)
        m["remote_states_seen"] = sorted({S4.remote_source(d).get("state") for _m, _b, d in self.st.rs.since(t0)} - {None})
        if m["streaming_s"] is not None:
            m["frames"], m["frames_advancing"] = self.frames()
            sock = parse_sockets(A.ssh(self.a.remote, SOCK_CMD)[1], c["port"])
            last = self.st.rs.last()
            src = S4.remote_source(last[2]) if last else {}
            m["sockets"] = sock
            m["status_transport"] = src.get("transport")
            m["fps_actual"], m["stream"] = src.get("fps_actual"), f"{src.get('stream_width')}x{src.get('stream_height')}"
            m["transport"], m["transport_source"], m["transport_mismatch"] = actual_transport(sock, src)
        return m


# ---------------------------------------------------------------- шаги


def step_cells(mx, idx):
    rows, vs, problems = [], [], []
    for n in idx:
        c = mx.cs[n]
        blocker = cell_blocker(c, mx.ready)
        m = {"id": c["id"], "skipped": blocker} if blocker else mx.run_cell(c)
        if m.get("error"):
            v, p = "ERROR", [m["error"]]
        else:
            v, p = judge_cell(c, m, blocker)
        m["verdict"], m["problems"] = v, p
        rows.append(m)
        vs.append(v)
        problems += [f"{c['id']} {c['transport']}/{c['codec']}@{c['server_transports']}: {x}" for x in p]
        log(f"   {c['id']} {c['transport']:4}/{c['codec']:5} @{c['server_transports']:7} → {v}"
            f" (транспорт {m.get('transport')}, {m.get('transport_source')})" + (f": {'; '.join(p)}" if p else ""))
    return agg(vs), problems, rows


def step_loss(mx):
    c = mx.cs[2]                                       # auto/mjpeg @ tcp,udp
    a = mx.a
    if not mx.select(c["n"]) or mx.st.wait(mx.st.ui, lambda d: S4.console_live(d, "rtsp"), time.monotonic(), 15) is None:
        return "ERROR", ["ячейка rtsp:2 не дошла до пульта — отказ не вносится"], {}
    time.sleep(2)
    t0 = time.monotonic()
    mx.server_stop(c["server"])
    m = {"inject": f"kill сервера {c['port']}", "outage_s": a.outage}
    m["off_s"] = mx.st.wait(mx.st.ui, lambda d: not S4.console_live(d), t0, a.outage)
    time.sleep(max(0.0, a.outage - (time.monotonic() - t0)))
    states = {S4.remote_source(d).get("state") for _m, _b, d in mx.st.rs.since(t0)}
    m["remote_states_seen"] = sorted(s for s in states if s)
    m["remote_fault_state"] = next((s for s in ("connect-error", "no-signal", "error") if s in states), None)
    t1 = time.monotonic()
    m["restore_ok"] = mx.server_start(c["server"])
    m["back_s"] = mx.st.wait(mx.st.ui, lambda d: S4.console_live(d, "rtsp"), t1, 30)
    v4, v6, p = S4.judge_fault(m, ("connect-error", "no-signal"))
    return agg([v4, v6]), p, m


def step_switches(mx):
    a = mx.a
    before = S4.resources(a)
    seq = []
    for _ in range(a.switches):
        for t in SWITCH_SEQ:
            t0 = time.monotonic()
            if t == "synthetic":
                r = mx.to_synthetic()
            else:
                c = mx.cs[t]
                mx.select(t)
                if c["expect"] == "fail":
                    time.sleep(4)
                    r = {"ok": True, "note": "ячейка отказа: только выбор, затем следующий источник"}
                else:
                    tr = mx.st.wait(mx.st.rs, remote_rtsp(c["url"]), t0, S4.LIMITS["switch_s"])
                    tc = mx.st.wait(mx.st.ui, lambda d: S4.console_live(d, "rtsp"), t0, S4.LIMITS["switch_s"])
                    r = {"ok": tr is not None and tc is not None, "remote_s": tr, "console_s": tc}
            seq.append({"to": t, **r})
    time.sleep(3)
    after = S4.resources(a)
    _rv, rp = S4.judge_resources(before, after)
    hang = [s for s in seq if not s["ok"]]
    p = rp + [f"переключение на {s['to']} не завершилось за {S4.LIMITS['switch_s']} с" for s in hang]
    return ("FAIL" if p else "PASS"), p, {"switches": seq, "resources_before": before, "resources_after": after}


def step_gui(mx):
    ids = ",".join(c["id"] for c in mx.cs if c["expect"] != "fail" and not cell_blocker(c, mx.ready)) + ",synthetic"
    if not mx.to_synthetic()["ok"]:
        return "ERROR", ["перед съёмкой GUI источник не вернулся на synthetic"], {}
    states, notes = A.run_gui(mx.a, f"--by select --select {ids} --timeout 300", lambda phase: None, "rtsp")
    if states is None:
        return "ERROR", [notes.get("error", "gui_capture не запущен")], {}
    cs = [c for c in mx.cs if not cell_blocker(c, mx.ready)]
    v, p = judge_gui_matrix(states, cs)
    return v, p, {"selected": ids, "states": len(states)}


def plan(a):
    lines = [f"S5 RTSP-матрица (серверы на pi1 {a.console}, источник на pi2 {a.remote}; ≈ 7 мин, --gui ≈ 10 мин):",
             "  0  предпроверка (серийные номера, OTA в покое), snap «до»; контракт NETWORK: сервер --transports? блок "
             "transport?",
             "     серверы pi1: " + "; ".join(f"{p} {c} {t}" for p, c, t in SERVERS.values()),
             "  1  S5-01 службы, health, пульт CONNECTED                                               ~0,3 мин"]
    lines += ["  2  S5-02 ячейки rtsp:0…5 (выбор файлом, streaming/live, кадры, транспорт); ячейки:      ~1 мин"]
    lines += [f"       {c['id']}: {c['transport']:4} × {c['codec']:5} @ сервер {c['server_transports']:7} → "
              f"{ {'any': 'live, транспорт любой', 'fail': 'error без зависания'}.get(c['expect'], 'live, ' + c['expect'])}"
              for c in cells(a.console)]
    lines += ["  3  S5-03 ячейки rtsp:6…9 — один транспорт недоступен                                 ~1 мин",
              f"  4  S5-04 kill сервера 8601 на {a.outage:g} с при rtsp:2, возврат                           ~0,5 мин",
              f"  5  S5-05 переключения {' → '.join(map(str, SWITCH_SEQ))} × {a.switches}, ресурсы        ~1,5 мин",
              f"  6  S5-06 GUI: gui_capture --by select rtsp:0…7,synthetic: {'да' if a.gui else 'нет → ERROR'}  ~3 мин",
              "  7  S5-07 журналы + check_video_link; snap «после», сравнение с базовой линией         ~2,5 мин",
              "Реально: две платы, сеть, rtspsrc/UDP/TCP, декодирование и GUI пульта. Модель: камера — тестовый сервер "
              "(videotestsrc); GUI — Qt offscreen без cage/HDMI."]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console", default="192.168.1.92")
    ap.add_argument("--remote", default="192.168.1.171")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--gui", action="store_true", help="S5-06: снимки GUI pi1 по всем ячейкам (пульт стоит ≈ 3 мин)")
    ap.add_argument("--outage", type=float, default=6.0)
    ap.add_argument("--switches", type=int, default=1)
    ap.add_argument("--only", default=",".join(STEPS))
    ap.add_argument("--plan", action="store_true")
    a = ap.parse_args(argv)
    a.rtsp_url = a.rtsp_codec = a.source_cmd = None                  # для S4.Stand
    if a.plan:
        print(plan(a))
        return 0
    if not a.out:
        ap.error("--out обязателен")
    a.out.mkdir(parents=True, exist_ok=True)
    only = set(a.only.split(","))
    ids, problems = E.preflight(argparse.Namespace(console=a.console, remote=a.remote,
                                                   console_serial=E.SERIALS["console"],
                                                   remote_serial=E.SERIALS["remote"]))
    meta = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "boards": ids, "args": {k: str(v) for k, v in vars(a).items()}}
    if problems:
        (a.out / "summary.json").write_text(json.dumps({**meta, "preflight": problems, "verdict": "ERROR"},
                                                       ensure_ascii=False, indent=1))
        log("ПРЕДПРОВЕРКА: " + "; ".join(problems) + " — без действий")
        return 2
    ctx = {"t_start_epoch": float(E.out(a.console, "date +%s.%N") or time.time())}
    t0 = time.monotonic()
    before = A.snap_all(a, "before") if "S5-07" in only else None
    mx = Matrix(a)
    help_text = subprocess.run([sys.executable, str(SERVER), "--help"], capture_output=True, text=True).stdout
    rel = "C=$(readlink -f /var/lib/sheregesh-deploy/current 2>/dev/null || echo /usr/lib/sheregesh)"
    hits = E.out(a.remote, f"{rel}; grep -l transport $C/software/sheregesh/videosources.py "
                           "$C/software/sheregesh/videoinput.py 2>/dev/null")
    status_src = S4.remote_source(E.jload(E.out(a.remote, f"cat {S4.REMOTE_STATUS}")))
    mx.ready = readiness(help_text, hits.strip(), status_src)
    mx.codec_arg = mx.ready["server_codec"]
    meta["contract"] = mx.ready
    log(f"контракт NETWORK: {mx.ready}")
    mx.orig_conf = E.out(a.remote, f"cat {S4.VCONF} 2>/dev/null || echo __ABSENT__")
    results = {}

    def put(step, v, detail, extra=None):
        results[step] = {"verdict": v, "detail": detail, **({"measured": extra} if extra is not None else {})}
        log(f"{step}: {v}" + (f" — {'; '.join(detail)}" if v != "PASS" and detail else ""))
    subprocess.run(["scp", "-q", str(SERVER), f"sheregesh-ui@{a.console}:/tmp/"], check=False)
    mx.st.ui.start()
    mx.st.rs.start()
    started, need = [], set()
    try:
        time.sleep(2)
        if "S5-01" in only:
            r = S4.step_services(a, ctx)
            last = mx.st.ui.last()
            conn = bool(last and E.link_up(last[2]))
            put("S5-01", "PASS" if r["verdict"] == "PASS" and conn else "FAIL",
                [] if conn else ["пульт не CONNECTED"], r)
        need = {"S5-02", "S5-03", "S5-04", "S5-05", "S5-06"} & only
        if need:
            for key in SERVERS:
                if mx.server_start(key):
                    started.append(key)
            missing = [k for k in SERVERS if k not in started]
            if missing:
                log(f"серверы не запустились: {missing} (журналы /tmp/s5-rtsp-<порт>.log на pi1)")
            meta["servers_started"] = started
            time.sleep(3)                                  # каталог блока проверяет доступность раз в 2 с
        if "S5-02" in only:
            v, p, rows = step_cells(mx, range(0, 6))
            put("S5-02", v, p, rows)
        if "S5-03" in only:
            v, p, rows = step_cells(mx, range(6, 10))
            put("S5-03", v, p, rows)
        if "S5-04" in only:
            v, p, m = step_loss(mx)
            put("S5-04", v, p, m)
        if "S5-05" in only:
            v, p, m = step_switches(mx)
            put("S5-05", v, p, m)
        if "S5-06" in only:
            if a.gui:
                v, p, m = step_gui(mx)
                ctx["console_restarted"] = True
                put("S5-06", v, p, m)
            else:
                put("S5-06", "ERROR", ["без --gui визуального доказательства нет (ui-state не доказательство)"])
    finally:
        if need:
            A.restore_config(a.remote, mx.orig_conf)
        for key in SERVERS:
            mx.server_stop(key)
        mx.st.ui.stop()
        mx.st.rs.stop()
    if "S5-07" in only:
        j = S4.step_journal(a, ctx)
        after = A.snap_all(a, "after")
        ota = {}
        for board in ("pi1", "pi2"):
            v, bad, checks = A.judge_ota(board, before[board], after[board], ctx.get("console_restarted", False))
            (a.out / f"{board}-compare.txt").write_text("\n".join(f"{c['status']} {c['id']} — {c['detail']}"
                                                                 for c in checks) + f"\n== {v}\n")
            ota[board] = {"verdict": v, "unexpected": bad}
        v = agg([j["verdict"], *(o["verdict"] for o in ota.values())])
        put("S5-07", v, [] if v == "PASS" else [f"журналы {j['verdict']}"] +
            [f"OTA {b} {o['verdict']} {o['unexpected']}" for b, o in ota.items()], {"journal": j, "ota": ota})
    for k in STEPS:
        results.setdefault(k, {"verdict": "SKIP", "detail": ["не запрошен (--only)"]})
    summary = {**meta, "cells": cells(a.console), "duration_min": round((time.monotonic() - t0) / 60, 1),
               "results": results, "verdict": A.verdict_of(results)}
    (a.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    log(f"ИТОГ {summary['verdict']} за {summary['duration_min']} мин: " +
        ", ".join(f"{k} {v['verdict']}" for k, v in results.items()))
    return {"PASS": 0, "FAIL": 1, "ERROR": 2, A.BLOCKED: 3, "SKIP": 2}[summary["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
