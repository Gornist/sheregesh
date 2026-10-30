#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Короткая функциональная приёмка pi2 после OTA на лабораторный образ bd74c8c (слот B), 5–10 мин без учёта загрузки
(docs/acceptance/s5-ota/ACCEPT-PI2.md). С машины владельца по SSH от sheregesh-ui. Две фазы:

  python3 tools/probe/s5_pi2_accept.py --plan [--phase trial|committed]                 # только план, без SSH
  python3 tools/probe/s5_pi2_accept.py --phase trial --out DIR [--gui] [--only P2-01,…]   # пробная загрузка, до confirm
  python3 tools/probe/s5_pi2_accept.py --phase committed --out DIR2 --baseline DIR      # после confirm

  P2-01  загрузка с обновлённого слота: слот B, OTA (trial: TRYBOOT/HEALTH_PENDING, tryboot; committed: COMMITTED),
         образ bd74c8c (/usr/share/sheregesh/build.json) и его ota_version, код приложения — заводской (не DEV-выпуск),
         uptime, failed-юниты 0, sheregesh-health PASS
  P2-02  SSH, связь с pi1: check_link --only health,identity, TLS 1.3 управления, видео tls1.3-mutual,
         check_video_link --only session (s4_accept.step_link)
  P2-03  synthetic до GUI pi1: выбор GUI-путём (videoselect через работающий пульт = Backend.selectVideoSource),
         live и кадры растут по ui-state; --gui — снимок gui_capture --by select
  P2-04  RTSP shcam (rtsp:1 «shcam Pi5 IMX477», H.264 1280×720@30): выбор, live (и снимок при --gui)
  P2-05  эмулятор п. 7 (tools/lab-video/pi2): start sheregesh-lab-usbip@hdmi → 534d:2109 в списке как hdmi, выбор, live;
         stop («выдернуть») → no-device на блоке и «нет устройства» на пульте; start → возврат; enable отклонён polkit;
         vivid: start sheregesh-lab-vivid sheregesh-lab-vivid-feed@cvbs, lab-vivid cvbs nosignal → «нет сигнала»,
         ok → возврат; затем блок на synthetic и только потом stop vivid (README п. 5)
  P2-06  S5-D1: выбор GUI-путём сохраняется в video-input.json (rtsp_urls целы) и переживает
         trial: systemctl restart sheregesh-remote-net (polkit 50-…: любые действия с этим юнитом);
         committed: systemctl reboot (polkit 50-…: login1.reboot) — с ожиданием возврата SSH и таймаутом
  P2-07  (committed) COMMITTED, слот B; slot_guard «после» против снимка «до confirm» фазы trial — только ожидаемые
         отличия фиксации; pi1 не затронут (OTA B/COMMITTED, версия и boot_id прежние)

Перезагрузка в фазе trial не выполняется никогда: обычная перезагрузка пробной загрузки вернёт основной слот A.
В фазе committed она выполняется, только если агент OTA сообщает COMMITTED, слот B и autoboot [all] = 3.
Исходный video-input.json (S5-D1 теперь сохраняет выбор!) возвращается явной записью + SIGHUP в finally; затем
сверяется активный источник. Вердикты PASS | FAIL | ERROR | BLOCKED | SKIP; итог FAIL > ERROR > BLOCKED > PASS > SKIP.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/probe"))
sys.path.insert(0, str(ROOT / "tools/deploy/ota-check"))
import s4_accept as S4  # noqa: E402
import s5_accept as S5  # noqa: E402

E = S4.E
STEPS = ("P2-01", "P2-02", "P2-03", "P2-04", "P2-05", "P2-06", "P2-07")
# P2-05 (лабораторный эмулятор) — после confirm: упавший лабораторный юнит на пробном слоте сорвал бы гейт failed-units
# confirm, а сбросить failed polkit не даёт — остался бы только abort (решение главного агента S5-OTA).
PHASE_STEPS = {"trial": ("P2-01", "P2-02", "P2-03", "P2-04", "P2-06"), "committed": ("P2-01", "P2-05", "P2-06", "P2-07")}
TRIAL_STATES = ("TRYBOOT", "HEALTH_PENDING")
BLOCKED = S5.BLOCKED
EXPECT_COMMIT, EXPECT_VERSION = "bd74c8c", "0.1.1792949168"      # build-report.json образа (source_date_epoch)
BOOT_PART_B = 3
USBIP = "sheregesh-lab-usbip@hdmi"
VIVID, VIVID_FEED = "sheregesh-lab-vivid", "sheregesh-lab-vivid-feed@cvbs"
LAB_VIVID = "/usr/lib/sheregesh/lab-video/lab-vivid"
LIMITS = {"select_s": 25.0, "device_s": 30.0, "restart_s": 60.0, "boot_s": 300.0, "ready_s": 180.0}
LIVE_WINDOW_S, MIN_FRAMES = 3.0, 20          # кадры должны расти: ≥ 20 декодированных за окно (≈ 30 к/с × 3 с)
DENIED = ("access denied", "interactive authentication required", "not authorized", "authentication is required")
REL = "C=$(readlink -f /var/lib/sheregesh-deploy/current 2>/dev/null || echo /usr/lib/sheregesh)"
LOG = None


def log(msg):
    line = time.strftime("%H:%M:%S") + " " + msg
    print(line, flush=True)
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------------------------------------------------------------- чистые функции (тесты: test_s5_pi2_accept.py)


def verdict_of(results):
    return S5.verdict_of(results)


def agg(vs):
    """Как s4_accept.agg, но BLOCKED не теряется: FAIL > ERROR > BLOCKED > PASS."""
    vs = [v for v in vs if v != "SKIP"]
    for v in ("FAIL", "ERROR", BLOCKED, "PASS"):
        if v in vs:
            return v
    return "ERROR"


def ota_summary(d):
    """Ответ sheregesh-ota-client status → плоское состояние; None — агент недоступен (ERROR, не PASS)."""
    if not isinstance(d, dict) or d.get("result") != "OK" or not isinstance(d.get("state"), dict):
        return None
    st, sl, ab, b = d["state"], d.get("slots") or {}, d.get("autoboot") or {}, d.get("build") or {}
    return {"state": st.get("state"), "committed": st.get("active"), "target": st.get("target"),
            "version_active": st.get("version_active"), "version_target": st.get("version_target"),
            "slot": sl.get("active"), "tryboot_now": sl.get("tryboot_now"), "consistent": d.get("consistent"),
            "default_part": ab.get("default_part"), "commit": b.get("commit"), "ota_version": b.get("ota_version"),
            "switching": d.get("switching")}


