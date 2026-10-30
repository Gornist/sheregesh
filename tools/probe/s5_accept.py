#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Приёмка S5: один USB-адаптер захвата (v4l2:<id>) на двух платах за 5–10 мин (docs/acceptance/s5-video/ACCEPT.md,
S5-11…S5-15). Поверх tools/probe/s4_accept.py: те же выборки ui-state pi1 / link-status pi2, службы, связь, журналы,
ресурсы и slot_guard; добавлены источник V4L2, выбор через GUI-путь и ручной отказ (человек вынимает кабель).

  python3 tools/probe/s5_accept.py --plan --source v4l2:<id>                 # только план, без SSH
  python3 tools/probe/s5_accept.py --source v4l2:<id>|v4l2 --out DIR [--gui] [--gui-fault]
      [--manual-fault cable|usb] [--standard pal|ntsc] [--mode WxH@fps] [--format F] [--source-cmd CMD] [--only …]

  S5-01  службы, failed, health; связь TLS 1.3 / видео tls1.3-mutual (s4_accept VID-01/02)
  S5-11  выбор v4l2:<id> тем же вызовом, что кнопка «Видео» (gui_capture --by select --select <id>,synthetic):
         снимки потока адаптера в GUI pi1, кадр меняется; возврат на synthetic — тоже GUI-путём (VID-03/04)
  S5-12  потеря сигнала: ЧЕЛОВЕК вынимает кабель CVBS/HDMI (или USB — --manual-fault usb) по подсказке; пульт
         «нет видео» ≤ 3 с, на блоке no-signal (no-device) (VID-05). --gui-fault — ещё цикл со снимками GUI
  S5-13  возврат кабеля: изображение само ≤ 10 с (VID-06)
  S5-14  synthetic ↔ v4l2:<id> × --switches, без зависаний; ресурсы до/после (VID-07)
  S5-15  журналы с начала прогона + check_video_link; slot_guard «до/после» обеих плат (VID-08/09)

Нет адаптера в status.video.sources блока (или он available=false) → S5-11…14 «BLOCKED: hardware unavailable»
(никогда не PASS). Вердикты: PASS | FAIL | ERROR | BLOCKED | SKIP; итог FAIL > ERROR > BLOCKED > PASS > SKIP.

Почему «нет сигнала» для HDMI судится по состоянию блока, а доля тёмного поля — только при ярком потоке: при отказе
пульт гасит поле кадра (frame_dark ≥ 0,8), но и сам поток HDMI может быть тёмным (тёмный рабочий стол, чёрный экран
источника) — тогда «чёрное поле» ничего не доказывает. Поэтому основной критерий — состояние источника на блоке
(no-signal / no-device) и «нет видео» на пульте по ui-state; поле кадра — дополнительный, только если снимок потока
был ярким (frame_dark ≤ 0,5). Адаптер, который без сигнала шлёт синее/чёрное поле и не сообщает статус входа, на блоке
остаётся streaming — это FAIL (ограничение класса устройства, videoingest: детектора пустого кадра нет).
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
import s4_accept as S4  # noqa: E402

E = S4.E
STEPS = ("S5-01", "S5-11", "S5-12", "S5-13", "S5-14", "S5-15")
V4L2_INPUTS = ("hdmi", "cvbs", "usb", "v4l2")
BRIGHT_MAX = 0.5          # поток считается ярким, если доля почти чёрных пикселей поля кадра не больше
BLOCKED = "BLOCKED"
HW_UNAVAILABLE = "BLOCKED: hardware unavailable"
FAULTS = {"cable": ("no-signal",), "usb": ("no-device",)}
PROMPTS = {"cable": ("ВЫНЬТЕ кабель сигнала (CVBS/HDMI) из адаптера и нажмите Enter в тот же момент… ",
                     "ВЕРНИТЕ кабель сигнала и нажмите Enter в тот же момент… "),
           "usb": ("ОТКЛЮЧИТЕ адаптер от USB pi2 и нажмите Enter в тот же момент… ",
                   "ПОДКЛЮЧИТЕ адаптер обратно к тому же порту USB и нажмите Enter в тот же момент… ")}
