#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Приёмка S4 «видео» на двух платах за ≤ 15 мин (docs/acceptance/s4-video/ACCEPT.md). С машины владельца по SSH.

  python3 tools/probe/s4_accept.py --plan                                    # только план, без SSH
  python3 tools/probe/s4_accept.py --console 192.168.1.92 --remote 192.168.1.171 --out DIR \\
      [--gui] [--rtsp-url rtsp://H:8554/s4 --rtsp-stop 'CMD' --rtsp-start 'CMD'] [--only VID-01,VID-02,…]

Сценарий переиспользует готовые средства и ничего не дублирует:
  e2e_run.py          предпроверка (серийные номера, OTA в покое), выборка ui-state пульта, ресурсы, журналы, health
  check_link.py       VID-02: --only health,identity (без перезапуска пульта)
  check_video_link.py VID-08 доп.: --only session,stream,leftovers
  gui_capture.py      VID-03/04 (+ рендеринг VID-05/06): реальный рендеринг сцены QML на pi1, --by video
  slot_guard.py       VID-09: снимки «до/после» обеих плат, --mode unchanged: чистый PASS и состояние платы по снимку

Воздействия на платы (только права sheregesh-ui, юниты и образ не меняются; всё возвращается в finally):
  · SIGSTOP/SIGCONT конвейера видеоприёма pi2 (модель «источник перестал отдавать кадры»; страховочный SIGCONT на плате);
  · ~/.config/sheregesh/video-input.json на pi2 + SIGHUP службы блока (смена источника); в конце — исходный источник;
  · --rtsp-stop/--rtsp-start — команды владельца для тестового RTSP-сервера (tools/probe/s4_rtsp_test_server.py, агент B);
  · --gui: остановка sheregesh-console на pi1 на время gui_capture (≈ 1,5 мин), затем start.
Вердикты PASS | FAIL | ERROR | SKIP. ERROR — испытание не доказательно (никогда не PASS); SKIP — не запрошено или нет
условия (например, нет --rtsp-url). Итог: FAIL > ERROR > PASS; SKIP итог не улучшает и указывается в протоколе.
"""
import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/probe"))
sys.path.insert(0, str(ROOT / "tools/deploy/ota-check"))
import e2e_run as E  # noqa: E402

REMOTE_STATUS = "/run/sheregesh-remote/link-status.json"
RX_STATE = "/run/sheregesh-video/state.json"           # служба rx пульта (контракт видео §4)
VCONF = "~/.config/sheregesh/video-input.json"
GUI_DIR = "/tmp/s4-gui"
STEPS = ("VID-01", "VID-02", "VID-03", "VID-04", "VID-05", "VID-06", "VID-07", "VID-08", "VID-09")
DARK_MIN = 0.8                 # доля почти чёрных пикселей поля кадра при отказе источника (gui_capture frame_dark)
LIMITS = {"video_off_s": 3.0, "video_back_s": 10.0, "switch_s": 15.0}
# VID-09: slot_guard --mode unchanged считает направление от слота снимка «до» (cf200e2), поэтому на любом слоте
# (pi1 на B COMMITTED, pi2 на A) ожидается чистый PASS. Эти проверки обязаны присутствовать и пройти:
REQUIRED_PASS = ("slot.active_unchanged", "bootconfig.autoboot", "health", "failed")
OTA_REST = ("IDLE", "COMMITTED", "ROLLED_BACK")       # покой OTA, как предпроверка e2e_run
BOOT_SLOT = {2: "A", 3: "B"}                          # boot_partition → слот
# Шум, не относящийся к службам «Шерегеша» (RESULT.md цикла 2): GPU без дисплея под cage.
JOURNAL_NOISE = ("vc4-drm", "swiotlb")
SAFE_CONT_S = 15        # страховочный SIGCONT на pi2, даже если Mac или SSH пропадут


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- чистые функции суждения (тесты: test_s4_qa_accept.py)


def verdict_of(results):
    vs = [r["verdict"] for r in results.values()]
    return "FAIL" if "FAIL" in vs else "ERROR" if "ERROR" in vs else "PASS" if "PASS" in vs else "SKIP"


def board_state(before, after):
    """Инварианты ожидаемого состояния платы по снимкам slot_guard (не по тексту деталей): оба снимка верны, слот по
    пяти признакам ядра не сменился и совпадает с агентом OTA, OTA в покое, пробной загрузки нет, основной раздел
    autoboot — активный слот. → список нарушений."""
    import slot_guard as G
    out = []
    infos = []
    for tag, snap in (("до", before), ("после", after)):
        errs, info = G.validate(snap)
        if errs:
            out.append(f"снимок «{tag}» неполон: {'; '.join(errs)[:200]}")
        infos.append(info)
    if out:
        return out
    active = infos[1]["active"]
    if infos[0]["active"] != active:
        out.append(f"слот сменился: {infos[0]['active']} → {active}")
    ota = json.loads(after["fields"]["ota.status"])
    st, slots = ota.get("state") or {}, ota.get("slots") or {}
    if st.get("state") not in OTA_REST:
        out.append(f"OTA {st.get('state')!r} не в покое ({'/'.join(OTA_REST)})")
    if ota.get("consistent") is not True:
        out.append("агент OTA сообщает несогласованное состояние слотов")
    if slots.get("active") != active or st.get("active") != active:
        out.append(f"слот агента OTA {slots.get('active')}/{st.get('active')} ≠ слоту по ядру {active}")
    if slots.get("tryboot_now") is not False:
        out.append("идёт пробная загрузка (tryboot_now)")
    default = BOOT_SLOT.get((ota.get("autoboot") or {}).get("default_part"))
    if default != active:
        out.append(f"основной раздел autoboot — слот {default}, активен {active}")
    return out


def judge_slot_guard(board, verdict, checks, console_restarted=False, before=None, after=None):
    """slot_guard --mode unchanged → вердикт VID-09. Любой ERROR — ERROR; нет снимков — ERROR (нечем проверить плату).
    Обязательные проверки REQUIRED_PASS есть и прошли; любой FAIL — FAIL, кроме same.unit.sheregesh-console на pi1,
    если шаг сам перезапускал пульт (--gui); состояние платы — board_state по снимкам."""
    if verdict == "ERROR" or any(c["status"] == "ERROR" for c in checks):
        return "ERROR", [c["id"] for c in checks if c["status"] == "ERROR"]
    if before is None or after is None:
        return "ERROR", ["нет снимков slot_guard «до/после»: состояние платы не проверено"]
    unexpected = []
    for c in checks:
        if c["status"] == "PASS":
            continue
        if console_restarted and board == "pi1" and c["id"] == "same.unit.sheregesh-console":
            continue
        unexpected.append(f"{c['id']}: {c['detail']}")
    status = {c["id"]: c["status"] for c in checks}
    unexpected += [f"нет проверки {k}" for k in REQUIRED_PASS if k not in status]
    unexpected += board_state(before, after)
    return ("FAIL" if unexpected else "PASS"), unexpected


def first_at(rows, t0, pred):
    """Секунды от t0 до первой выборки (mono, board, doc) с pred(doc); None — не наблюдалось."""
    for mono, _b, doc in rows:
        if mono >= t0 and doc and pred(doc):
            return round(mono - t0, 2)
    return None


def console_live(ui, source=None):
    vs = ui.get("videoSource") or {}
    return bool(vs.get("live")) and (source is None or str(vs.get("input", "")).lower() == source)


def remote_is(st, kind, state="streaming"):
    src = remote_source(st)
    return kind in (str(src.get("type", "")).lower(), str(src.get("input", "")).lower()) and src.get("state") == state


def jload(text):
    try:
        d = json.loads(text or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def rx_fault(transitions, wall0):
    """'rx:degraded' — служба rx пульта с момента wall0 переходила в degraded/recovering (отказ источника под SRTP)."""
    seen = {t[1] for t in transitions if isinstance(t, list) and len(t) >= 2 and t[0] >= wall0 - 1}
    return "rx:degraded" if seen & {"degraded", "recovering"} else None


def remote_source(st):
    return ((st.get("video") or {}).get("source") or {})


def judge_fault(m, fault_states):
    """Потеря/возврат → (VID-05, VID-06, проблемы). «Нет видео» на пульте ≤ 3 с и состояние отказа на блоке
    наблюдалось (VID-05); изображение вернулось само ≤ 10 с (VID-06). Пороги REQ-SW-002 пп.3–4 — по аналогии:
    требование о потоке, здесь — источник блока."""
    off, back = [], []
    if m.get("off_s") is None:
        off.append("пульт не перестал показывать кадры")
    elif m["off_s"] > LIMITS["video_off_s"]:
        off.append(f"«нет видео» через {m['off_s']} с > {LIMITS['video_off_s']}")
    if m.get("remote_fault_state") not in fault_states:
        off.append(f"на блоке не наблюдалось {'/'.join(fault_states)}: {m.get('remote_fault_state')!r}")
    if m.get("back_s") is None:
        back.append("изображение не вернулось само")
    elif m["back_s"] > LIMITS["video_back_s"]:
        back.append(f"возврат через {m['back_s']} с > {LIMITS['video_back_s']}")
    return ("FAIL" if off else "PASS"), ("FAIL" if back else "PASS"), off + back


def agg(vs):
    return "ERROR" if not vs else "FAIL" if "FAIL" in vs else "ERROR" if "ERROR" in vs else "PASS"


def judge_resources(before, after, fd_tol=3, thr_tol=2):
    """VID-07: ресурсы службы блока и пульта до/после переключений. Строго: ровно один конвейер на плате, сирот и
    зомби нет, NRestarts и MainPID прежние. Допуск: дескрипторы ±fd_tol, потоки ±thr_tol (сокеты сеансов TLS)."""
    problems = []
    for role in ("remote", "console"):
        b, a = before[role], after[role]
        if a.get("gst_count") != 1 or a.get("gst_orphans"):
            problems.append(f"{role}: конвейеров {a.get('gst_count')}, сирот {len(a.get('gst_orphans') or [])}")
        if a.get("zombies"):
            problems.append(f"{role}: зомби {a['zombies']}")
        if a.get("nrestarts") != b.get("nrestarts"):
            problems.append(f"{role}: NRestarts {b.get('nrestarts')} → {a.get('nrestarts')}")
        if (b.get("service") or {}).get("MainPID") != (a.get("service") or {}).get("MainPID"):
            problems.append(f"{role}: MainPID сменился")
        pb, pa = {p["pid"]: p for p in b.get("procs", [])}, {p["pid"]: p for p in a.get("procs", [])}
        for pid in pb.keys() & pa.keys():
            dfd, dth = pa[pid]["fds"] - pb[pid]["fds"], pa[pid]["threads"] - pb[pid]["threads"]
            if abs(dfd) > fd_tol:
                problems.append(f"{role}: дескрипторы {pid} {pb[pid]['fds']} → {pa[pid]['fds']}")
            if abs(dth) > thr_tol:
                problems.append(f"{role}: потоки {pid} {pb[pid]['threads']} → {pa[pid]['threads']}")
        if not pb.keys() & pa.keys():
            problems.append(f"{role}: процесс приложения не найден до/после")
    return ("FAIL" if problems else "PASS"), problems


def judge_gui(states, want_fault=None):
    """VID-03/04 (и рендеринг VID-05/06) по states.json gui_capture. Доказательство — PNG; states — привязка
    снимков к состоянию. stream: frame_changed=true при источнике synthetic; отказ: поле кадра чёрное
    (frame_dark ≥ DARK_MIN — подпись «no frames for N s» в поле меняется, frame_changed тут не критерий; для старых
    states без frame_dark — frame_changed=false); восстановление: frame_changed=true."""
    by = {s["shot"]: s for s in states}
    res = {}

    def pair(prefix):
        s = by.get(f"{prefix}-video-t+1s.png")
        return s, (s or {}).get("frame_changed")
    s, ch = pair("01-02-stream")
    src = ((s or {}).get("videoSource") or {}).get("input")
    res["VID-03"] = ("PASS" if s and src == "synthetic" else "ERROR" if not s else "FAIL",
                     f"снимок 01-02-stream: источник {src!r}")
    res["VID-04"] = ("PASS" if ch is True else "ERROR" if s is None else "FAIL",
                     f"смена области кадра за 1 с: {ch!r}")
    if want_fault:
        s, ch = pair(f"03-04-{want_fault}")
        dark = (s or {}).get("frame_dark")
        ok = (dark >= DARK_MIN) if dark is not None else ch is False
        res["VID-05"] = ("PASS" if s and ok else "ERROR" if s is None else "FAIL",
                         f"снимок {want_fault}: поле кадра чёрное (доля {dark!r}, порог {DARK_MIN}), прежний кадр "
                         f"не показан: {ok}")
        s, ch = pair(f"05-06-recovered-{want_fault}")
        res["VID-06"] = ("PASS" if ch is True else "ERROR" if s is None else "FAIL",
                         f"после восстановления кадр сменяется: {ch!r}")
    return res


def journal_problems(lines):
    """VID-08: строки журнала, которые делают шаг FAIL (Traceback, аварийные завершения юнитов sheregesh-*)."""
    bad = []
    for ln in lines:
        if any(n in ln for n in JOURNAL_NOISE):
            continue
        low = ln.lower()
        if "traceback" in low or "failed with result" in low or "main process exited, code=" in low \
                or ("stopped: result=" in low and "result=success" not in low):
            bad.append(ln)
    return bad


def spec_json(source, rtsp_url=None, codec="h264"):
    return json.dumps({"source": "rtsp", "url": rtsp_url, "codec": codec} if source == "rtsp" else {"source": source})


# ---------------------------------------------------------------- работа со стендом


class RemoteSampler(E.Sampler):
    """То же, что выборка пульта, но link-status.json блока (состояние источника на pi2)."""
    CMD = ("while :; do printf '%s ' \"$(date +%s.%N)\"; tr -d '\\n' < " + REMOTE_STATUS + " 2>/dev/null || printf '{}'; "
           "echo; sleep 0.25; done")


def ssh(host, cmd, timeout=60):
    return E.ssh(host, cmd, timeout)


def run_tool(args, timeout=900):
    r = subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def zombies(host, unit):
    pid = E.out(host, f"systemctl show -p MainPID --value {unit}")
    return E.out(host, f"ps -o pid,stat --ppid {pid} --no-headers | awk '$2 ~ /Z/' | wc -l").strip() if pid.isdigit() else "?"


def resources(a):
    r = {"console": E.resources(a.console, "console"), "remote": E.resources(a.remote, "remote")}
    z = zombies(a.remote, E.REMOTE_UNIT)
    r["remote"]["zombies"] = int(z) if z.isdigit() else None
    return r


class Stand:
    def __init__(self, a):
        self.a = a
        self.ui = E.Sampler(a.console)
        self.rs = RemoteSampler(a.remote)
        self.orig_conf = None          # исходное содержимое video-input.json (None — файла не было)

    def srtp(self):
        """Видео идёт через службы (ADR-0016): источник блока — процесс sheregesh-video-tx, а не gst-launch агента."""
        return (jload(E.out(self.a.console, f"cat {E.UI} 2>/dev/null")).get("video") or {}).get("mode") == "remote-srtp"

    def gst_pgid(self):
        """Группа процессов источника кадров блока: SRTP — служба tx (SIGSTOP группы = «источник не отдаёт кадры»);
        TLS — конвейер gst-launch …rtpstreampay агента."""
        if self.srtp():
            pid = E.out(self.a.remote, "systemctl show -p MainPID --value sheregesh-video-tx").strip()
            pg = E.out(self.a.remote, f"ps -o pgid= -p {pid}").strip() if pid not in ("", "0") else ""
            return pg or None
        rows = E.out(self.a.remote, "ps -eo pid,pgid,args --no-headers | grep -E 'gst-launch-1.0.*rtpstreampay' | "
                                    "grep -v grep").split("\n")
        return rows[0].split()[1] if rows and rows[0].strip() else None

    def rx_fault_since(self, wall0):
        """SRTP: при замороженной tx состояние источника блока не наблюдаемо (процесс стоит) — отказ виден на приёмнике
        пульта: переход службы rx в degraded/recovering с меткой ≥ wall0 (часы плат согласованы chrony, ADR-0017)."""
        st = (jload(E.out(self.a.console, f"cat {RX_STATE} 2>/dev/null")).get("streams") or {}).get("main") or {}
        return rx_fault(st.get("transitions") or [], wall0)

    def wait(self, sampler, pred, t0, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            t = first_at(sampler.since(t0), t0, pred)
            if t is not None:
                return t
            time.sleep(0.2)
        return None

    def set_source(self, source):
        """--source-cmd (команда выбора источника, например команда пульта; {source}, {url} подставляются) — если
        задана; иначе описание источника атомарно в video-input.json и SIGHUP только главному процессу службы блока
        (kill -HUP <MainPID>; `systemctl kill` без --kill-whom=main бьёт и gst-launch). Удаление файла + SIGHUP
        источник не меняет (agent.video_spec: «not found» → конфигурация не перечитана), поэтому возврат — записью."""
        if self.a.source_cmd:
            cmd = self.a.source_cmd.format(source=source, url=shlex.quote(self.a.rtsp_url or ""))
            return subprocess.run(cmd, shell=True, timeout=30).returncode == 0
        body = shlex.quote(spec_json(source, self.a.rtsp_url, self.a.rtsp_codec))
        code, _ = ssh(self.a.remote, f"umask 077; printf '%s' {body} > {VCONF}.tmp && mv {VCONF}.tmp {VCONF} && "
                                     f"kill -HUP $(systemctl show -p MainPID --value {E.REMOTE_UNIT})")
        return code == 0

    def switch(self, source, timeout=LIMITS["switch_s"]):
        t0 = time.monotonic()
        if not self.set_source(source):
            return {"to": source, "ok": False, "reason": "запись конфигурации/SIGHUP не удались"}
        kind = "rtsp" if source == "rtsp" else "synthetic"
        tr = self.wait(self.rs, lambda d: remote_is(d, kind), t0, timeout)
        tc = self.wait(self.ui, lambda d: console_live(d, kind), t0, timeout)
        return {"to": source, "remote_streaming_s": tr, "console_live_s": tc, "ok": tr is not None and tc is not None}

    def fault(self, kind, outage):
        """Потеря и возврат источника. synthetic — SIGSTOP конвейера (модель «нет кадров»); rtsp — остановка сервера."""
        a, m = self.a, {"kind": kind, "outage_s": outage}
        pre_rows = self.rs.last()
        m["remote_restarts_before"] = remote_source(pre_rows[2]).get("restarts") if pre_rows else None
        t0, wall0, srtp = time.monotonic(), time.time(), self.srtp()
        m["transport"] = "srtp" if srtp else "tls"
        if kind == "synthetic":
            pg = self.gst_pgid()
            if not pg:
                return {**m, "error": "нет конвейера видеоприёма на pi2"}
            ssh(a.remote, f"nohup sh -c 'sleep {SAFE_CONT_S}; kill -CONT -- -{pg}' >/dev/null 2>&1 &")
            ok = ssh(a.remote, f"kill -STOP -- -{pg}")[0] == 0
            m["inject"] = f"SIGSTOP группы {pg}"
        else:
            ok = subprocess.run(a.rtsp_stop, shell=True, timeout=30).returncode == 0
            m["inject"] = "rtsp-stop"
        if not ok:
            return {**m, "error": "отказ не внесён"}
        m["off_s"] = self.wait(self.ui, lambda d: not console_live(d), t0, outage)
        time.sleep(max(0.0, outage - (time.monotonic() - t0)))
        states = {remote_source(d).get("state") for _m, _b, d in self.rs.since(t0)}
        m["remote_states_seen"] = sorted(s for s in states if s)
        m["remote_fault_state"] = next((s for s in ("no-signal", "connect-error", "error", "no-device")
                                        if s in states), None)
        if srtp and kind == "synthetic":
            m["remote_fault_state"] = self.rx_fault_since(wall0)
        t1 = time.monotonic()
        if kind == "synthetic":
            ok = ssh(a.remote, f"kill -CONT -- -{pg} 2>/dev/null; echo done")[0] == 0
        else:
            ok = subprocess.run(a.rtsp_start, shell=True, timeout=30).returncode == 0
        m["restore_ok"] = ok
        m["back_s"] = self.wait(self.ui, lambda d: console_live(d), t1, 30)
        return m


def snap_all(a, tag):
    import slot_guard as G
    out = {}
    for board, host in (("pi1", a.console), ("pi2", a.remote)):
        d = G.snap(board, host)
        errs, _info = G.validate(d)
        d["valid"], d["validation_errors"] = not errs, errs
        (a.out / f"{board}-{tag}.json").write_text(json.dumps(d, ensure_ascii=False, indent=1))
        out[board] = d
    return out


# ---------------------------------------------------------------- шаги


def step_services(a, ctx):
    rows = {}
    for role, host, unit in (("console", a.console, E.CONSOLE_UNIT), ("remote", a.remote, E.REMOTE_UNIT)):
        show = E.out(host, f"systemctl show -p ActiveState,SubState,NRestarts,MainPID {unit} | tr '\\n' ' '")
        failed = E.out(host, "systemctl --failed --no-legend --plain")
        rows[role] = {"unit": unit, "show": show, "failed": failed, "health": E.health(host, role)}
    ok = all("ActiveState=active" in r["show"] and "SubState=running" in r["show"] and not r["failed"]
             and r["health"]["verdict"] == "PASS" for r in rows.values())
    ctx["nrestarts0"] = {k: v["show"] for k, v in rows.items()}
    return {"verdict": "PASS" if ok else "FAIL", "boards": rows}


def step_link(a, ctx):
    d = a.out / "check_link"
    code, text = run_tool([ROOT / "tools/probe/check_link.py", "--console", a.console, "--remote", a.remote,
                           "--out", d, "--only", "health,identity"])
    s = E.jload((d / "summary.json").read_text()) if (d / "summary.json").exists() else {}
    ui = E.jload(E.out(a.console, f"cat {E.UI}"))
    st = E.jload(E.out(a.remote, f"cat {REMOTE_STATUS}"))
    facts = {"check_link": s.get("results"), "peerLink": ui.get("peerLink"), "linkTls": ui.get("linkTls"),
             "remote_tls": st.get("tls"), "video_transport": (st.get("video") or {}).get("transport")}
    if not s:
        return {"verdict": "ERROR", "reason": "check_link без summary.json", "log": text[-2000:]}
    ok = (all(v == "PASS" for v in (s.get("results") or {}).values()) and facts["peerLink"] == "CONNECTED"
          and facts["linkTls"] == "TLSv1.3" and facts["video_transport"] == "tls1.3-mutual")
    return {"verdict": "PASS" if ok else "FAIL", **facts}


def step_gui(a, ctx, stand):
    """VID-03/04 и рендеринг потери/возврата синтетического источника: gui_capture --by video на pi1."""
    rel = "C=$(readlink -f /var/lib/sheregesh-deploy/current 2>/dev/null || echo /usr/lib/sheregesh)"
    pp = "PYTHONPATH=$C/software:$C/software/emulators:$C/software/console:$C/software/remote"   # как launcher.py
    subprocess.run(["scp", "-q", str(ROOT / "tools/probe/gui_capture.py"), f"sheregesh-ui@{a.console}:/tmp/"], check=False)
    ctx["console_restarted"] = True
    srtp = stand.srtp()                 # до остановки пульта: потом ui-state.json устаревает
    try:
        if ssh(a.console, f"systemctl stop {E.CONSOLE_UNIT}")[0] != 0:
            return {"VID-03": ("ERROR", "служба пульта не остановилась")}
        ssh(a.console, f"rm -rf {GUI_DIR}; {rel}; nohup env {pp} python3 /tmp/gui_capture.py --out {GUI_DIR} --by video "
                       f"--video-states no-signal --timeout {35 if srtp else 120} >{GUI_DIR}.log 2>&1 &")

        def phase(name, timeout):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if ssh(a.console, f"test -e {GUI_DIR}/phase-{name}")[0] == 0:
                    return True
                time.sleep(1)
            return False
        notes = {"stream": phase("01-02-stream", 60), "transport": "srtp" if srtp else "tls"}
        # SRTP: при остановке потока GUI показывает «starting» (состояние TLS-приёма блока), а не no-signal — открытый
        # дефект показа (2026-10-30); снимки отказа здесь не делаются, VID-05/06 — по step_faults
        if notes["stream"] and not srtp:
            pg = stand.gst_pgid()
            if pg:
                ssh(a.remote, f"nohup sh -c 'sleep {SAFE_CONT_S}; kill -CONT -- -{pg}' >/dev/null 2>&1 &")
                ssh(a.remote, f"kill -STOP -- -{pg}")
                notes["no-signal"] = phase("03-04-no-signal", 12)
                ssh(a.remote, f"kill -CONT -- -{pg}")
                notes["recovered"] = phase("05-06-recovered-no-signal", 30)
        for _ in range(45):                                     # gui_capture завершается сам после done/timeout
            if ssh(a.console, "pgrep -u sheregesh-ui -f '[/]tmp/gui_capture.py' >/dev/null")[0] != 0:
                break
            time.sleep(1)
    finally:
        ssh(a.console, f"pkill -u sheregesh-ui -f '[/]tmp/gui_capture.py'; systemctl start {E.CONSOLE_UNIT}")
        time.sleep(8)          # ui-state.json остановленного пульта устарел: ждём, пока новый процесс его перепишет
    subprocess.run(["scp", "-q", "-r", f"sheregesh-ui@{a.console}:{GUI_DIR}", str(a.out / "gui")], check=False)
    sf = a.out / "gui" / "states.json"
    states = json.loads(sf.read_text()) if sf.exists() else []
    res = judge_gui(states, want_fault=None if notes.get("transport") == "srtp" else "no-signal")
    res["_notes"] = notes
    return res


def step_faults(a, ctx, stand):
    """VID-05/06 по выборке ui-state (служба пульта штатная): synthetic — всегда; rtsp — при --rtsp-*."""
    out = {"synthetic": [], "rtsp": []}
    for _ in range(a.cycles):
        out["synthetic"].append(stand.fault("synthetic", a.outage))
        stand.wait(stand.ui, console_live, time.monotonic(), 10)
        time.sleep(3)
    if a.rtsp_url and a.rtsp_stop and a.rtsp_start:
        sw = stand.switch("rtsp")
        out["to_rtsp"] = sw
        if sw["ok"]:
            for _ in range(a.cycles):
                out["rtsp"].append(stand.fault("rtsp", a.outage))
                time.sleep(3)
        out["to_synthetic"] = stand.switch("synthetic")
    v5, v6, problems = [], [], []
    for kind, ms in (("synthetic", out["synthetic"]), ("rtsp", out["rtsp"])):
        for m in ms:
            if m.get("error"):
                v5.append("ERROR")
                v6.append("ERROR")
                problems.append(f"{kind}: {m['error']}")
                continue
            fs = ("rx:degraded",) if m.get("transport") == "srtp" and kind == "synthetic" else \
                ("no-signal",) if kind == "synthetic" else ("connect-error", "no-signal")
            a5, a6, p = judge_fault(m, fs)
            v5.append(a5)
            v6.append(a6)
            problems += [f"{kind}: {x}" for x in p]
    note = "" if out["rtsp"] else "RTSP не испытан (нет --rtsp-url/--rtsp-stop/--rtsp-start) — проверено частично"
    detail = "; ".join([*problems, note]).strip("; ")
    return {"VID-05": (agg(v5), detail), "VID-06": (agg(v6), detail), "_measured": out}


def step_switch(a, ctx, stand):
    if not a.rtsp_url:
        return {"verdict": "SKIP", "reason": "нет --rtsp-url (тестовый RTSP-сервер агента B)"}
    before = resources(a)
    sw = []
    for _ in range(a.switches):
        sw.append(stand.switch("rtsp"))
        sw.append(stand.switch("synthetic"))
    time.sleep(3)
    after = resources(a)
    rv, rp = judge_resources(before, after)
    hang = [s for s in sw if not s["ok"]]
    problems = rp + [f"переключение на {s['to']} не завершилось за {LIMITS['switch_s']} с: {s}" for s in hang]
    return {"verdict": "FAIL" if problems else "PASS", "switches": sw, "problems": problems,
            "resources_before": before, "resources_after": after}


def step_journal(a, ctx):
    since = ctx["t_start_epoch"]
    lines = {}
    for role, host, unit in (("console", a.console, E.CONSOLE_UNIT), ("remote", a.remote, E.REMOTE_UNIT)):
        text = E.out(host, f"journalctl --since @{int(since)} -o short-iso --no-pager -u {unit} "
                           "-u sheregesh-ota.service -u sheregesh-health.service SYSLOG_IDENTIFIER=sheregesh-notify-relay "
                           "| tail -2000", 120)
        tb = E.out(host, f"journalctl --since @{int(since)} -o cat --no-pager | grep -c Traceback || true")
        failed = E.out(host, "systemctl --failed --no-legend --plain")
        nr = E.out(host, f"systemctl show -p NRestarts --value {unit}")
        lines[role] = {"problems": journal_problems(text.splitlines()), "traceback_total": tb, "failed": failed,
                       "nrestarts": nr}
    code, text = run_tool([ROOT / "tools/probe/check_video_link.py", "--console", a.console, "--remote", a.remote,
                           "--out", a.out / "check_video_link", "--only", "session,stream,leftovers"])
    sfile = a.out / "check_video_link" / "summary.json"
    cvl = json.loads(sfile.read_text()).get("results") if sfile.exists() else None
    ok = all(not v["problems"] and v["traceback_total"] in ("0", "") and not v["failed"] for v in lines.values())
    ok = ok and cvl is not None and all(v == "PASS" for v in cvl.values())
    return {"verdict": "PASS" if ok else ("ERROR" if cvl is None else "FAIL"), "boards": lines,
            "check_video_link": cvl}


def step_ota(a, ctx, before, after):
    import slot_guard as G
    res = {}
    for board in ("pi1", "pi2"):
        ver = (json.loads(after[board]["fields"].get("build", "{}") or "{}")).get("ota_version", "?") \
            if after[board].get("fields") else "?"
        v, checks = G.compare(before[board], after[board], "unchanged", ver)
        (a.out / f"{board}-compare.txt").write_text("\n".join(f"{c['status']} {c['id']} — {c['detail']}" for c in checks)
                                                   + f"\n== {v}\n")
        jv, unexpected = judge_slot_guard(board, v, checks, ctx.get("console_restarted", False) and board == "pi1",
                                          before=before[board], after=after[board])
        res[board] = {"slot_guard": v, "verdict": jv, "unexpected": unexpected}
    vs = [r["verdict"] for r in res.values()]
    return {"verdict": "FAIL" if "FAIL" in vs else "ERROR" if "ERROR" in vs else "PASS", "boards": res}


# ---------------------------------------------------------------- план и запуск


def plan(a):
    rtsp = "есть" if a.rtsp_url else "нет (VID-07 SKIP, VID-05/06 только synthetic — частично)"
    return "\n".join([
        "S4 приёмка — порядок (≈ 11–14 мин):",
        "  0  предпроверка: серийные номера, OTA ∈ IDLE/COMMITTED/ROLLED_BACK; slot_guard snap pi1/pi2 «до»   ~1 мин",
        "  1  VID-01 службы, failed, health обеих ролей                                                  ~0,3 мин",
        "  2  VID-02 check_link health,identity; TLS 1.3 управления, tls1.3-mutual видео                 ~1 мин",
        f"  3  VID-03/04 (+рендер VID-05/06) gui_capture --by video на pi1: {'да' if a.gui else 'нет (--gui) → ERROR'}  ~2 мин",
        f"  4  VID-05/06 потеря/возврат: synthetic SIGSTOP конвейера {a.outage} с × {a.cycles}; RTSP: {rtsp}  ~2–3 мин",
        f"  5  VID-07 переключения synthetic↔rtsp × {a.switches}, ресурсы до/после                       ~2 мин",
        "  6  VID-08 журналы с начала прогона, failed, NRestarts; check_video_link session,stream,leftovers ~1,5 мин",
        "  7  VID-09 slot_guard snap «после», compare --mode unchanged: PASS и плата в покое на своём слоте  ~1 мин",
        "Модели (не реальные отказы): SIGSTOP конвейера = «источник перестал отдавать кадры»; остановка RTSP-сервера = "
        "«камера/кодер недоступны». Реальные захваты — USB-DIAG.md после доставки.",
    ])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console", default="auto")
    ap.add_argument("--remote", default="auto")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--only", default=",".join(STEPS))
    ap.add_argument("--gui", action="store_true", help="VID-03/04: остановить пульт и снять реальный рендеринг")
    ap.add_argument("--rtsp-url")
    ap.add_argument("--rtsp-codec", default="h264", choices=("h264", "mjpeg"),
                    help="кодек RTSP-источника (тестовый сервер s4_rtsp_test_server.py — mjpeg)")
    ap.add_argument("--rtsp-stop", help="команда (на Mac), останавливающая тестовый RTSP-сервер")
    ap.add_argument("--rtsp-start", help="команда (на Mac), запускающая его снова")
    ap.add_argument("--source-cmd", help="команда выбора источника на Mac ({source} = synthetic|rtsp, {url}); "
                                        "по умолчанию video-input.json + SIGHUP главному процессу блока")
    ap.add_argument("--outage", type=float, default=6.0)
    ap.add_argument("--cycles", type=int, default=2)
    ap.add_argument("--switches", type=int, default=3)
    ap.add_argument("--plan", action="store_true")
    a = ap.parse_args(argv)
    if a.plan:
        print(plan(a))
        return 0
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # tools/: адреса плат по серийному номеру
    from stand import boards
    boards.resolve_args(a, {'console': 'pi1', 'remote': 'pi2'})
    if not a.out:
        ap.error("--out обязателен")
    a.out.mkdir(parents=True, exist_ok=True)
    only = set(a.only.split(","))
    ids, problems = E.preflight(argparse.Namespace(console=a.console, remote=a.remote,
                                                   console_serial=E.SERIALS["console"],
                                                   remote_serial=E.SERIALS["remote"]))
    meta = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "boards": ids, "args": {k: str(v) for k, v in vars(a).items()}}
    if problems:
        meta["preflight"] = problems
        (a.out / "summary.json").write_text(json.dumps({**meta, "verdict": "ERROR"}, ensure_ascii=False, indent=1))
        log("ПРЕДПРОВЕРКА: " + "; ".join(problems) + " — без действий")
        return 2
    ctx = {"t_start_epoch": float(E.out(a.console, "date +%s.%N") or time.time())}
    t0 = time.monotonic()
    before = snap_all(a, "before") if "VID-09" in only else None
    stand = Stand(a)
    stand.orig_conf = E.out(a.remote, f"cat {VCONF} 2>/dev/null || echo __ABSENT__")
    stand.ui.start()
    stand.rs.start()
    results = {}

    def put(step, v, detail):
        results[step] = {"verdict": v, "detail": detail}
        log(f"{step}: {v}" + (f" — {detail}" if v != "PASS" and isinstance(detail, str) else ""))
    try:
        time.sleep(2)
        if "VID-01" in only:
            r = step_services(a, ctx)
            put("VID-01", r["verdict"], r)
        if "VID-02" in only:
            r = step_link(a, ctx)
            put("VID-02", r["verdict"], r)
        gui = {}
        if a.gui and ({"VID-03", "VID-04"} & only):
            gui = step_gui(a, ctx, stand)
            stand.wait(stand.ui, console_live, time.monotonic(), 60)       # пульт вернулся и показывает видео
        for k in ("VID-03", "VID-04"):
            if k in only:
                put(k, *(gui.get(k) or ("ERROR", "без --gui визуального доказательства нет (ui-state не доказательство)")))
        if {"VID-05", "VID-06"} & only:
            r = step_faults(a, ctx, stand)
            (a.out / "faults.json").write_text(json.dumps(r["_measured"], ensure_ascii=False, indent=1))
            for k in ("VID-05", "VID-06"):
                v, d = r[k]
                gv = gui.get(k)
                if gv and gv[0] != "PASS" and v == "PASS":                 # рендеринг отказа не подтвердился
                    v, d = gv[0], f"{d}; рендеринг: {gv[1]}"
                put(k, v, d)
        if "VID-07" in only:
            r = step_switch(a, ctx, stand)
            put("VID-07", r["verdict"], r)
    finally:
        if stand.orig_conf is not None and a.rtsp_url:                    # исходный источник — явной записью + SIGHUP
            ssh(a.remote, f"printf '%s' {shlex.quote(spec_json('synthetic'))} > {VCONF}.tmp && mv {VCONF}.tmp {VCONF} && "
                          f"kill -HUP $(systemctl show -p MainPID --value {E.REMOTE_UNIT})")
            if stand.orig_conf.strip() == "__ABSENT__":
                ssh(a.remote, f"rm -f {VCONF}")                            # после возврата на synthetic: как было
            else:
                ssh(a.remote, f"printf '%s' {shlex.quote(stand.orig_conf)} > {VCONF} && "
                              f"kill -HUP $(systemctl show -p MainPID --value {E.REMOTE_UNIT})")
        stand.ui.stop()
        stand.rs.stop()
    if "VID-08" in only:
        r = step_journal(a, ctx)
        put("VID-08", r["verdict"], r)
    if "VID-09" in only:
        after = snap_all(a, "after")
        r = step_ota(a, ctx, before, after)
        put("VID-09", r["verdict"], r)
    for k in STEPS:
        results.setdefault(k, {"verdict": "SKIP", "detail": "не запрошен (--only)"})
    summary = {**meta, "duration_min": round((time.monotonic() - t0) / 60, 1), "results": results,
               "verdict": verdict_of(results)}
    (a.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    log(f"ИТОГ {summary['verdict']} за {summary['duration_min']} мин: " +
        ", ".join(f"{k} {v['verdict']}" for k, v in results.items()))
    return {"PASS": 0, "FAIL": 1, "ERROR": 2, "SKIP": 2}[summary["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