def ota_problems(o, phase, version=EXPECT_VERSION):
    """Расхождения состояния OTA pi2 с фазой. trial — пробная загрузка B; committed — B основной."""
    if o is None:
        return ["агент OTA недоступен (sheregesh-ota-client status)"]
    p = []
    if o["slot"] != "B":
        p.append(f"загружен слот {o['slot']!r}, ожидался B")
    if o["consistent"] is not True:
        p.append(f"состояние несогласовано (consistent={o['consistent']!r})")
    if phase == "trial":
        if o["state"] not in TRIAL_STATES:
            p.append(f"состояние {o['state']!r}, ожидалось {'/'.join(TRIAL_STATES)} (до confirm)")
        if o["target"] != "B" or o["tryboot_now"] is not True:
            p.append(f"не пробная загрузка B: target={o['target']!r}, tryboot_now={o['tryboot_now']!r}")
        if version and o["version_target"] != version:
            p.append(f"version_target {o['version_target']!r} ≠ {version}")
    else:
        if o["state"] != "COMMITTED" or o["committed"] != "B":
            p.append(f"состояние {o['state']!r}/{o['committed']!r}, ожидалось COMMITTED/B")
        if o["default_part"] != BOOT_PART_B:
            p.append(f"autoboot [all] = {o['default_part']!r}, ожидался {BOOT_PART_B} (boot_b)")
        if version and o["version_active"] != version:
            p.append(f"version_active {o['version_active']!r} ≠ {version}")
    return p


def reboot_allowed(o):
    """Перезагрузка pi2 допустима, только когда основной слот — B (иначе прошивка вернёт A и отменит пробу)."""
    p = ota_problems(o, "committed", version=None)
    return (not p), "; ".join(p) or "COMMITTED, слот B, autoboot [all]=3"


def judge_boot(ident, phase, commit=EXPECT_COMMIT, version=EXPECT_VERSION, allow_dev_release=False):
    """P2-01 → (вердикт, подробности, critical). critical — плата не в ожидаемом слоте/состоянии: активные шаги не
    выполняются (BLOCKED), чтобы не воздействовать на непроверенную загрузку."""
    o, b, h = ident.get("ota"), ident.get("build") or {}, ident.get("health") or {}
    if o is None or h.get("verdict") is None:
        return "ERROR", "нет ответа агента OTA или sheregesh-health", True
    slot = ota_problems(o, phase, version)
    p = list(slot)
    if not str(b.get("commit", "")).startswith(commit):
        p.append(f"образ {str(b.get('commit'))[:12]!r}, ожидался {commit}")
    if version and b.get("ota_version") != version:
        p.append(f"ota_version образа {b.get('ota_version')!r} ≠ {version}")
    dep = ident.get("deploy_current") or "factory"
    if dep != "factory" and not allow_dev_release:
        p.append(f"код приложения из DEV-выпуска {dep}, а не из образа (--allow-dev-release, если так задумано)")
    if (ident.get("failed") or "").strip():
        p.append(f"failed-юниты: {ident['failed'].strip()}")
    if h.get("verdict") != "PASS":
        p.append(f"sheregesh-health {h.get('verdict')}: {[c.get('id') for c in h.get('not_pass', [])]}")
    up = ident.get("uptime_s")
    detail = "; ".join(p) or (f"B {o['state']}, образ {str(b.get('commit'))[:12]} {b.get('ota_version')}, "
                              f"uptime {up} с, failed 0, health PASS")
    return ("FAIL" if p else "PASS"), detail, bool(slot) or not str(b.get("commit", "")).startswith(commit)


def kind_of(sid, sources):
    """Ожидаемый вход (ui-state videoSource.input / status.video.source.input) для id источника."""
    ent = next((s for s in sources or [] if s.get("id") == sid), None)
    if ent and ent.get("input"):
        return str(ent["input"]).lower()
    return "synthetic" if sid == "synthetic" else "rtsp" if sid.startswith("rtsp:") else "v4l2"


def judge_live(docs, kind, min_frames=MIN_FRAMES):
    """По выборкам ui-state пульта за окно: источник kind live, счётчик декодированных кадров растёт, fps > 0."""
    ok = [d for d in docs if S4.console_live(d, kind)]
    if len(ok) < 2:
        seen = sorted({str((d.get("videoSource") or {}).get("input")) + "/" +
                       str((d.get("videoSource") or {}).get("display")) for d in docs})
        return False, f"нет живого потока {kind} в выборке ({len(docs)} строк, видели {seen})"
    f0, f1 = ((ok[i].get("video") or {}).get("frames_decoded") or 0 for i in (0, -1))
    fps = (ok[-1].get("video") or {}).get("decoded_fps_5s") or 0
    if f1 - f0 < min_frames or fps <= 0:
        return False, f"кадры не растут: {f0} → {f1} (нужно ≥ {min_frames}), {fps} к/с"
    return True, f"{kind} live, кадров +{f1 - f0} за окно, {fps} к/с"


def rtsp_url(conf, n):
    items = conf.get("rtsp_urls") if isinstance(conf, dict) else None
    if not isinstance(items, list) or not 0 <= n < len(items):
        return None
    it = items[n]
    return it if isinstance(it, str) else it.get("url") if isinstance(it, dict) else None


def judge_saved(text, sid, orig_text):
    """S5-D1: video-input.json после выбора описывает sid, прочие ключи (rtsp_urls) не потеряны."""
    try:
        cur = json.loads(text)
    except (TypeError, ValueError):
        return False, f"video-input.json не JSON: {str(text)[:80]!r}"
    try:
        orig = json.loads(orig_text) if orig_text and orig_text.strip() != "__ABSENT__" else {}
    except ValueError:
        orig = {}
    if not isinstance(cur, dict):
        return False, "video-input.json не объект"
    src = str(cur.get("source", "")).lower()
    p = []
    if sid == "synthetic":
        if src != "synthetic":
            p.append(f"source={src!r}, ожидался synthetic")
    elif sid.startswith("rtsp:"):
        want = rtsp_url(cur, int(sid.split(":", 1)[1])) if sid.split(":", 1)[1].isdigit() else None
        if src != "rtsp" or not want or cur.get("url") != want:
            p.append(f"source={src!r} url={cur.get('url')!r}, ожидался rtsp {want!r}")
    elif sid.startswith("v4l2:"):
        if src not in ("v4l2", "uvc") or str(cur.get("device", "")) != sid[5:]:
            p.append(f"source={src!r} device={cur.get('device')!r}, ожидался {sid}")
    if isinstance(orig, dict) and "rtsp_urls" in orig and cur.get("rtsp_urls") != orig["rtsp_urls"]:
        p.append("rtsp_urls изменились")
    return (not p), "; ".join(p) or f"описание {sid}, rtsp_urls целы"


def persist_target(active, rtsp_id):
    """Источник для S5-D1: отличный от текущего, чтобы сохранение было видно в файле."""
    return rtsp_id if active != rtsp_id else "synthetic"