log = S4.log


# ---------------------------------------------------------------- чистые функции (тесты: test_s5_qa_accept.py)


def verdict_of(results):
    vs = [r["verdict"] for r in results.values()]
    for v in ("FAIL", "ERROR", BLOCKED, "PASS"):
        if v in vs:
            return v
    return "SKIP"


def resolve_source(sources, want):
    """want: v4l2 (единственный захват блока) | v4l2:<id> → (id, None) или (None, причина). Список — status.video.sources."""
    v4l2 = [s for s in sources or [] if str(s.get("id", "")).startswith("v4l2:")]
    if want in ("v4l2", "v4l2:", "v4l2:auto"):
        avail = [s for s in v4l2 if s.get("available")]
        if len(avail) == 1:
            return avail[0]["id"], None
        return None, (f"{HW_UNAVAILABLE}: захватов V4L2 в списке блока нет" if not avail else
                      f"захватов несколько {[s['id'] for s in avail]} — укажите --source v4l2:<id>")
    hit = next((s for s in v4l2 if s.get("id") == want), None)
    if hit is None:
        return None, f"{HW_UNAVAILABLE}: {want} нет в списке источников блока ({[s.get('id') for s in v4l2] or 'пусто'})"
    if not hit.get("available"):
        return None, f"{HW_UNAVAILABLE}: {want} в списке, но available=false (устройство отключено)"
    return want, None


def v4l2_spec(source_id, standard="auto", mode="auto", fmt="auto"):
    return {"source": "v4l2", "device": source_id.split(":", 1)[1], "mode": mode, "format": fmt, "standard": standard}


def merged_config(orig_text, spec):
    """Новое содержимое video-input.json: описание источника заменяется, прочие ключи (rtsp_urls …) сохраняются."""
    try:
        cur = json.loads(orig_text) if orig_text and orig_text.strip() != "__ABSENT__" else {}
    except ValueError:
        cur = {}
    cur = cur if isinstance(cur, dict) else {}
    keep = {k: v for k, v in cur.items() if k not in S4_SPEC_KEYS}
    return json.dumps({**keep, **spec}, ensure_ascii=False)


S4_SPEC_KEYS = {"source", "device", "mode", "format", "standard", "url", "codec", "latency_ms", "timeout_s",
                "transport"}


def remote_v4l2(st, state="streaming"):
    src = S4.remote_source(st)
    return str(src.get("type", "")).lower() in ("v4l2", "uvc") and src.get("state") == state


def console_v4l2(ui):
    vs = ui.get("videoSource") or {}
    return bool(vs.get("live")) and str(vs.get("input", "")).lower() in V4L2_INPUTS


def judge_manual_fault(m, fault_states):
    """S5-12/13 по измерениям ручного отказа. Поверх S4.judge_fault: если блок всё время оставался streaming —
    отдельное объяснение (адаптер шлёт поле без статуса входа)."""
    v12, v13, problems = S4.judge_fault(m, fault_states)
    seen = m.get("remote_states_seen") or []
    if v12 == "FAIL" and seen and set(seen) <= {"streaming"}:
        problems.append("блок весь отказ сообщал streaming: адаптер без сигнала шлёт поле и не сообщает статус входа "
                        "(ограничение класса устройства, детектора пустого кадра нет)")
    return v12, v13, problems


