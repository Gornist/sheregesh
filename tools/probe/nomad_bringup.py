#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Пошаговое включение настоящего RadioMaster Nomad на линии UART3 удалённого блока pi2 (ADR-0014 §3.4).

Перед запуском — схема и измерения docs/hardware/NOMAD-WIRING.md (там же порядок запуска). Каждый шаг печатает
результат по-русски и при ошибке останавливает прогон; следующий шаг не выполняется.

  # с машины владельца: шаги 1–3 исполняются на pi2 по SSH (sheregesh-ui, без sudo, без файлов на плате)
  python3 tools/probe/nomad_bringup.py --host 192.168.1.171
  python3 tools/probe/nomad_bringup.py --host 192.168.1.171 --steps 1          # только послушать линию
  python3 tools/probe/nomad_bringup.py --host 192.168.1.171 --switch-backend   # 1–3, затем шаг 4 (с подтверждением)

  # прямо на pi2 (из выпуска DEV deploy или образа)
  C=$(readlink -f /var/lib/sheregesh-deploy/current); PYTHONPATH=$C/software python3 nomad_bringup.py --local

Шаги:
  1  найти UART3 (serial@7e201600) и проверить, что линия в покое: 2 с только чтение, НИЧЕГО НЕ ОТПРАВЛЯЕТСЯ;
  2  ping 0x28 → device info 0x29 (имя «RM Nomad…», прошивка); сначала только ping, если модуль молчит — ping вместе
     с нейтральными кадрами каналов (как EdgeTX: ELRS отвечает в окне после кадра хоста). Проверяется и эхо своей
     передачи (полудуплекс: RX pi2 видит свою TX через резистор — значит провода линии на месте);
  3  нейтраль (каналы 992, взведение false): до 10 с на захват (device info и синхронизация 0x3A.0x10), затем
     10 с замера — период модуля, link statistics 0x14, частота и джиттер кадров, ошибки CRC, коллизии;
  4  ТОЛЬКО с --switch-backend и подтверждением оператора: modulectl configure backend=crsf-uart через пульт pi1
     (как кнопка экрана «Модуль»), ожидание READY. Возврат: configure backend=lab-emulator.

Шаги 2–3 заставляют модуль работать на передачу (ELRS TX излучает, пока идут кадры хоста): перед шагом 2 сценарий
спрашивает, подключены ли антенны и питание XT30 (--yes — не спрашивать). Пока агент блока держит UART (backend
crsf-uart), шаги 1–3 не выполняются. Коды выхода: 0 — все запрошенные шаги прошли, 1 — шаг остановил прогон, 2 —
ошибка запуска (нет пакета sheregesh.crsf, нет SSH, отказ оператора).
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time

HERE = os.path.abspath(__file__) if "__file__" in globals() else None
RESULT = "@@RESULT "
LINK_STATUS = "/run/sheregesh-remote/link-status.json"
REL = "C=$(readlink -f /var/lib/sheregesh-deploy/current 2>/dev/null || echo /usr/lib/sheregesh)"
RELEASE_PATHS = ("/var/lib/sheregesh-deploy/current/software", "/usr/lib/sheregesh/software")
BAUD = 400000
LISTEN_S = 2.0                     # шаг 1: только чтение
PING_S = 3.0                       # шаг 2: только ping
# ELRS 4.1 подбирает скорость и полярность сам, по потоку кадров хоста: аппаратное измерение после ~300 перепадов,
# проверка раз в 1 с, полярность меняется через шаг — захват от единиц секунд (EDGETX-EXTERNAL-MODULE.md §1.4). После
# паузы между шагами модуль снова ищет скорость: каждому шагу — свой запас на захват.
PING_NEUTRAL_S = 10.0              # шаг 2: ping с нейтральными кадрами, если модуль молчит на одиночный ping
WARMUP_S = 10.0                    # шаг 3: ждать device info и синхронизацию до начала замера
NEUTRAL_S = 10.0                   # шаг 3: замер
NAME_PREFIX = "RM Nomad"           # профиль radiomaster-nomad: crsf_name
PERIOD_MIN_US, PERIOD_MAX_US = 1000, 50000
JITTER_P99_MAX_US = 2000           # как CR-05
BACKEND_UART, BACKEND_LAB = "crsf-uart", "lab-emulator"


def say(text=""):
    print(text, flush=True)


