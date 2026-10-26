# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Внешний модуль удалённого блока из командной строки (стенд, приёмка S6) — тем же путём, что GUI (ADR-0012, 9.4).

  python3 -m sheregesh.link.modulectl status
  python3 -m sheregesh.link.modulectl reinit | reset [--expect READY [--expect-timeout 15]]
  python3 -m sheregesh.link.modulectl lab detach | attach | iface-error | hang | heal | slow-init | crash
                                          | silent | garbage | crc
                                          | emu-unavailable | emu-available
  python3 -m sheregesh.link.modulectl configure enabled=false [auto_init=true auto_recover=true profile=ID
                                          backend=lab-emulator|crsf-pty-lab|crsf-uart]

Как videoselect: по умолчанию запрос передаётся работающему пульту по локальному каналу (unix-сокет пульта,
{"cmd": "module", …}); пульт исполняет его слотами экрана «Модуль» с подтверждением. Запускать от пользователя
пульта (sheregesh-ui). --via direct — собственный сеанс PeerClient (ключ канала --link-config; работающий пульт
вытесняется на 1–3 с); --via auto — пульт, если запущен. Для испытаний на loopback: --via direct --port N
(без TLS, 127.0.0.1).
Вывод — JSON {"result", "detail", "via", "module": M}; M — как ModuleView.snapshot() пульта ({"support", "stale",
"session", "telemetry"}; через пульт — ещё "display").
--expect STATE — после команды опрашивать состояние раз в 0,5 с до telemetry.state == STATE (итог timeout, если нет).
Код выхода: 0 — ok (и состояние достигнуто при --expect); 1 — отказ или таймаут; 2 — нет связи / пульта или
неверные аргументы. Лабораторных сценариев вне лабораторного бэкенда блок не исполняет (forbidden)."""
import argparse
import json
import sys
import time
from pathlib import Path

from ..module import wire
from . import tls
from .client import PeerClient
from .codec import TYPES
from .videoselect import ConsoleAbsent, ask_console, socket_path

NO_LINK = ("not-connected", "no-link", "bad-request")
POLL_S = 0.5


def parse_configure(pairs):
    """KEY=VALUE… → args для configure (ValueError — неверный ключ или значение)."""
    out = {}
    for p in pairs:
        k, sep, v = p.partition("=")
        if not sep or k not in wire.CONFIGURE_KEYS:
            raise ValueError(f"expected KEY=VALUE with KEY one of {', '.join(wire.CONFIGURE_KEYS)}: {p!r}")
        if wire.CONFIGURE_KEYS[k] is bool:
            if v.lower() not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
                raise ValueError(f"{k}: expected true or false, got {v!r}")
            out[k] = v.lower() in ("true", "1", "yes", "on")
        else:
            if not wire.PROFILE_ID.fullmatch(v):
                raise ValueError(f"{k}: bad value {v!r}")
            out[k] = v
    if not out:
        raise ValueError("configure needs at least one KEY=VALUE")
    return out


def view(snap):
    """M в прямом сеансе: тот же вид, что ModuleView.snapshot() пульта, без display."""
    session = snap.get("session")
    tel = (snap.get("telemetry") or {}).get("module") if snap.get("phase") == "connected" else None
    if snap.get("phase") != "connected":
        support = "no-link"
    elif "module-cmd" not in TYPES.get(session or "", ()):
        support = "old-remote"
    elif tel is None:
        support = "absent"
    else:
        support = "ok"
    return {"support": support, "stale": False, "session": session, "telemetry": tel}


class Console:
    def __init__(self, path, timeout):
        self.path, self.timeout = path, timeout

    def run(self, op, args, wait):
        req = {"cmd": "module", "op": op, "wait": wait}
        if args:
            req["args"] = args
        return ask_console(self.path, req, wait + self.timeout)

    def status(self):
        r = ask_console(self.path, {"cmd": "module", "op": "status"}, self.timeout)
        return r.get("module") or {}


class Direct:
    def __init__(self, a):
        if a.port is not None:
            self.pc = PeerClient("127.0.0.1", a.port, heartbeat_timeout_s=3.0)
        else:
            cfg = tls.LinkConfig.load(a.link_config)
            self.pc = PeerClient(None, None, heartbeat_timeout_s=3.0, tls_cfg=cfg)
        self.pc.start()
        end = time.monotonic() + a.connect_timeout
        while time.monotonic() < end:
            s = self.pc.snapshot()
            if s["phase"] == "connected" and s["telemetry"] is not None:
                break
            time.sleep(0.1)

    def connected(self):
        return self.pc.snapshot()["phase"] == "connected"

    def run(self, op, args, wait):
        snap = self.pc.snapshot()
        if snap["phase"] != "connected":
            return {"result": "not-connected", "detail": f"{snap['phase']}: {snap['detail']}", "module": view(snap)}
        res = self.pc.module_command(op, args, wait_s=wait)
        return {"result": res["result"], "detail": res["error"], "module": view(self.pc.snapshot())}

    def status(self):
        return view(self.pc.snapshot())

    def close(self):
        self.pc.stop()


def expect(chan, state, timeout):
    """Опрос раз в 0,5 с до telemetry.state == state; → (достигнуто, последний M)."""
    end = time.monotonic() + timeout
    m = {}
    while True:
        try:
            m = chan.status()
        except (OSError, ValueError, ConsoleAbsent):
            m = {}
        if ((m.get("telemetry") or {}).get("state")) == state and m.get("support") == "ok" and not m.get("stale"):
            return True, m
        if time.monotonic() >= end:
            return False, m
        time.sleep(POLL_S)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m sheregesh.link.modulectl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=wire.OPS)
    ap.add_argument("params", nargs="*", help="lab: SCENARIO; configure: KEY=VALUE…")
    ap.add_argument("--via", choices=("auto", "console", "direct"), default="auto",
                    help="через работающий пульт (console), прямым сеансом (direct) или пульт, если запущен (auto)")
    ap.add_argument("--socket", default=None, help=f"сокет пульта (по умолчанию {socket_path()})")
    ap.add_argument("--link-config", default=str(Path("~/.config/sheregesh/link.json").expanduser()),
                    help="ключ канала для прямого сеанса")
    ap.add_argument("--port", type=int, default=None, help="прямой сеанс без TLS к 127.0.0.1:PORT (испытания, loopback)")
    ap.add_argument("--wait", type=float, default=None,
                    help="ждать итог команды, с (по умолчанию 15; configure — 30)")
    ap.add_argument("--expect", choices=wire.STATES, default=None, help="ждать состояние модуля после команды")
    ap.add_argument("--expect-timeout", type=float, default=15.0)
    ap.add_argument("--connect-timeout", type=float, default=10.0)
    a = ap.parse_args(argv)
    args = {}
    try:
        if a.op == "lab":
            if len(a.params) != 1 or a.params[0] not in wire.LAB_SCENARIOS:
                raise ValueError("lab needs one scenario: " + ", ".join(wire.LAB_SCENARIOS))
            args = {"scenario": a.params[0]}
        elif a.op == "configure":
            args = parse_configure(a.params)
        elif a.params:
            raise ValueError(f"{a.op} takes no parameters")
    except ValueError as e:
        ap.error(str(e))
    wait = a.wait if a.wait is not None else (30.0 if a.op == "configure" else 15.0)
    chan, out = None, None
    try:
        if a.via != "direct" and a.port is None:
            path = a.socket or socket_path()
            chan = Console(path, a.connect_timeout)
            try:
                out = {**chan.run(a.op, args, wait), "via": "console"}
            except ConsoleAbsent as e:
                chan = None
                if a.via == "console":
                    out = {"result": "not-connected", "detail": f"console not running ({e})", "via": "console",
                           "module": None}
                else:
                    print(f"modulectl: пульт не запущен ({e}) — прямой сеанс управления с блоком; работающий пульт "
                          "был бы вытеснен на 1–3 с", file=sys.stderr, flush=True)
            except (OSError, ValueError) as e:     # нет доступа к каталогу пульта, обрыв, неверный ответ
                chan = None
                out = {"result": "not-connected", "detail": f"console {path}: {e} (run as the console user)",
                       "via": "console", "module": None}
        elif a.port is None:
            print("modulectl: прямой сеанс управления — работающий пульт будет вытеснен на 1–3 с и переподключится",
                  file=sys.stderr, flush=True)
        if out is None:
            try:
                chan = Direct(a)
            except tls.LinkConfigError as e:
                out = {"result": "not-connected", "detail": f"link config: {e}", "via": "direct", "module": None}
            else:
                out = {**chan.run(a.op, args, wait), "via": "direct"}
        if a.expect and chan is not None and out["result"] == "ok":
            reached, m = expect(chan, a.expect, a.expect_timeout)
            out["module"] = m or out.get("module")
            if not reached:
                state = ((m or {}).get("telemetry") or {}).get("state")
                out.update(result="timeout", detail=f"state {state} after {a.expect_timeout:g} s, expected {a.expect}")
    finally:
        if isinstance(chan, Direct):
            chan.close()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if out["result"] == "ok":
        return 0
    return 2 if out["result"] in NO_LINK else 1


if __name__ == "__main__":
    sys.exit(main())