def judge_gui_select(states, sid):
    """S5-11 по states.json gui_capture --by select --select <sid>,synthetic."""
    tag = sid.replace(":", "_")
    by = {s.get("shot"): s for s in states}
    err = next((s for s in states if s.get("select_error")), None)
    v = by.get(f"03-04-selected-{tag}-video-t+1s.png")
    back = by.get("05-06-selected-synthetic-video-t+1s.png")
    if v is None:
        if err:
            return "FAIL", f"блок отклонил выбор: {err.get('select_error')}"
        return ("ERROR" if not states else "FAIL"), f"снимка потока {sid} нет (states: {len(states)})"
    inp = str((v.get("videoSource") or {}).get("input", "")).lower()
    problems = []
    if inp not in V4L2_INPUTS:
        problems.append(f"на снимке источник {inp!r}, ожидался захват ({'/'.join(V4L2_INPUTS)})")
    if v.get("frame_changed") is not True:
        problems.append(f"кадр адаптера за 1 с не сменился ({v.get('frame_changed')!r}): источник без движения?")
    if back is None:
        problems.append("возврат на synthetic GUI-путём не снят")
    elif back.get("frame_changed") is not True:
        problems.append("после возврата на synthetic кадр не меняется")
    return ("FAIL" if problems else "PASS"), "; ".join(problems) or \
        f"{sid}: вход {inp}, кадр меняется, яркость поля (тёмная доля) {v.get('frame_dark')}"


def judge_gui_fault(states, fault):
    """Рендеринг отказа (--gui-fault). Снимок отказа существует ⇔ пульт ≥ 2 с показывал состояние fault (gui_capture
    --by video). Доля тёмного — только если поток был ярким (frame_dark ≤ BRIGHT_MAX), иначе неприменима."""
    by = {s.get("shot"): s for s in states}
    st = by.get("01-02-stream-video-t+1s.png")
    f = by.get(f"03-04-{fault}-video-t+1s.png")
    rec = by.get(f"05-06-recovered-{fault}-video-t+1s.png")
    if st is None:
        return "ERROR", "снимка потока нет", {}
    bright = st.get("frame_dark") is not None and st["frame_dark"] <= BRIGHT_MAX
    notes = {"stream_dark": st.get("frame_dark"), "dark_applicable": bright}
    if f is None:
        return "FAIL", f"пульт не показал «{fault}» ≥ 2 с (на блоке отказ не наступил?)", notes
    problems = []
    if bright and (f.get("frame_dark") or 0) < S4.DARK_MIN:
        problems.append(f"поле кадра при отказе не погашено: доля тёмного {f.get('frame_dark')} < {S4.DARK_MIN}")
    if rec is None:
        problems.append("восстановление не снято")
    elif rec.get("frame_changed") is not True:
        problems.append("после возврата кадр не меняется")
    why = "" if bright else (f"поток тёмный (доля тёмного {st.get('frame_dark')} > {BRIGHT_MAX}) — критерий по полю "
                             "кадра неприменим, суждение по состоянию блока/пульта")
    return ("FAIL" if problems else "PASS"), "; ".join(problems + ([why] if why else [])), notes


def judge_ota(board, before, after, console_restarted=False):
    """slot_guard --mode unchanged относительно базовой линии самой платы: FAIL сравнения «до» с самим собой —
    свойства состояния (слот B, autoboot) и допустимы с той же деталью; любой иной FAIL — изменение за прогон."""
    import slot_guard as G
    ver = "?"
    _v0, base = G.compare(before, before, "unchanged", ver)
    v, checks = G.compare(before, after, "unchanged", ver)
    if v == "ERROR" or any(c["status"] == "ERROR" for c in base + checks):
        return "ERROR", [c["id"] for c in base + checks if c["status"] == "ERROR"], checks
    allowed = {c["id"]: c["detail"] for c in base if c["status"] == "FAIL"}
    bad = [f"{c['id']}: {c['detail']}" for c in checks if c["status"] == "FAIL"
           and allowed.get(c["id"]) != c["detail"]
           and not (console_restarted and board == "pi1" and c["id"] == "same.unit.sheregesh-console")]
    return ("FAIL" if bad else "PASS"), bad, checks


# ---------------------------------------------------------------- стенд


def ssh(host, cmd, timeout=60):
    return E.ssh(host, cmd, timeout)