# ---------------------------------------------------------------- чистые функции (тесты: test_s8_crsf_accept.py)


def holders(path, proc="/proc"):
    """Процессы (видимые этому пользователю), у которых открыт path → [(pid, команда)]."""
    try:
        real = os.path.realpath(path)
    except OSError:
        return []
    out = []
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return []
    for pid in pids:
        fd_dir = os.path.join(proc, pid, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.path.realpath(os.path.join(fd_dir, fd)) == real:
                    with open(os.path.join(proc, pid, "cmdline"), "rb") as f:
                        cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
                    out.append((int(pid), cmd[:120]))
                    break
            except OSError:
                continue
    return out


def agent_backend(doc):
    """backend модуля из link-status.json блока (module.backend, S8) или None."""
    m = (doc or {}).get("module")
    b = m.get("backend") if isinstance(m, dict) else None
    return b if isinstance(b, str) else None


def judge_idle(nbytes, frames, garbage):
    """Шаг 1: в линии без нашей передачи. → (ок, текст)."""
    if nbytes == 0:
        return True, (f"за {LISTEN_S:g} с в линии ни одного байта — линия в покое. Так и должно быть: модуль отвечает "
                      "только на кадры хоста")
    if frames and not garbage:
        return False, (f"модуль сам шлёт кадры без запроса ({nbytes} байт, кадры {frames}). Для ELRS TX это необычно: "
                       "проверьте, что на линии нет другого хоста (аппаратуры, адаптера), и повторите шаг 1")
    return False, (f"в линии {nbytes} байт без нашей передачи (мусор {garbage} байт, верных кадров {len(frames)}). "
                   "Вероятно: нет общей земли, линия «висит» (нет подтяжки), другой уровень или полярность, помеха. "
                   "Проверьте провод GND (контакт 9) и уровень линии мультиметром — NOMAD-WIRING.md §3. Не продолжаю")


def judge_ping(snap, stats, phase):
    """Шаг 2. snap — CrsfOutput.snapshot(), stats — UartPort.stats(), phase — "ping" | "ping+neutral" | None."""
    p = []
    if (stats or {}).get("tx_bytes", 0) and not (stats or {}).get("echo_ok"):
        p.append("эхо своей передачи не вернулось: RX pi2 (контакт 29) не видит TX (контакт 7) — проверьте провода и "
                 "резистор линии")
    dev = (snap or {}).get("device")
    if not dev:
        p.append("device info 0x29 не пришёл ни на одиночный ping, ни вместе с нейтральными кадрами — модуль не "
                 "отвечает. Проверьте питание XT30 (светодиоды модуля), провод сигнала к адаптеру (прозвонка, "
                 "NOMAD-WIRING.md §3) и что модуль не в режиме Wi-Fi")
        if (snap or {}).get("garbage_bytes") or (snap or {}).get("crc_errors"):
            p.append(f"при этом в ответ идут байты с ошибками (мусор {snap.get('garbage_bytes')}, CRC "
                     f"{snap.get('crc_errors')}) — похоже на другую скорость или полярность линии")
        return False, "; ".join(p)
    name = str(dev.get("name") or "")
    if not name.startswith(NAME_PREFIX):
        p.append(f"модуль ответил, но имя «{name}» не начинается с «{NAME_PREFIX}» — профиль radiomaster-nomad его "
                 "не примет")
    if p:
        return False, "; ".join(p)
    how = "на одиночный ping" if phase == "ping" else "только вместе с нейтральными кадрами каналов"
    return True, f"модуль ответил {how}: «{name}», прошивка {dev.get('sw')}, серийный {dev.get('serial')}"


def judge_neutral(first, last, dt_s, sync_at_s):
    """Шаг 3. first/last — снимки CrsfOutput в начале и конце, dt_s — между ними, sync_at_s — когда появилась
    синхронизация (None — не было). → (ок, текст, сводка)."""
    sent = (last.get("frames_sent") or 0) - (first.get("frames_sent") or 0)
    rate = round(sent / dt_s, 1) if dt_s > 0 else None
    period = last.get("period_us")
    summ = {"frames": sent, "rate_hz": rate, "period_us": period, "synced": last.get("synced"),
            "sync_at_s": sync_at_s, "jitter_us_p99": last.get("jitter_us_p99"), "link": last.get("link"),
            "crc_errors": (last.get("crc_errors") or 0) - (first.get("crc_errors") or 0),
            "collisions": (last.get("collisions") or 0) - (first.get("collisions") or 0),
            "overruns": (last.get("overruns") or 0) - (first.get("overruns") or 0), "device": last.get("device")}
    p, notes = [], []
    if not last.get("synced"):
        p.append(f"синхронизации 0x3A.0x10 нет (захват {WARMUP_S:g} с + замер {NEUTRAL_S:g} с) — модуль не подстроил "
                 f"период (кадры шли с периодом {period} мкс, {rate} Гц)")
    elif not isinstance(period, int) or not PERIOD_MIN_US <= period <= PERIOD_MAX_US:
        p.append(f"период после синхронизации {period} мкс вне 1…50 мс")
    if not last.get("device"):
        p.append("device info пропал — модуль перестал отвечать")
    if summ["crc_errors"]:
        p.append(f"ошибок CRC в ответах модуля: {summ['crc_errors']}")
    if isinstance(last.get("jitter_us_p99"), int) and last["jitter_us_p99"] > JITTER_P99_MAX_US:
        notes.append(f"p99 джиттера {last['jitter_us_p99']} мкс > {JITTER_P99_MAX_US} (нагрузка pi2?)")
    if summ["collisions"]:
        notes.append(f"коллизий на линии {summ['collisions']} (модуль отвечал одновременно с нашей передачей)")
    if not last.get("link"):
        notes.append("link statistics 0x14 нет (без связанного приёмника так бывает)")
    if p:
        return False, "; ".join(p + notes), summ
    link = last.get("link") or {}
    txt = (f"синхронизация через {sync_at_s} с, период модуля {period} мкс ({rate} Гц кадров), p99 джиттера "
           f"{last.get('jitter_us_p99')} мкс, CRC без ошибок" +
           (f"; link: RSSI {link.get('rssi')}, LQ {link.get('lq')}, мощность {link.get('tx_power')} мВт"
            if link else ""))
    return True, txt + ("; " + "; ".join(notes) if notes else ""), summ


def modulectl_cmd(args):
    """Команда modulectl на пульте pi1 (из выпуска пульта), путь — локальный канал работающего пульта."""
    return f"{REL}; PYTHONPATH=$C/software python3 -m sheregesh.link.modulectl {args} --via console 2>&1"


def parse_json(text):
    """Последний JSON-объект с ключом result в тексте (вывод modulectl)."""
    dec, text, out = json.JSONDecoder(), text or "", {}
    i = text.find("{")
    while i >= 0:
        try:
            v, _e = dec.raw_decode(text[i:])
            if isinstance(v, dict) and "result" in v:
                out = v
        except ValueError:
            pass
        i = text.find("{", i + 1)
    return out


def remote_cmd(source, argv):
    """Команда SSH для pi2: этот же сценарий из base64 в python3 -c, пакет sheregesh — из выпуска на плате."""
    b64 = base64.b64encode(source.encode()).decode()
    quoted = " ".join("'" + x.replace("'", "'\\''") + "'" for x in argv)
    return f"{REL}; PYTHONPATH=$C/software python3 -u -c \"$(echo {b64} | base64 -d)\" {quoted} 2>&1"


# ---------------------------------------------------------------- шаги на pi2 (--local)


def load_crsf():
    """Пакет sheregesh.crsf (агент K) из выпуска на плате; при запуске из рабочей копии — из software/."""
    extra = list(RELEASE_PATHS)
    if HERE:
        extra.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "software"))
    for attempt in range(2):
        try:
            from sheregesh.crsf import frames, output, port
            return frames, port, output
        except ImportError:
            if attempt:
                raise
            sys.path[:0] = [p for p in extra if os.path.isdir(p)]
    return None