def judge_persist(m):
    """P2-06 по измерению: выбор сохранён, после перезапуска/перезагрузки тот же источник активен и показывается,
    файл после старта прежний (блок его не перезаписал другим источником)."""
    p = []
    if not m.get("selected"):
        return "ERROR", f"выбор {m.get('want')} не выполнен: {m.get('select_detail')}"
    if not m.get("saved_ok"):
        p.append(f"выбор не сохранён: {m.get('saved_detail')}")
    if m.get("event_ok") is False:
        return "ERROR", f"{m.get('event')}: {m.get('event_detail')}"
    if m.get("ready_s") is None:
        p.append(f"после {m.get('event')} источник {m.get('want')} не показан на пульте за {m.get('timeout_s')} с")
    if m.get("active_after") != [m.get("want")]:
        p.append(f"после {m.get('event')} активен {m.get('active_after')}, ожидался {m.get('want')}")
    if not m.get("saved_after_ok"):
        p.append(f"файл после {m.get('event')}: {m.get('saved_after_detail')}")
    return ("FAIL" if p else "PASS"), "; ".join(p) or \
        f"{m.get('want')} сохранён и активен после {m.get('event')} за {m.get('ready_s')} с"


def judge_outage(m, fault_state):
    """Отказ эмулятора: «нет видео» ≤ 3 с и состояние fault_state на блоке (s4 judge_fault), пульт показал
    display=fault_state; возврат ≤ 10 с."""
    if m.get("error"):
        return "ERROR", m["error"]
    v1, v2, p = S4.judge_fault(m, (fault_state,))
    if m.get("console_fault_s") is None:
        p.append(f"пульт не показал «{fault_state}» (videoSource.display)")
        v1 = "FAIL"
    return agg([v1, v2]), "; ".join(p) or (f"«нет видео» {m.get('off_s')} с, {fault_state} на пульте "
                                           f"{m.get('console_fault_s')} с, возврат {m.get('back_s')} с")


def judge_enable_denied(rc, text, enabled):
    """systemctl enable лабораторного юнита от sheregesh-ui: обязан отказать polkit (manage-unit-files не разрешён)."""
    low = (text or "").lower()
    if rc == 0 or enabled == "enabled":
        return "FAIL", f"enable прошёл (rc {rc}, is-enabled {enabled!r}): polkit разрешает больше start/stop/restart"
    if any(d in low for d in DENIED):
        return "PASS", f"отказ polkit (rc {rc}): {text.strip()[:120]}"
    if "read-only" in low:
        return "FAIL", f"polkit пропустил enable, отказала только файловая система: {text.strip()[:120]}"
    return "ERROR", f"enable не прошёл, но причина не polkit: rc {rc}: {text.strip()[:120]}"


def pick_vivid(before, after, want="auto"):
    """id входа CVBS vivid в status.video.sources: --vivid-id или единственный новый доступный v4l2 с input cvbs."""
    if want != "auto":
        return S5.resolve_source(after, want)
    old = {s.get("id") for s in before or []}
    cvbs = [s for s in after or [] if str(s.get("id", "")).startswith("v4l2:") and s.get("available")
            and str(s.get("input", "")).lower() == "cvbs" and not str(s.get("id")).startswith("v4l2:534d:")]
    new = [s for s in cvbs if s["id"] not in old] or cvbs
    if len(new) == 1:
        return new[0]["id"], None
    return None, (f"{S5.HW_UNAVAILABLE}: входа CVBS vivid нет в списке блока" if not new else
                  f"входов CVBS несколько {[s['id'] for s in new]} — укажите --vivid-id")


def judge_commit(before, after, version=EXPECT_VERSION):
    """P2-07: slot_guard «до confirm» (пробная загрузка B) против «после» (COMMITTED, обычно после перезагрузки).
    База — compare(..., 'unchanged'): FAIL сравнения «до» с самим собой (свойства состояния: слот B, разрешение OTA)
    допустимы с той же деталью. Ожидаемые отличия фиксации: autoboot.txt ([all]=3) и его запись в bootconfig (p1),
    состояние агента, отзыв разрешения (ota-disable). При перезагрузке ещё boot_id, dt.tryboot=0, InvocationID юнитов,
    счётчики ввода-вывода (сброшены) — вместо них: слот A с начала загрузки не писался."""
    import slot_guard as G
    _v0, base = G.compare(before, before, "unchanged", "?")
    v, checks = G.compare(before, after, "unchanged", "?")
    if v == "ERROR" or any(c["status"] == "ERROR" for c in base + checks):
        return "ERROR", [f"{c['id']}: {c['detail']}" for c in base + checks if c["status"] == "ERROR"], checks
    b, a = before["fields"], after["fields"]
    rebooted = b["boot_id"] != a["boot_id"]
    allowed = {c["id"]: c["detail"] for c in base if c["status"] == "FAIL"}
    bad = []
    for c in checks:
        cid, det = c["id"], c["detail"]
        if c["status"] != "FAIL" or allowed.get(cid) == det:
            continue
        if cid in ("same.autoboot.sha256", "same.ota.status", "untouched.p1.bootconfig"):
            continue
        if cid == "bootconfig.autoboot" and G.autoboot_parts(a["autoboot"]).get("all") == str(BOOT_PART_B):
            continue
        if cid == "same.ota.enable_present" and (b["ota.enable_present"], a["ota.enable_present"]) == ("YES", "no"):
            continue
        if rebooted and (cid == "same.boot_id" or cid.startswith("untouched.")
                         or (cid == "same.dt.tryboot" and a["dt.tryboot"] == "0")):
            continue
        if rebooted and cid.startswith("same.unit."):
            s = dict(x.split("=", 1) for x in a[cid[5:]].strip(";").split(";") if "=" in x)
            if s.get("ActiveState") == "active" and s.get("NRestarts", "0") == "0":
                continue
        bad.append(f"{cid}: {det}")
    if rebooted:
        for p in G.SLOT_PARTS["A"]:
            w, d = (int(x) for x in a[f"io.{p}"].split())
            ok = w == 0 and d == 0
            checks.append({"id": f"untouched_since_boot.{p}.{G.LAYOUT[p]}", "status": "PASS" if ok else "FAIL",
                           "detail": f"записано {w}, отброшено {d}"})
            if not ok:
                bad.append(f"слот A ({G.LAYOUT[p]}) писался с начала загрузки: {w}/{d}")
    o = ota_summary(json.loads(a["ota.status"]))
    p = ota_problems(o, "committed", version)
    if rebooted and o and o["tryboot_now"] is not False:
        p.append(f"после перезагрузки tryboot_now={o['tryboot_now']!r}")
    bad += [f"ota: {x}" for x in p]
    return ("FAIL" if bad else "PASS"), bad, checks


def judge_pi1(base, now):
    """pi1 не затронут: OTA COMMITTED/B, версия и согласованность прежние, та же загрузка (boot_id)."""
    if not base or not now or base.get("ota") is None or now.get("ota") is None:
        return "ERROR", ["нет состояния OTA pi1 «до» или «после»"]
    p = []
    bo, no = base["ota"], now["ota"]
    if bo["state"] != "COMMITTED" or bo["slot"] != "B":
        p.append(f"базовая линия pi1 не B/COMMITTED: {bo['slot']}/{bo['state']}")
    for k in ("state", "committed", "slot", "version_active", "consistent", "commit"):
        if bo.get(k) != no.get(k):
            p.append(f"pi1 {k}: {bo.get(k)!r} → {no.get(k)!r}")
    if base.get("boot_id") != now.get("boot_id"):
        p.append("pi1 перезагружался за приёмку (boot_id сменился)")
    return ("FAIL" if p else "PASS"), p