def hup():
    return f"kill -HUP $(systemctl show -p MainPID --value {E.REMOTE_UNIT})"


def write_config(remote, text):
    return ssh(remote, f"umask 077; printf '%s' {shlex.quote(text)} > {S4.VCONF}.tmp && mv {S4.VCONF}.tmp {S4.VCONF} && "
                       f"{hup()}")[0] == 0


def restore_config(remote, orig):
    """Исходный video-input.json: было — записать и SIGHUP; не было — synthetic + SIGHUP, затем удалить (S4 §2)."""
    if orig is None:
        return
    if orig.strip() == "__ABSENT__":
        write_config(remote, json.dumps({"source": "synthetic"}))
        ssh(remote, f"rm -f {S4.VCONF}")
    else:
        write_config(remote, orig)


def run_gui(a, args, during, sub):
    """gui_capture на pi1 при остановленной службе пульта (уроки S4: шаблон '[/]tmp/…' — pkill не убивает свою оболочку;
    PYTHONPATH как у launcher.py, с software/emulators). during(phase) — внесение воздействий по меткам фаз."""
    rel = "C=$(readlink -f /var/lib/sheregesh-deploy/current 2>/dev/null || echo /usr/lib/sheregesh)"
    pp = "PYTHONPATH=$C/software:$C/software/emulators:$C/software/console:$C/software/remote"
    gdir = f"/tmp/s5-gui-{sub}"
    subprocess.run(["scp", "-q", str(ROOT / "tools/probe/gui_capture.py"), f"sheregesh-ui@{a.console}:/tmp/"], check=False)
    notes = {}
    try:
        if ssh(a.console, f"systemctl stop {E.CONSOLE_UNIT}")[0] != 0:
            return None, {"error": "служба пульта не остановилась"}
        ssh(a.console, f"rm -rf {gdir}; {rel}; nohup env {pp} python3 /tmp/gui_capture.py --out {gdir} {args} "
                       f">{gdir}.log 2>&1 &")

        def phase(name, timeout):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if ssh(a.console, f"test -e {gdir}/phase-{name}")[0] == 0:
                    return True
                time.sleep(1)
            return False
        notes = during(phase) or {}
        for _ in range(240):
            if ssh(a.console, "pgrep -u sheregesh-ui -f '[/]tmp/gui_capture.py' >/dev/null")[0] != 0:
                break
            time.sleep(1)
    finally:
        ssh(a.console, f"pkill -u sheregesh-ui -f '[/]tmp/gui_capture.py'; systemctl start {E.CONSOLE_UNIT}")
        time.sleep(8)
    dest = a.out / f"gui-{sub}"
    subprocess.run(["scp", "-q", "-r", f"sheregesh-ui@{a.console}:{gdir}", str(dest)], check=False)
    sf = dest / "states.json"
    return (json.loads(sf.read_text()) if sf.exists() else []), notes