def resolve_device(a, port_mod):
    if a.device and a.device != "auto":
        return a.device, "задан вручную"
    path = port_mod.find_uart3()
    return path, "найден по serial@7e201600" if path else None


def precheck(a, port_mod):
    """Общее для шагов 1–3: устройство UART3 есть, доступно, не занято агентом блока. → (путь, проблема)."""
    path, how = resolve_device(a, port_mod)
    if not path:
        return None, ("UART3 не найден: нет tty с of_node serial@7e201600. Нужен образ с dtoverlay=uart3 "
                      "(os/layer/sheregesh-lab.yaml) — на платы только новым образом по решению владельца")
    say(f"  UART3: {path} ({how})")
    if not os.path.exists(path):
        return None, f"{path} не существует"
    if not os.access(path, os.R_OK | os.W_OK):
        return None, (f"нет прав на {path} у пользователя {os.environ.get('USER', '?')} (группа dialout?) — это "
                      "настройка образа, sudo сценарий не использует")
    try:
        with open(LINK_STATUS, encoding="utf-8") as f:
            backend = agent_backend(json.load(f))
    except (OSError, ValueError):
        backend = None
    if backend == BACKEND_UART:
        return None, (f"агент блока уже работает с UART (backend {BACKEND_UART}) — шаги 1–3 мешали бы ему. Верните "
                      f"backend {BACKEND_LAB} (modulectl configure backend={BACKEND_LAB}) или пропустите их")
    busy = holders(path)
    if busy:
        return None, f"{path} уже открыт: " + "; ".join(f"PID {p} «{c}»" for p, c in busy)
    return path, None