def judge_gui_pick(states, sid, kind):
    """Снимок gui_capture --by select для одного id: вход kind, кадр за 1 с сменился."""
    err = next((s for s in states if s.get("select_error")), None)
    tag = sid.replace(":", "_")
    shot = next((s for s in states if str(s.get("shot", "")).endswith(f"-selected-{tag}-video-t+1s.png")), None)
    if shot is None:
        if err:
            return "FAIL", f"блок отклонил выбор: {err.get('select_error')}"
        return ("ERROR" if not states else "FAIL"), f"снимка {sid} нет (states: {len(states)})"
    inp = str((shot.get("videoSource") or {}).get("input", "")).lower()
    p = []
    if inp != kind:
        p.append(f"на снимке вход {inp!r}, ожидался {kind!r}")
    if shot.get("frame_changed") is not True:
        p.append(f"кадр за 1 с не сменился ({shot.get('frame_changed')!r})")
    return ("FAIL" if p else "PASS"), "; ".join(p) or f"снимок {shot['shot']}: {kind}, кадр меняется"


def wait_reboot(host, boot0, timeout, ssh=None, clock=time.monotonic, sleep=time.sleep):
    """Ожидание перезагрузки: SSH пропал (или boot_id сменился) и вернулся с новым boot_id.
    → {"down_s", "up_s", "boot_id"}; up_s None — не вернулся за timeout."""
    ssh = ssh or (lambda h, c, t: E.ssh(h, c, t))
    t0, down, out = clock(), None, {"down_s": None, "up_s": None, "boot_id": None}
    while clock() - t0 < timeout:
        code, text = ssh(host, "cat /proc/sys/kernel/random/boot_id", 10)
        now = round(clock() - t0, 1)
        if code != 0:
            down = down if down is not None else now
        elif text.strip() and text.strip() != boot0:
            out.update(down_s=down if down is not None else now, up_s=now, boot_id=text.strip())
            return out
        sleep(2)
    out["down_s"] = down
    return out


# ---------------------------------------------------------------- стенд


def ssh(host, cmd, timeout=60):
    return E.ssh(host, cmd, timeout)


def videoselect(a, args, timeout=60):
    """sheregesh.link.videoselect на pi1 через работающий пульт (Backend.selectVideoSource, как кнопка «Видео»)."""
    code, text = ssh(a.console, f"{REL}; PYTHONPATH=$C/software python3 -m sheregesh.link.videoselect {args} "
                                f"--via console 2>&1", timeout)
    i = text.find("{")
    return code, E.jload(text[i:] if i >= 0 else ""), text


class Stand(S4.Stand):
    """Выборки ui-state pi1 / link-status pi2 (s4_accept), выбор GUI-путём, эмулятор п. 7 и перезапуски."""

    def __init__(self, a):
        super().__init__(a)
        self.changed = False

    def sources(self):
        code, d, text = videoselect(self.a, "--list", 40)
        if code == 0 and d.get("sources"):
            return d.get("sources"), d.get("active") or [], d
        st = E.jload(E.out(self.a.remote, f"cat {S4.REMOTE_STATUS}"))
        src = (st.get("video") or {}).get("sources") or []
        return src, [s["id"] for s in src if s.get("active")], {"fallback": "link-status", "videoselect": text[-300:]}

    def select(self, sid, wait=20):
        self.changed = True
        code, d, text = videoselect(self.a, f"{sid} --wait {wait:g}", wait + 40)
        return code == 0 and d.get("result") == "ok", d.get("result") or text[-300:]

    def conf(self):
        return E.out(self.a.remote, f"cat {S4.VCONF} 2>/dev/null || echo __ABSENT__")

    def pick(self, sid, kind, timeout=LIMITS["select_s"]):
        """Выбор GUI-путём → блок streaming kind, пульт live kind, затем окно LIVE_WINDOW_S: кадры растут."""
        t0 = time.monotonic()
        ok, why = self.select(sid)
        m = {"id": sid, "kind": kind, "selected": ok, "select_detail": why}
        if not ok:
            return m
        m["remote_streaming_s"] = self.wait(self.rs, lambda d: S4.remote_is(d, kind), t0, timeout)
        m["console_live_s"] = self.wait(self.ui, lambda d: S4.console_live(d, kind), t0, timeout)
        t1 = time.monotonic()
        time.sleep(LIVE_WINDOW_S)
        m["live_ok"], m["live_detail"] = judge_live([d for _m, _b, d in self.ui.since(t1)], kind)
        return m

    def outage(self, inject, restore, fault_state, kind, hold=6.0):
        """Отказ эмулятора командой на pi2 (systemctl / lab-vivid), удержание hold с, снятие; замеры как s4 fault."""
        m = {"inject": inject, "restore": restore, "fault_state": fault_state, "outage_s": hold}
        t0 = time.monotonic()
        code, text = ssh(self.a.remote, inject, 60)
        if code != 0:
            return {**m, "error": f"отказ не внесён: rc {code} {text[-200:]}"}
        m["off_s"] = self.wait(self.ui, lambda d: not S4.console_live(d), t0, 10)
        m["console_fault_s"] = self.wait(self.ui, lambda d: (d.get("videoSource") or {}).get("display") == fault_state,
                                         t0, 12)
        time.sleep(max(0.0, hold - (time.monotonic() - t0)))
        states = {S4.remote_source(d).get("state") for _m, _b, d in self.rs.since(t0)}
        m["remote_states_seen"] = sorted(s for s in states if s)
        m["remote_fault_state"] = fault_state if fault_state in states else next(
            (s for s in ("no-signal", "no-device", "error") if s in states), None)
        t1 = time.monotonic()
        code, text = ssh(self.a.remote, restore, 60)
        m["restore_ok"] = code == 0
        if code != 0:
            m["restore_error"] = text[-200:]
        m["back_s"] = self.wait(self.ui, lambda d: S4.console_live(d, kind), t1, 30)
        m["remote_back_s"] = self.wait(self.rs, lambda d: S4.remote_is(d, kind), t1, 30)
        return m

    def sessions(self):
        r = self.ui.last()
        return ((r[2] if r else {}).get("video") or {}).get("sessions") or 0

    def ready_after(self, kind, sessions0, t0, timeout):
        """Новый видеосеанс (счётчик sessions пульта вырос) и live kind: устаревшие строки ui-state не в счёт."""
        return self.wait(self.ui, lambda d: S4.console_live(d, kind) and
                         ((d.get("video") or {}).get("sessions") or 0) > sessions0, t0, timeout)

    def restore(self, orig, orig_active):
        """Исходный video-input.json явной записью + SIGHUP (s5_accept.restore_config), затем сверка активного."""
        S5.restore_config(self.a.remote, orig)
        time.sleep(4)
        _src, active, _d = self.sources()
        return {"file_restored": self.conf().strip() == (orig or "").strip(),
                "active_before": orig_active, "active_after": active,
                "active_restored": orig_active is None or active == orig_active}