class Stand(S4.Stand):
    """Стенд S4 с источником V4L2: выбор файлом (с сохранением rtsp_urls) или --source-cmd; ручной отказ."""

    def __init__(self, a, prompt=E.fresh_input):
        super().__init__(a)
        self.sid, self.prompt = None, prompt

    def set_source(self, source):
        a = self.a
        sid = self.sid if source == "v4l2" else "synthetic"
        if a.source_cmd:
            return subprocess.run(a.source_cmd.format(source=shlex.quote(sid)), shell=True, timeout=30).returncode == 0
        spec = v4l2_spec(sid, a.standard, a.mode, a.format) if source == "v4l2" else {"source": "synthetic"}
        return write_config(a.remote, merged_config(self.orig_conf, spec))

    def switch(self, source, timeout=S4.LIMITS["switch_s"]):
        t0 = time.monotonic()
        if not self.set_source(source):
            return {"to": source, "ok": False, "reason": "выбор источника не удался"}
        if source == "v4l2":
            rp, cp = remote_v4l2, console_v4l2
        else:
            rp, cp = (lambda d: S4.remote_is(d, "synthetic")), (lambda d: S4.console_live(d, "synthetic"))
        tr = self.wait(self.rs, rp, t0, timeout)
        tc = self.wait(self.ui, cp, t0, timeout)
        return {"to": source, "remote_streaming_s": tr, "console_live_s": tc, "ok": tr is not None and tc is not None}

    def manual_fault(self, kind, outage):
        """Человек снимает сигнал (Enter — момент действия, ±1 с), ждём ≥ outage, человек возвращает."""
        m = {"kind": kind, "outage_s": outage, "inject": f"человек: {kind}"}
        take, give = PROMPTS[kind]
        self.prompt(take)
        t0 = time.monotonic()
        m["off_s"] = self.wait(self.ui, lambda d: not S4.console_live(d), t0, max(outage, 8))
        left = outage - (time.monotonic() - t0)
        if left > 0:
            print(f"   держите отключённым ещё {left:.0f} с…", flush=True)
            time.sleep(left)
        states = {S4.remote_source(d).get("state") for _m, _b, d in self.rs.since(t0)}
        m["remote_states_seen"] = sorted(s for s in states if s)
        m["remote_fault_state"] = next((s for s in ("no-signal", "no-device", "error") if s in states), None)
        self.prompt(give)
        t1 = time.monotonic()
        m["restore_ok"] = True
        m["back_s"] = self.wait(self.ui, console_v4l2, t1, 30)
        m["remote_back_s"] = self.wait(self.rs, remote_v4l2, t1, 30)
        return m


# ---------------------------------------------------------------- шаги


def step_select_gui(a, stand):
    sid = stand.sid
    if not stand.switch("synthetic")["ok"]:
        return "ERROR", "перед выбором через GUI источник не вернулся на synthetic", {}
    states, notes = run_gui(a, f"--by select --select {sid},synthetic --timeout 150", lambda phase: None, "select")
    if states is None:
        return "ERROR", notes.get("error", "gui_capture не запущен"), {}
    v, d = judge_gui_select(states, sid)
    return v, d, {"states": len(states)}


def step_gui_fault(a, stand, fault):
    state = FAULTS[fault][0]
    take, give = PROMPTS[fault]

    def during(phase):
        n = {"stream": phase("01-02-stream", 60)}
        if n["stream"]:
            stand.prompt(take)
            n[state] = phase(f"03-04-{state}", 20)
            time.sleep(1)
            stand.prompt(give)
            n["recovered"] = phase(f"05-06-recovered-{state}", 40)
        return n
    states, notes = run_gui(a, f"--by video --video-states {state} --timeout 150", during, "fault")
    if states is None:
        return "ERROR", notes.get("error", "gui_capture не запущен"), notes
    v, d, extra = judge_gui_fault(states, state)
    return v, d, {**notes, **extra}


def step_faults(a, stand):
    out = []
    for _ in range(a.cycles):
        out.append(stand.manual_fault(a.manual_fault, a.outage))
        stand.wait(stand.ui, console_v4l2, time.monotonic(), 10)
        time.sleep(2)
    v12, v13, problems = [], [], []
    for m in out:
        a12, a13, p = judge_manual_fault(m, FAULTS[a.manual_fault])
        v12.append(a12)
        v13.append(a13)
        problems += p
    return S4.agg(v12), S4.agg(v13), "; ".join(problems), out


def step_switch(a, stand):
    before = S4.resources(a)
    sw = []
    for _ in range(a.switches):
        sw.append(stand.switch("synthetic"))
        sw.append(stand.switch("v4l2"))
    time.sleep(3)
    after = S4.resources(a)
    rv, rp = S4.judge_resources(before, after)
    hang = [s for s in sw if not s["ok"]]
    problems = rp + [f"переключение на {s['to']} не завершилось за {S4.LIMITS['switch_s']} с: {s}" for s in hang]
    return ("FAIL" if problems else "PASS"), problems, {"switches": sw, "resources_before": before,
                                                         "resources_after": after}