def step1(a):
    say("Шаг 1. Линия в покое: только чтение, ничего не отправляется")
    frames, port_mod, _out = load_crsf()
    path, why = precheck(a, port_mod)
    if why:
        return {"ok": False, "text": why}
    port = port_mod.UartPort(path, baud=a.baud, half_duplex=True)
    parser = frames.Parser(frames.HOST_RX_SYNCS)
    got, nbytes = [], 0
    try:
        end = time.monotonic() + LISTEN_S
        while time.monotonic() < end:
            data = port.read()
            if data:
                nbytes += len(data)
                got += parser.feed(data)
            time.sleep(0.01)
        termios2 = getattr(port, "termios2", None)
    finally:
        port.close()
    kinds = sorted({f"0x{f.type:02X}" for f in got})
    ok, text = judge_idle(nbytes, kinds, parser.garbage_bytes)
    return {"ok": ok, "text": text, "data": {"device": path, "baud": a.baud, "termios2": termios2, "bytes": nbytes,
                                             "frames": kinds, "garbage": parser.garbage_bytes}}


def _run_output(port, out_mod, mode, log):
    box = [mode]
    out = out_mod.CrsfOutput(port, lambda: (box[0], [0] * 8, 0), log=log)
    return out, box


def step2(a):
    say("Шаг 2. ping → device info (только ping; если модуль молчит — ping с нейтральными кадрами)")
    frames, port_mod, out_mod = load_crsf()
    path, why = precheck(a, port_mod)
    if why:
        return {"ok": False, "text": why}
    port = port_mod.UartPort(path, baud=a.baud, half_duplex=True)
    out, box = _run_output(port, out_mod, "none", lambda s: say(f"    {s}"))
    phase, snap = None, {}
    try:
        out.start()
        for name, mode, secs in (("ping", "none", PING_S), ("ping+neutral", "neutral", PING_NEUTRAL_S)):
            box[0] = mode
            if mode == "neutral":
                say("  на одиночный ping ответа нет — ping вместе с нейтральными кадрами каналов (взведение false)")
            end = time.monotonic() + secs
            while time.monotonic() < end:
                snap = out.snapshot()
                if snap.get("device"):
                    phase = name
                    break
                time.sleep(0.05)
            if phase:
                break
        snap = out.snapshot()
    finally:
        out.stop()
        stats = port.stats()
        port.close()
    # Эхо: RX pi2 видит свою передачу через резистор линии — провода TX/RX и линия на месте (единичные пропуски
    # бывают, когда модуль отвечает вплотную к нашему кадру).
    stats["echo_bytes"] = getattr(port, "echo_bytes", 0)
    stats["echo_ok"] = stats["echo_bytes"] * 2 >= stats.get("tx_bytes", 0) > 0
    ok, text = judge_ping(snap, stats, phase)
    return {"ok": ok, "text": text, "data": {"phase": phase, "device": snap.get("device"), "port": stats,
                                             "pings": snap.get("pings_sent"), "frames": snap.get("frames_sent"),
                                             "crc_errors": snap.get("crc_errors"),
                                             "garbage_bytes": snap.get("garbage_bytes")}}