# ---------------------------------------------------------------- шаги


def identity(a):
    ident = E.board_identity(a.remote)
    ident["ota_full"] = E.jload(E.out(a.remote, "sheregesh-ota-client status"))
    ident["ota"] = ota_summary(ident["ota_full"])
    ident["failed"] = E.out(a.remote, "systemctl --failed --no-legend --plain")
    ident["health"] = E.health(a.remote, "remote")
    return ident


def pi1_state(a):
    return {"ota": ota_summary(E.jload(E.out(a.console, "sheregesh-ota-client status"))),
            "boot_id": E.out(a.console, "cat /proc/sys/kernel/random/boot_id")}


def step_boot(a, phase):
    ident = identity(a)
    v, d, critical = judge_boot(ident, phase, a.expect_commit, a.expect_version, a.allow_dev_release)
    return v, d, critical, {k: ident.get(k) for k in ("serial", "build", "ota", "failed", "health", "uptime_s",
                                                       "deploy_current", "kernel", "package")}


def step_link(a, ctx):
    r = S4.step_link(a, ctx)
    code, who = ssh(a.remote, "id -un; hostname")
    code2, text = S4.run_tool([ROOT / "tools/probe/check_video_link.py", "--console", a.console, "--remote", a.remote,
                               "--out", a.out / "check_video_link", "--only", "session"])
    sf = a.out / "check_video_link" / "summary.json"
    cvl = (json.loads(sf.read_text()).get("results") if sf.exists() else None)
    fails, errors = [], []
    if code != 0 or not who.startswith("sheregesh-ui"):
        fails.append(f"SSH: rc {code} {who!r}")
    if r["verdict"] == "ERROR":
        errors.append(f"связь: {r.get('reason')}")
    elif r["verdict"] != "PASS":
        fails.append(f"связь {r['verdict']}: peerLink {r.get('peerLink')}, linkTls {r.get('linkTls')}, "
                     f"видео {r.get('video_transport')}, check_link {r.get('check_link')}")
    if cvl is None:
        errors.append("check_video_link без summary.json")
    elif any(v != "PASS" for v in cvl.values()):
        fails.append(f"check_video_link session {cvl}")
    v = "FAIL" if fails else "ERROR" if errors else "PASS"
    ok = f"SSH {who.splitlines()[0] if who else '?'}, mTLS TLSv1.3, видео tls1.3-mutual, session PASS"
    return v, "; ".join(fails + errors) or ok, {"link": r, "check_video_link": cvl}


def judge_pick(m):
    if not m.get("selected"):
        return "FAIL", f"выбор {m['id']} отклонён/не выполнен: {m.get('select_detail')}"
    p = []
    if m.get("remote_streaming_s") is None:
        p.append(f"блок не перешёл на {m['kind']} (streaming) за {LIMITS['select_s']} с")
    if m.get("console_live_s") is None:
        p.append(f"пульт не показал {m['kind']} live за {LIMITS['select_s']} с")
    if not m.get("live_ok"):
        p.append(m.get("live_detail", ""))
    return ("FAIL" if p else "PASS"), "; ".join(p) or \
        f"{m['id']}: блок {m['remote_streaming_s']} с, пульт {m['console_live_s']} с; {m['live_detail']}"


def step_usbip(a, stand, srcs0):
    """P2-05 (USB/IP): вставить, выбрать, выдернуть/вернуть, enable отклонён → (вердикты, подробности, замеры)."""
    r, notes, vs = {}, [], []
    code, text = ssh(a.remote, f"systemctl cat {USBIP}.service >/dev/null 2>&1 && echo yes || echo no")
    if text.strip() != "yes":
        return [BLOCKED], [f"{S5.HW_UNAVAILABLE}: юнита {USBIP} нет в образе"], r
    t0 = time.monotonic()
    code, text = ssh(a.remote, f"systemctl --no-ask-password start {USBIP}", 90)
    r["start"] = {"rc": code, "out": text[-300:]}
    if code != 0:
        return ["FAIL"], [f"start {USBIP} от sheregesh-ui: rc {code} {text[-200:]}"], r
    sid, why = None, None
    end = time.monotonic() + LIMITS["device_s"]
    while time.monotonic() < end:
        srcs, _act, _d = stand.sources()
        sid, why = S5.resolve_source(srcs, a.hdmi_id)
        if sid:
            break
        time.sleep(2)
    r["appear_s"] = round(time.monotonic() - t0, 1) if sid else None
    r["sources"] = srcs
    if not sid:
        return ["FAIL"], [f"{a.hdmi_id} не появился в status.video.sources за {LIMITS['device_s']} с: {why}"], r
    kind = kind_of(sid, srcs)
    if kind != "hdmi":
        vs.append("FAIL")
        notes.append(f"{sid} классифицирован как {kind!r}, ожидался hdmi")
    m = stand.pick(sid, kind)
    r["pick"] = m
    v, d = judge_pick(m)
    vs.append(v)
    notes.append(f"HDMI {d}")
    if v == "PASS":
        o = stand.outage(f"systemctl --no-ask-password stop {USBIP}", f"systemctl --no-ask-password start {USBIP}",
                         "no-device", kind, hold=a.outage)
        r["unplug"] = o
        v, d = judge_outage(o, "no-device")
        vs.append(v)
        notes.append(f"выдернуть/вставить: {d}")
    rc, text = ssh(a.remote, f"systemctl --no-ask-password enable {USBIP}.service 2>&1", 60)
    _c, enabled = ssh(a.remote, f"systemctl is-enabled {USBIP}.service 2>&1 || true")
    v, d = judge_enable_denied(rc, text, enabled.strip())
    r["enable"] = {"rc": rc, "out": text[-400:], "is_enabled": enabled.strip()}
    vs.append(v)
    notes.append(f"enable: {d}")
    return vs, notes, r