def snap_all(a, tag):
    return S4.snap_all(a, tag)


def plan(a):
    return "\n".join([
        f"S5 приёмка адаптера {a.source} (≈ 6–8 мин; --gui +2, --gui-fault +2):",
        "  0  предпроверка (серийные номера, OTA в покое), slot_guard snap «до», источник в status.video.sources",
        "     нет адаптера / available=false → S5-11…14 BLOCKED: hardware unavailable",
        "  1  S5-01 службы, health, связь TLS (s4 VID-01/02)                                         ~1,3 мин",
        f"  2  S5-11 GUI-путь: gui_capture --by select --select {a.source},synthetic: {'да' if a.gui else 'нет → ERROR'}  ~2 мин",
        f"  3  S5-12/13 ручной отказ «{a.manual_fault}» × {a.cycles}: подсказка — ЧЕЛОВЕК вынимает, через ≥ {a.outage:g} с "
        "возвращает                                                                          ~1 мин",
        f"     --gui-fault: ещё цикл со снимками GUI (поле кадра — только при ярком потоке): {'да' if a.gui_fault else 'нет'}",
        f"  4  S5-14 synthetic ↔ {a.source} × {a.switches}, ресурсы до/после                                  ~1 мин",
        "  5  S5-15 журналы + check_video_link; slot_guard «после», сравнение с базовой линией платы  ~2,5 мин",
        "Реально: адаптер, кабель, GUI-путь выбора. Модель: GUI снимается Qt offscreen (без cage/HDMI пульта).",
    ])