def step3(a):
    say(f"Шаг 3. {NEUTRAL_S:g} с нейтрали: синхронизация, link statistics, частота и джиттер")
    frames, port_mod, out_mod = load_crsf()
    path, why = precheck(a, port_mod)
    if why:
        return {"ok": False, "text": why}
    port = port_mod.UartPort(path, baud=a.baud, half_duplex=True)
    out, _box = _run_output(port, out_mod, "neutral", lambda s: say(f"    {s}"))
    sync_at = None
    try:
        out.start()
        ts = time.monotonic()
        while time.monotonic() - ts < WARMUP_S:                  # захват скорости модулем, device info, sync
            s = out.snapshot()
            if s.get("synced") and s.get("device"):
                sync_at = round(time.monotonic() - ts, 1)
                break
            time.sleep(0.1)
        got = f"синхронизация через {sync_at} с" if sync_at is not None else f"синхронизации нет за {WARMUP_S:g} с"
        say(f"  захват: {got}; замер {NEUTRAL_S:g} с")
        t0, first = time.monotonic(), out.snapshot()
        while time.monotonic() - t0 < NEUTRAL_S:
            time.sleep(1.0)
            s = out.snapshot()
            say(f"  {time.monotonic() - t0:4.1f} с: период {s.get('period_us')} мкс, synced {s.get('synced')}, "
                f"кадров {s.get('frames_sent')}, link {s.get('link')}")
        dt, last = time.monotonic() - t0, out.snapshot()
    finally:
        out.stop()
        stats = port.stats()
        port.close()
    ok, text, summ = judge_neutral(first, last, dt, sync_at)
    return {"ok": ok, "text": text, "data": {**summ, "port": stats}}


STEPS = {1: step1, 2: step2, 3: step3}


def run_local_step(a, n):
    """Шаг n на этой плате; итог — строкой RESULT (для запуска по SSH) и словарём."""
    try:
        res = STEPS[n](a)
    except ImportError as e:
        res = {"ok": False, "text": f"нет пакета sheregesh.crsf ({e}) — на плате выпуск до S8", "fatal": True}
    except OSError as e:
        res = {"ok": False, "text": f"ошибка порта: {e}"}
    res["step"] = n
    say(RESULT + json.dumps(res, ensure_ascii=False, default=str))
    return res


# ---------------------------------------------------------------- оркестровка