def step_vivid(a, stand, srcs0):
    r, notes, vs = {}, [], []
    code, text = ssh(a.remote, f"systemctl cat {VIVID}.service {VIVID_FEED}.service >/dev/null 2>&1 && echo yes || "
                               "echo no")
    if text.strip() != "yes":
        return [BLOCKED], [f"{S5.HW_UNAVAILABLE}: юнитов vivid нет в образе"], r
    t0 = time.monotonic()
    code, text = ssh(a.remote, f"systemctl --no-ask-password start {VIVID} {VIVID_FEED}", 120)
    r["start"] = {"rc": code, "out": text[-300:]}
    if code != 0:
        return ["FAIL"], [f"start vivid: rc {code} {text[-200:]}"], r
    sid, why = None, None
    end = time.monotonic() + LIMITS["device_s"]
    while time.monotonic() < end:
        srcs, _act, _d = stand.sources()
        sid, why = pick_vivid(srcs0, srcs, a.vivid_id)
        if sid:
            break
        time.sleep(2)
    r["appear_s"] = round(time.monotonic() - t0, 1) if sid else None
    r["status"] = E.out(a.remote, f"{LAB_VIVID} status 2>&1 | tail -20")
    if not sid:
        return ["FAIL"], [f"вход CVBS vivid не появился за {LIMITS['device_s']} с: {why}"], r
    m = stand.pick(sid, "cvbs", timeout=LIMITS["device_s"])
    r["pick"] = m
    v, d = judge_pick(m)
    vs.append(v)
    notes.append(f"vivid {d}")
    if v == "PASS":
        o = stand.outage(f"{LAB_VIVID} cvbs nosignal", f"{LAB_VIVID} cvbs ok", "no-signal", "cvbs", hold=a.outage)
        r["nosignal"] = o
        v, d = judge_outage(o, "no-signal")
        vs.append(v)
        notes.append(f"нет сигнала/возврат: {d}")
    return vs, notes, r


def lab_cleanup(a, stand):
    """Блок на synthetic (узлы vivid и USB-устройство отпущены), затем stop юнитов п. 7; failed после — проверка."""
    m = stand.pick("synthetic", "synthetic")
    if not (m.get("selected") and m.get("remote_streaming_s") is not None):     # запасной путь: файл + SIGHUP
        S5.write_config(a.remote, S5.merged_config(stand.conf(), {"source": "synthetic"}))
        m["fallback_file"] = True
        m["remote_streaming_s"] = stand.wait(stand.rs, lambda d: S4.remote_is(d, "synthetic"), time.monotonic(), 20)
    out = {"synthetic": {k: m.get(k) for k in ("selected", "remote_streaming_s", "console_live_s", "live_ok",
                                               "fallback_file")}}
    for unit in (USBIP, VIVID_FEED, VIVID):
        code, text = ssh(a.remote, f"systemctl --no-ask-password stop {unit}", 90)
        out[unit] = {"rc": code, "out": text[-200:]}
    out["state"] = E.out(a.remote, f"systemctl show -p ActiveState -p Result {USBIP} {VIVID_FEED} {VIVID} | "
                                   "paste -sd' '")
    out["failed"] = E.out(a.remote, "systemctl --failed --no-legend --plain")
    out["vivid_loaded"] = E.out(a.remote, "test -d /sys/module/vivid && echo yes || echo no")
    return out


def step_lab(a, stand):
    srcs0, _act, _d = stand.sources()
    vs, notes, rec = [], [], {}
    try:
        v1, n1, rec["usbip"] = step_usbip(a, stand, srcs0)
        v2, n2, rec["vivid"] = step_vivid(a, stand, srcs0)
        vs, notes = v1 + v2, n1 + n2
    finally:
        rec["cleanup"] = c = lab_cleanup(a, stand)
    if c["failed"].strip():
        vs.append("FAIL")
        notes.append(f"после остановки п. 7 failed-юниты: {c['failed'].strip()} — confirm будет отклонён "
                     "(гейт failed-units), а сбросить состояние failed этих юнитов polkit не разрешает")
    bad_stop = [f"{u} rc {c[u]['rc']}" for u in (USBIP, VIVID_FEED, VIVID) if c[u]["rc"] != 0]
    if bad_stop:
        vs.append("FAIL")
        notes.append("stop юнита п. 7 не прошёл: " + ", ".join(bad_stop))
    if c["synthetic"]["remote_streaming_s"] is None:
        vs.append("ERROR")
        notes.append("возврат блока на synthetic перед остановкой vivid не подтверждён")
    if c["vivid_loaded"] == "yes":
        vs.append("FAIL")
        notes.append("после stop sheregesh-lab-vivid модуль vivid загружен (modprobe -r не прошёл?)")
    return agg(vs), "; ".join(notes), rec


def step_persist_restart(a, stand, orig):
    _src, active, _d = stand.sources()
    want = persist_target((active or [None])[0], a.rtsp_id)
    srcs, _a, _d = stand.sources()
    kind = kind_of(want, srcs)
    m = {"want": want, "kind": kind, "event": f"systemctl restart {E.REMOTE_UNIT}", "timeout_s": LIMITS["restart_s"]}
    pk = stand.pick(want, kind)
    m["selected"], m["select_detail"] = pk["selected"] and pk.get("console_live_s") is not None, pk.get("select_detail")
    if not m["selected"]:
        return judge_persist(m), m
    m["saved_ok"], m["saved_detail"] = judge_saved(stand.conf(), want, orig)
    s0, pid0 = stand.sessions(), E.out(a.remote, f"systemctl show -p MainPID --value {E.REMOTE_UNIT}")
    t0 = time.monotonic()
    code, text = ssh(a.remote, f"systemctl --no-ask-password restart {E.REMOTE_UNIT}", 90)
    m["event_ok"], m["event_detail"] = code == 0, f"rc {code} {text[-200:]}"
    m["restart_cmd_s"] = round(time.monotonic() - t0, 1)
    if code == 0:
        m["ready_s"] = stand.ready_after(kind, s0, t0, LIMITS["restart_s"])
        m["mainpid"] = [pid0, E.out(a.remote, f"systemctl show -p MainPID --value {E.REMOTE_UNIT}")]
        _src, m["active_after"], _d = stand.sources()
        m["saved_after_ok"], m["saved_after_detail"] = judge_saved(stand.conf(), want, orig)
        if m["mainpid"][0] == m["mainpid"][1]:
            m["event_ok"], m["event_detail"] = False, f"MainPID не сменился ({pid0}) — перезапуска не было"
    return judge_persist(m), m