def main(argv=None, prompt=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console", default="auto")
    ap.add_argument("--remote", default="auto")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--source", default="v4l2", help="v4l2:<id> из status.video.sources блока или v4l2 (единственный)")
    ap.add_argument("--standard", default="auto", choices=("auto", "pal", "ntsc"))
    ap.add_argument("--mode", default="auto", help="auto | WxH@fps")
    ap.add_argument("--format", default="auto", help="auto | yuyv | uyvy | mjpeg | h264")
    ap.add_argument("--source-cmd", help="команда выбора на Mac ({source} = v4l2:<id> | synthetic) вместо файла")
    ap.add_argument("--manual-fault", choices=tuple(FAULTS), default="cable")
    ap.add_argument("--outage", type=float, default=8.0)
    ap.add_argument("--cycles", type=int, default=1)
    ap.add_argument("--switches", type=int, default=2)
    ap.add_argument("--gui", action="store_true", help="S5-11: выбор через GUI-путь и снимки (пульт стоит ≈ 1,5 мин)")
    ap.add_argument("--gui-fault", action="store_true", help="ещё один ручной отказ со снимками GUI")
    ap.add_argument("--only", default=",".join(STEPS))
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
        (a.out / "summary.json").write_text(json.dumps({**meta, "preflight": problems, "verdict": "ERROR"},
                                                       ensure_ascii=False, indent=1))
        log("ПРЕДПРОВЕРКА: " + "; ".join(problems) + " — без действий")
        return 2
    ctx = {"t_start_epoch": float(E.out(a.console, "date +%s.%N") or time.time())}
    t0 = time.monotonic()
    before = snap_all(a, "before") if "S5-15" in only else None
    stand = Stand(a, prompt or E.fresh_input)
    stand.orig_conf = E.out(a.remote, f"cat {S4.VCONF} 2>/dev/null || echo __ABSENT__")
    st = E.jload(E.out(a.remote, f"cat {S4.REMOTE_STATUS}"))
    sid, why = resolve_source((st.get("video") or {}).get("sources"), a.source)
    stand.sid = sid
    meta["source"] = {"want": a.source, "id": sid, "problem": why}
    results = {}

    def put(step, v, detail, extra=None):
        results[step] = {"verdict": v, "detail": detail, **({"measured": extra} if extra else {})}
        log(f"{step}: {v}" + (f" — {detail}" if v != "PASS" and detail else ""))
    stand.ui.start()
    stand.rs.start()
    changed = False
    try:
        time.sleep(2)
        if "S5-01" in only:
            r1, r2 = S4.step_services(a, ctx), S4.step_link(a, ctx)
            put("S5-01", S4.agg([r1["verdict"], r2["verdict"]]), "" if r1["verdict"] == r2["verdict"] == "PASS"
                else f"службы {r1['verdict']}, связь {r2['verdict']}", {"services": r1, "link": r2})
        if sid is None:
            v = BLOCKED if why.startswith(HW_UNAVAILABLE) else "ERROR"
            for k in ("S5-11", "S5-12", "S5-13", "S5-14"):
                if k in only:
                    put(k, v, why)
        else:
            if "S5-11" in only:
                changed = True
                if a.gui:
                    v, d, x = step_select_gui(a, stand)
                    ctx["console_restarted"] = True
                    put("S5-11", v, d, x)
                else:
                    put("S5-11", "ERROR", "без --gui визуального доказательства нет (ui-state не доказательство)")
            if {"S5-12", "S5-13"} & only:
                changed = True
                sw = stand.switch("v4l2")
                if not sw["ok"]:
                    for k in ("S5-12", "S5-13"):
                        put(k, "FAIL", f"источник {sid} не дошёл до пульта за {S4.LIMITS['switch_s']} с", sw)
                else:
                    v12, v13, d, ms = step_faults(a, stand)
                    (a.out / "faults.json").write_text(json.dumps(ms, ensure_ascii=False, indent=1))
                    if a.gui_fault:
                        gv, gd, gx = step_gui_fault(a, stand, a.manual_fault)
                        ctx["console_restarted"] = True
                        (a.out / "gui-fault-judgement.json").write_text(json.dumps(
                            {"verdict": gv, "detail": gd, **gx}, ensure_ascii=False, indent=1))
                        if gv != "PASS" and v12 == "PASS":
                            v12, d = gv, f"{d}; рендеринг: {gd}".strip("; ")
                        elif gd:
                            d = f"{d}; рендеринг: {gd}".strip("; ")
                    put("S5-12", v12, d, {"switch": sw, "faults": ms})
                    put("S5-13", v13, d)
            if "S5-14" in only:
                changed = True
                v, p, x = step_switch(a, stand)
                put("S5-14", v, "; ".join(p), x)
    finally:
        if changed:
            restore_config(a.remote, stand.orig_conf)
        stand.ui.stop()
        stand.rs.stop()
    if "S5-15" in only:
        j = S4.step_journal(a, ctx)
        after = snap_all(a, "after")
        ota = {}
        for board in ("pi1", "pi2"):
            v, bad, checks = judge_ota(board, before[board], after[board], ctx.get("console_restarted", False))
            (a.out / f"{board}-compare.txt").write_text("\n".join(f"{c['status']} {c['id']} — {c['detail']}"
                                                                 for c in checks) + f"\n== {v}\n")
            ota[board] = {"verdict": v, "unexpected": bad}
        v = S4.agg([j["verdict"], *(o["verdict"] for o in ota.values())])
        put("S5-15", v, "" if v == "PASS" else f"журналы {j['verdict']}; OTA " +
            ", ".join(f"{b} {o['verdict']} {o['unexpected']}" for b, o in ota.items()), {"journal": j, "ota": ota})
    for k in STEPS:
        results.setdefault(k, {"verdict": "SKIP", "detail": "не запрошен (--only)"})
    summary = {**meta, "duration_min": round((time.monotonic() - t0) / 60, 1), "results": results,
               "verdict": verdict_of(results)}
    (a.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    log(f"ИТОГ {summary['verdict']} за {summary['duration_min']} мин: " +
        ", ".join(f"{k} {v['verdict']}" for k, v in results.items()))
    return {"PASS": 0, "FAIL": 1, "ERROR": 2, BLOCKED: 3, "SKIP": 2}[summary["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