def ssh(host, cmd, timeout=120, stream=False):
    """(код, вывод). stream — печатать строки по мере прихода (кроме строки RESULT)."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", f"sheregesh-ui@{host}", cmd]
    if not stream:
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            return r.returncode, r.stdout + r.stderr
        except subprocess.TimeoutExpired:
            return 124, ""
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    lines = []
    for ln in p.stdout:
        lines.append(ln)
        if not ln.startswith(RESULT):
            print(ln, end="", flush=True)
    return p.wait(), "".join(lines)


def result_of(text):
    for ln in reversed((text or "").splitlines()):
        if ln.startswith(RESULT):
            try:
                return json.loads(ln[len(RESULT):])
            except ValueError:
                return None
    return None


def own_source():
    if not HERE:
        raise SystemExit("запуск по --host возможен только из файла сценария")
    with open(HERE, encoding="utf-8") as f:
        return f.read()


def confirm(a, question, prompt=input):
    if a.yes:
        say(f"{question} — да (--yes)")
        return True
    try:
        ans = prompt(f"{question} [да/нет]: ").strip().lower()
    except EOFError:
        ans = ""
    return ans in ("да", "д", "yes", "y")


def step4(a, run=None):
    """Переключение backend на crsf-uart через пульт pi1 и ожидание READY."""
    run = run or ssh
    say(f"Шаг 4. backend {BACKEND_UART}: modulectl configure через пульт {a.console}")
    code, text = run(a.console, modulectl_cmd(f"configure backend={BACKEND_UART} --wait 30 --expect READY "
                                              f"--expect-timeout 20"), 120)
    d = parse_json(text)
    tel = ((d.get("module") or {}).get("telemetry") or {}) if d else {}
    dev = ((tel.get("crsf") or {}).get("device") or {}).get("name")
    data = {"rc": code, "result": d.get("result"), "detail": d.get("detail"), "state": tel.get("state"),
            "output": tel.get("output"), "device": dev,
            "backend": (tel.get("settings") or {}).get("backend")}
    back = modulectl_cmd(f"configure backend={BACKEND_LAB} --wait 30")
    if not d:
        return {"ok": False, "step": 4, "data": data,
                "text": f"modulectl не ответил JSON (rc {code}): {text[-200:]!r}. Вручную на pi1: "
                        f"{modulectl_cmd(f'configure backend={BACKEND_UART} --wait 30')}"}
    if d.get("result") != "ok":
        err = tel.get("error") or {}
        return {"ok": False, "step": 4, "data": data,
                "text": f"configure: {d.get('result')} {d.get('detail') or ''}; состояние {tel.get('state')} "
                        f"{err.get('code') or ''}. Вернуть прежний backend: {back}"}
    return {"ok": True, "step": 4, "data": data,
            "text": f"backend {BACKEND_UART}: модуль READY, device «{dev}», выход {tel.get('output')}. Вернуть "
                    f"лабораторный: {back}"}


def main(argv=None, prompt=input):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", help="pi2: шаги 1–3 по SSH (sheregesh-ui); без --host и с --local — на этой плате")
    ap.add_argument("--local", action="store_true", help="выполнять шаги на этой плате (pi2)")
    ap.add_argument("--console", default="192.168.1.92", help="pi1 для шага 4 (modulectl через пульт)")
    ap.add_argument("--steps", default="1,2,3", help="через запятую из 1,2,3 (шаг 4 — только --switch-backend)")
    ap.add_argument("--step", type=int, help=argparse.SUPPRESS)          # один шаг на плате (запуск по SSH)
    ap.add_argument("--switch-backend", action="store_true",
                    help=f"после шагов 1–3 переключить backend на {BACKEND_UART} (шаг 4, с подтверждением)")
    ap.add_argument("--device", default="auto", help="tty UART3: auto (по serial@7e201600) или путь")
    ap.add_argument("--baud", type=int, default=BAUD)
    ap.add_argument("--yes", action="store_true", help="не спрашивать подтверждений (антенны, шаг 4)")
    ap.add_argument("--out", help="файл JSON с итогами шагов")
    a = ap.parse_args(argv)
    if a.step is not None:
        if a.step not in STEPS:
            ap.error("--step: 1, 2 или 3")
        return 0 if run_local_step(a, a.step).get("ok") else 1
    try:
        steps = sorted({int(x) for x in a.steps.split(",") if x.strip()})
    except ValueError:
        ap.error("--steps: числа через запятую")
    if not steps or not set(steps) <= set(STEPS):
        ap.error("--steps: из 1, 2, 3")
    if a.switch_backend:
        steps.append(4)
    if not a.host and not a.local:
        ap.error("--host АДРЕС_pi2 (с машины владельца) или --local (на самой pi2)")
    src = own_source() if a.host else None
    results = []
    say(f"Nomad на UART3 pi2: шаги {steps} ({'по SSH ' + a.host if a.host else 'на этой плате'})")
    code = 0
    for n in steps:
        if n == 2 and not confirm(a, "Антенны подключены к ОБОИМ разъёмам Nomad, питание — только XT30 от БП 6–16,8 В "
                                     "с ограничением тока, общая земля с pi2?", prompt):
            say("Остановлено оператором: без антенн модуль на передачу не включаем")
            code = 2
            break
        if n == 4:
            if not confirm(a, f"Шаги 1–3 прошли. Переключить backend блока на {BACKEND_UART} (агент pi2 откроет "
                              f"UART и начнёт слать кадры модулю)?", prompt):
                say("Шаг 4 не выполнен: оператор отказался")
                code = 2
                break
            res = step4(a)
        elif a.host:
            rc, text = ssh(a.host, remote_cmd(src, ["--step", str(n), "--device", a.device, "--baud", str(a.baud)]),
                           120, stream=True)
            res = result_of(text) or {"ok": False, "step": n, "fatal": True,
                                      "text": f"нет итога шага с {a.host} (rc {rc}): {text[-200:]!r}"}
        else:
            res = run_local_step(a, n)
        results.append(res)
        say(f"  → шаг {n}: {'ГОТОВО' if res.get('ok') else 'СТОП'} — {res.get('text')}")
        if not res.get("ok"):
            code = 2 if res.get("fatal") else 1
            say(f"Прогон остановлен на шаге {n}. Следующие шаги не выполнялись.")
            break
    else:
        say("Все запрошенные шаги прошли." + ("" if 4 in steps else
                                             f" Переключение на {BACKEND_UART} — повторить с --switch-backend."))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "host": a.host, "steps": steps,
                       "results": results}, f, ensure_ascii=False, indent=1, default=str)
    return code


if __name__ == "__main__":
    sys.exit(main())