def step_persist_reboot(a, stand, orig):
    """committed: выбор GUI-путём → systemctl reboot pi2 → SSH вернулся → тот же источник активен, файл прежний."""
    o = ota_summary(E.jload(E.out(a.remote, "sheregesh-ota-client status")))
    ok, why = reboot_allowed(o)
    if not ok:
        return ("BLOCKED", f"перезагрузка запрещена: {why} (вернула бы основной слот)"), {"ota": o}
    _src, active, _d = stand.sources()
    want = persist_target((active or [None])[0], a.rtsp_id)
    srcs, _a, _d = stand.sources()
    kind = kind_of(want, srcs)
    m = {"want": want, "kind": kind, "event": "systemctl reboot", "timeout_s": LIMITS["ready_s"]}
    pk = stand.pick(want, kind)
    m["selected"], m["select_detail"] = pk["selected"] and pk.get("console_live_s") is not None, pk.get("select_detail")
    if not m["selected"]:
        return judge_persist(m), m
    m["saved_ok"], m["saved_detail"] = judge_saved(stand.conf(), want, orig)
    boot0, s0 = E.out(a.remote, "cat /proc/sys/kernel/random/boot_id"), stand.sessions()
    stand.rs.stop()
    t0 = time.monotonic()
    code, text = ssh(a.remote, "systemctl --no-ask-password reboot 2>&1", 30)
    m["reboot_rc"] = code                           # 255 — соединение закрыто перезагрузкой: это не отказ
    if code not in (0, 255, 124) or "denied" in text.lower() or "authentication" in text.lower():
        m["event_ok"], m["event_detail"] = False, f"reboot не принят: rc {code} {text[-200:]}"
        stand.rs = S4.RemoteSampler(a.remote).start()
        return judge_persist(m), m
    w = wait_reboot(a.remote, boot0, LIMITS["boot_s"])
    m["reboot"] = w
    if w["up_s"] is None:
        m["event_ok"], m["event_detail"] = False, (f"SSH не вернулся за {LIMITS['boot_s']:.0f} с (адрес DHCP сменился? "
                                                   "collect_boot.py find --mac d8:3a:dd:69:0f:6e)")
        return judge_persist(m), m
    m["event_ok"], m["event_detail"] = True, ""
    stand.rs = S4.RemoteSampler(a.remote).start()
    m["ready_s"] = stand.ready_after(kind, s0, t0, w["up_s"] + LIMITS["ready_s"])
    m["ready_after_ssh_s"] = round(m["ready_s"] - w["up_s"], 1) if m["ready_s"] is not None else None
    _src, m["active_after"], _d = stand.sources()
    m["saved_after_ok"], m["saved_after_detail"] = judge_saved(stand.conf(), want, orig)
    m["ota_after"] = ota_summary(E.jload(E.out(a.remote, "sheregesh-ota-client status")))
    v, d = judge_persist(m)
    back = ota_problems(m["ota_after"], "committed", a.expect_version)
    if back:
        v, d = "FAIL", f"{d}; после перезагрузки: {'; '.join(back)}"
    return (v, d), m


def snap_pi2(a, tag):
    import slot_guard as G
    d = G.snap("pi2", a.remote)
    errs, _info = G.validate(d)
    d["valid"], d["validation_errors"] = not errs, errs
    (a.out / f"pi2-{tag}.json").write_text(json.dumps(d, ensure_ascii=False, indent=1))
    return d


def step_commit(a, base):
    v, bad, checks = "ERROR", ["нет снимка «до confirm» (--baseline DIR/pi2-before-confirm.json)"], []
    bf = Path(a.baseline or "") / "pi2-before-confirm.json"
    after = snap_pi2(a, "after-commit")
    if bf.exists():
        v, bad, checks = judge_commit(json.loads(bf.read_text()), after, a.expect_version)
        (a.out / "pi2-compare-commit.txt").write_text("\n".join(f"{c['status']} {c['id']} — {c['detail']}"
                                                                for c in checks) + f"\n== {v}\n")
    pv, pp = judge_pi1(base.get("pi1"), pi1_state(a))
    res = agg([v, pv])
    return res, "; ".join(bad + pp) or "COMMITTED B; slot_guard — только ожидаемые отличия; pi1 не затронут", \
        {"pi2": {"verdict": v, "unexpected": bad}, "pi1": {"verdict": pv, "problems": pp}}


# ---------------------------------------------------------------- план и запуск


def plan(a):
    gui = "да (+2 мин)" if a.gui else "нет"
    tr = [
        "S5 pi2 после OTA bd74c8c — фаза trial (пробная загрузка B, до confirm), ≈ 4–5 мин (P2-05 — после confirm):",
        "  P2-01 слот B, TRYBOOT/HEALTH_PENDING, tryboot, образ bd74c8c / 0.1.1792949168, заводской код,",
        "        failed 0, health PASS; иначе активные шаги BLOCKED                                     ~0,5 мин",
        "  P2-02 SSH; check_link health,identity; TLS 1.3, видео tls1.3-mutual; check_video_link session   ~1,5 мин",
        f"  P2-03 synthetic GUI-путём (videoselect via console), live, кадры растут; снимок GUI: {gui}  ~0,3 мин",
        f"  P2-04 {a.rtsp_id} (shcam H.264 1280×720@30) — выбор, live                                ~0,3 мин",
        "  P2-06 S5-D1: выбор сохраняется в video-input.json, переживает systemctl restart sheregesh-remote-net ~1 мин",
        "  затем: исходный video-input.json + SIGHUP; slot_guard snap pi2 «до confirm», состояние pi1     ~0,7 мин",
        "  Перезагрузки нет: обычная перезагрузка пробной загрузки вернула бы слот A.",
    ]
    cm = [
        "S5 pi2 — фаза committed (после ota-confirm), ≈ 5,5 мин + загрузка pi2 (~1,5–2 мин):",
        "  P2-01 слот B, COMMITTED, autoboot [all]=3, образ bd74c8c, failed 0, health PASS            ~0,5 мин",
        f"  P2-05 start {USBIP} → {a.hdmi_id} как hdmi → выбор → stop/start (no-device ≤ 3 с, возврат ≤ 10 с),",
        "        enable отклонён polkit; start vivid + feed@cvbs → выбор → nosignal/ok; synthetic; stop всех ~3 мин",
        "  P2-06 выбор GUI-путём → video-input.json → systemctl reboot (только при COMMITTED/B/[all]=3) →",
        f"        SSH ≤ {LIMITS['boot_s']:.0f} с → тот же источник live ≤ {LIMITS['ready_s']:.0f} с, "
        "файл прежний                                                                ~1 мин",
        "  P2-07 slot_guard «после» против pi2-before-confirm.json фазы trial (только отличия фиксации),",
        "        OTA COMMITTED/B; pi1: OTA B/COMMITTED, версия и boot_id прежние                        ~1 мин",
        "  затем: исходный video-input.json + SIGHUP, сверка активного источника.",
    ]
    return "\n".join(tr if a.phase == "trial" else cm)


