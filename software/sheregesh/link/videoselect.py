# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Выбор видеоисточника блока с пульта из командной строки (стенд, приёмка) — тем же путём, что GUI.

  python3 -m sheregesh.link.videoselect --list
  python3 -m sheregesh.link.videoselect synthetic | rtsp:0 | v4l2:534d:2109 [--wait 15]

Блок держит один сеанс управления: новое подключение заменяет прежнее. Поэтому команда по умолчанию НЕ открывает
свой сеанс, а передаёт запрос работающему пульту по локальному каналу (unix-сокет в каталоге выполнения пользователя
пульта: $XDG_RUNTIME_DIR/sheregesh-console.sock, у службы — /run/sheregesh-ui, права 0700/0600;
sheregesh_console.control). Пульт выполняет Backend.selectVideoSource(id, True) — как кнопка экрана «Видео» —
в своём сеансе; единственный источник истины — блок (video-input.json, status.video.sources/active).
Запускать от пользователя пульта (sheregesh-ui):  sudo -u sheregesh-ui python3 -m sheregesh.link.videoselect …

--via auto (по умолчанию): пульт, если его канал есть; пульт не запущен — прямой сеанс PeerClient → video-select
(протокол 1.2) с предупреждением в stderr. --via console — только через пульт; --via direct — только прямой сеанс
(нужен ключ канала, --link-config; работающий пульт будет вытеснен на 1–3 с и переподключится сам).
Вывод — JSON: {"result": "ok"|<код отказа>, "detail", "via": "console"|"direct", "active", "source", "sources"}
(через пульт — ещё "display": что показывает экран «Видео»).
Код выхода: 0 — источник выбран и стал active; 1 — отказ (unknown, unavailable, unsupported, busy) или не стал
active за --wait; 2 — нет связи / пульта или неверные аргументы."""
import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

from . import tls
from .client import VIDEO_SOURCE_ID, PeerClient

CONSOLE_SOCKET = "sheregesh-console.sock"
CONSOLE_RUN_DIR = "/run/sheregesh-ui"            # RuntimeDirectory службы sheregesh-console (0700, sheregesh-ui)


def socket_candidates(env=None):
    """Где пульт мог открыть канал: SHEREGESH_CONSOLE_CONTROL (только он, если задан), иначе $XDG_RUNTIME_DIR,
    каталог службы и /run/user/<uid> (pam_systemd службы с PAMName может заменить XDG_RUNTIME_DIR)."""
    env = os.environ if env is None else env
    if env.get("SHEREGESH_CONSOLE_CONTROL"):
        return [env["SHEREGESH_CONSOLE_CONTROL"]]
    dirs = [env.get("XDG_RUNTIME_DIR"), CONSOLE_RUN_DIR, f"/run/user/{os.getuid()}"]
    return list(dict.fromkeys(os.path.join(d, CONSOLE_SOCKET) for d in dirs if d))


def socket_path(env=None):
    """Сокет пульта для команды: первый существующий из socket_candidates (или в каталоге без доступа — тогда команда
    сообщит «нет доступа», а не откроет прямой сеанс в обход работающего пульта), иначе первый из них."""
    paths = socket_candidates(env)
    for p in paths:
        d = os.path.dirname(p)
        if os.path.exists(p) or (os.path.isdir(d) and not os.access(d, os.X_OK)):
            return p
    return paths[0]


class ConsoleAbsent(Exception):
    """Пульт не запущен: сокета нет или никто не слушает."""


def ask_console(path, request, timeout):
    """Запрос работающему пульту → ответ (dict). ConsoleAbsent — пульта нет; OSError — нет доступа / обрыв."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    with s:
        try:
            s.connect(path)
        except (FileNotFoundError, ConnectionRefusedError) as e:
            raise ConsoleAbsent(f"{path}: {e.strerror}") from e
        s.sendall((json.dumps(request) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    reply = json.loads(buf.decode("utf-8") or "null")
    if not isinstance(reply, dict) or not isinstance(reply.get("result"), str):
        raise ValueError("bad reply from console")
    return reply


def _video(pc):
    return (pc.snapshot()["telemetry"] or {}).get("video") or {}


def direct(a):
    """Прямой сеанс управления (как до S5): PeerClient → video-select. Вытесняет сеанс работающего пульта."""
    try:
        cfg = tls.LinkConfig.load(a.link_config)
    except tls.LinkConfigError as e:
        return {"result": "not-connected", "detail": f"link config: {e}"}
    pc = PeerClient(None, None, heartbeat_timeout_s=3.0, tls_cfg=cfg).start()
    try:
        end = time.monotonic() + a.connect_timeout
        while time.monotonic() < end and not (pc.snapshot()["phase"] == "connected" and _video(pc).get("sources")):
            time.sleep(0.1)
        snap = pc.snapshot()
        if snap["phase"] != "connected":
            return {"result": "not-connected", "detail": f"{snap['phase']}: {snap['detail']}"}
        out = {"result": "ok", "detail": ""}
        if not a.list:
            res = pc.select_video_source(a.source_id, wait_s=min(5.0, a.wait))
            if res is not None:
                out.update(result=res["error"], detail=res.get("detail", ""))
            else:
                end = time.monotonic() + a.wait
                while time.monotonic() < end and a.source_id not in \
                        [e["id"] for e in _video(pc).get("sources", []) if e["active"]]:
                    time.sleep(0.1)
                if a.source_id not in [e["id"] for e in _video(pc).get("sources", []) if e["active"]]:
                    out.update(result="timeout", detail=f"{a.source_id} not active after {a.wait:g} s")
        v = _video(pc)
        out.update(active=[e["id"] for e in v.get("sources", []) if e["active"]], source=v.get("source"),
                   sources=v.get("sources", []))
        return out
    finally:
        pc.stop()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m sheregesh.link.videoselect", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source_id", nargs="?", help="id из status.video.sources")
    ap.add_argument("--list", action="store_true", help="только показать список источников блока")
    ap.add_argument("--via", choices=("auto", "console", "direct"), default="auto",
                    help="через работающий пульт (console), прямым сеансом (direct) или пульт, если запущен (auto)")
    ap.add_argument("--socket", default=None, help=f"сокет пульта (по умолчанию {socket_path()})")
    ap.add_argument("--link-config", default=str(Path("~/.config/sheregesh/link.json").expanduser()),
                    help="ключ канала для прямого сеанса")
    ap.add_argument("--wait", type=float, default=15.0, help="ждать active у выбранного источника, с")
    ap.add_argument("--connect-timeout", type=float, default=10.0)
    a = ap.parse_args(argv)
    if not a.list and not a.source_id:
        ap.error("нужен id источника или --list")
    if a.source_id is not None and not VIDEO_SOURCE_ID.fullmatch(a.source_id):
        ap.error(f"неверный id источника {a.source_id!r}")
    out = None
    if a.via != "direct":
        path = a.socket or socket_path()
        req = {"cmd": "list"} if a.list else {"cmd": "select", "id": a.source_id, "wait": a.wait}
        try:
            out = {**ask_console(path, req, a.wait + a.connect_timeout), "via": "console"}
        except ConsoleAbsent as e:
            if a.via == "console":
                out = {"result": "not-connected", "detail": f"console not running ({e})", "via": "console"}
            else:
                print(f"videoselect: пульт не запущен ({e}) — прямой сеанс управления с блоком; работающий пульт "
                      "был бы вытеснен на 1–3 с", file=sys.stderr, flush=True)
        except (OSError, ValueError) as e:          # нет доступа к каталогу пульта, обрыв, неверный ответ
            out = {"result": "not-connected", "detail": f"console {path}: {e} (run as the console user)",
                   "via": "console"}
    else:
        print("videoselect: прямой сеанс управления — работающий пульт будет вытеснен на 1–3 с и переподключится",
              file=sys.stderr, flush=True)
    if out is None:
        out = {**direct(a), "via": "direct"}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if out["result"] == "ok":
        return 0
    return 2 if out["result"] in ("not-connected", "no-link", "bad-request") else 1


if __name__ == "__main__":
    sys.exit(main())