def main(argv=None):
    global LOG
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console", default="auto")
    ap.add_argument("--remote", default="auto")
    ap.add_argument("--phase", choices=tuple(PHASE_STEPS), default="trial")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--baseline", help="committed: каталог --out фазы trial (pi2-before-confirm.json, baseline.json)")
    ap.add_argument("--expect-commit", default=EXPECT_COMMIT)
    ap.add_argument("--expect-version", default=EXPECT_VERSION)
    ap.add_argument("--allow-dev-release", action="store_true", help="P2-01: допустить код из DEV-выпуска")
    ap.add_argument("--rtsp-id", default="rtsp:1", help="id shcam в status.video.sources (rtsp_urls video-input.json)")
    ap.add_argument("--hdmi-id", default="v4l2:534d:2109")
    ap.add_argument("--vivid-id", default="auto")
    ap.add_argument("--outage", type=float, default=6.0, help="удержание отказа эмулятора, с")
    ap.add_argument("--gui", action="store_true", help="P2-03/04: снимки gui_capture --by select (только trial)")
    ap.add_argument("--only")
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
    if a.phase == "committed" and not a.baseline:
        ap.error("--phase committed требует --baseline DIR (каталог фазы trial)")
    a.out.mkdir(parents=True, exist_ok=True)
    LOG = a.out / "run.log"
    steps = PHASE_STEPS[a.phase]
    only = set(a.only.split(",")) & set(steps) if a.only else set(steps)
    ids = {"console": E.board_identity(a.console), "remote": E.board_identity(a.remote)}
    meta = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "phase": a.phase, "boards": ids,
            "args": {k: str(v) for k, v in vars(a).items()}}
    problems = [f"{r}: серийный номер {ids[r]['serial']!r} ≠ {E.SERIALS[r]!r}" for r in ids
                if ids[r]["serial"] != E.SERIALS[r]]
    pi1 = pi1_state(a)
    if (pi1["ota"] or {}).get("state") not in E.OTA_OK:
        problems.append(f"pi1: OTA {pi1['ota']!r} не в покое — без действий")
    base = json.loads((Path(a.baseline) / "baseline.json").read_text()) if a.baseline and \
        (Path(a.baseline) / "baseline.json").exists() else {}
    if problems:
        (a.out / "summary.json").write_text(json.dumps({**meta, "preflight": problems, "verdict": "ERROR"},
                                                       ensure_ascii=False, indent=1, default=str))
        log("ПРЕДПРОВЕРКА: " + "; ".join(problems) + " — без действий")
        return 2
    ctx = {"t_start_epoch": float(E.out(a.console, "date +%s.%N") or time.time())}
    t_run = time.monotonic()
    results, durations = {}, {}

    def put(step, v, detail, extra=None, t0=None):
        if t0 is not None:
            durations[step] = round(time.monotonic() - t0, 1)
        results[step] = {"verdict": v, "detail": detail, **({"measured": extra} if extra else {}),
                         **({"duration_s": durations[step]} if step in durations else {})}
        log(f"{step}: {v}" + (f" — {detail}" if detail else ""))

    stand = Stand(a)
    stand.orig_conf = stand.conf()
    _s, orig_active, _d = stand.sources()
    meta["video_input_orig"], meta["active_orig"] = stand.orig_conf, orig_active
    (a.out / "video-input.orig.json").write_text(stand.orig_conf)
    stand.ui.start()
    stand.rs.start()
    critical = False
    try:
        time.sleep(2)
        t0 = time.monotonic()
        v, d, critical, x = step_boot(a, a.phase)
        put("P2-01", v, d, x, t0)
        if a.phase == "trial" and not critical:
            (a.out / "baseline.json").write_text(json.dumps({"pi1": pi1, "pi2_ota": x.get("ota"),
                                                             "time": meta["time"]}, ensure_ascii=False, indent=1))
        blocked = "BLOCKED", "P2-01: pi2 не в ожидаемом слоте/состоянии OTA — воздействия не выполняются"
        if "P2-02" in only:
            t0 = time.monotonic()
            put("P2-02", *step_link(a, ctx), t0=t0)
        if "P2-03" in only or "P2-04" in only:
            gui_states = None
            if critical:
                for k in ("P2-03", "P2-04"):
                    if k in only:
                        put(k, *blocked)
            else:
                srcs, _act, _d = stand.sources()
                picks = {}
                for k, sid in (("P2-03", "synthetic"), ("P2-04", a.rtsp_id)):
                    if k not in only:
                        continue
                    t0 = time.monotonic()
                    ent = next((s for s in srcs if s.get("id") == sid), None)
                    if ent is None or not ent.get("available"):
                        why = "нет в списке" if ent is None else "available=false"
                        put(k, BLOCKED, f"{S5.HW_UNAVAILABLE}: {sid} {why} блока ({[x.get('id') for x in srcs]})",
                            None, t0)
                        continue
                    picks[k] = (sid, kind_of(sid, srcs), stand.pick(sid, kind_of(sid, srcs)), t0)
                if a.gui and a.phase == "trial" and picks:
                    sel = ",".join(p[0] for p in picks.values())
                    gui_states, notes = S5.run_gui(a, f"--by select --select {sel} --timeout 150",
                                                   lambda phase: None, "select")
                    stand.wait(stand.ui, S4.console_live, time.monotonic(), 60)
                for k, (sid, kind, m, t0) in picks.items():
                    v, d = judge_pick(m)
                    if a.gui and a.phase == "trial":
                        gv, gd = (judge_gui_pick(gui_states, sid, kind) if gui_states is not None
                                  else ("ERROR", "gui_capture не запущен"))
                        v, d = agg([v, gv]), f"{d}; GUI: {gd}"
                    else:
                        d += "; без снимка GUI (--gui)"
                    put(k, v, d, m, t0)
        if "P2-05" in only:
            t0 = time.monotonic()
            put("P2-05", *(blocked if critical else step_lab(a, stand)), t0=t0)
        if "P2-06" in only:
            t0 = time.monotonic()
            if critical:
                put("P2-06", *blocked)
            elif a.phase == "trial":
                (v, d), m = step_persist_restart(a, stand, stand.orig_conf)
                put("P2-06", v, d + " (перезагрузка — в фазе committed)", m, t0)
            else:
                (v, d), m = step_persist_reboot(a, stand, stand.orig_conf)
                put("P2-06", v, d, m, t0)
    finally:
        if stand.changed:
            try:
                meta["restore"] = stand.restore(stand.orig_conf, orig_active)
            except Exception as e:                                    # noqa: BLE001 — итог пишется в любом случае
                meta["restore"] = {"error": repr(e)}
            log(f"восстановление video-input.json: {meta['restore']}")
        stand.ui.stop()
        stand.rs.stop()
    if a.phase == "trial" and not critical:
        t0 = time.monotonic()
        snap = snap_pi2(a, "before-confirm")
        meta["before_confirm_snap"] = {"valid": snap["valid"], "errors": snap["validation_errors"][:5],
                                       "duration_s": round(time.monotonic() - t0, 1)}
        log(f"slot_guard snap pi2 «до confirm»: valid={snap['valid']}")
    if "P2-07" in only:
        t0 = time.monotonic()
        put("P2-07", *step_commit(a, base), t0=t0)
    for k in STEPS:
        results.setdefault(k, {"verdict": "SKIP", "detail": "не в этой фазе или не запрошен (--only)"})
    r = meta.get("restore") or {}
    if r and not (r.get("file_restored") and r.get("active_restored")):
        meta["restore_warning"] = "исходный video-input.json или активный источник не восстановлены — см. restore"
        log("ВНИМАНИЕ: " + meta["restore_warning"])
    summary = {**meta, "duration_min": round((time.monotonic() - t_run) / 60, 1), "durations_s": durations,
               "results": results, "verdict": verdict_of(results)}
    (a.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    log(f"ИТОГ {a.phase} {summary['verdict']} за {summary['duration_min']} мин: " +
        ", ".join(f"{k} {v['verdict']}" for k, v in results.items()))
    return {"PASS": 0, "FAIL": 1, "ERROR": 2, BLOCKED: 3, "SKIP": 2}[summary["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
