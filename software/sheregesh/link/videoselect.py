# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Выбор видеоисточника блока с пульта из командной строки (стенд, приёмка S4) — тем же путём, что GUI:
PeerClient → video-select (протокол 1.2) → блок проверяет id, переключает и сохраняет выбор.

  python3 -m sheregesh.link.videoselect [--link-config ~/.config/sheregesh/link.json] --list
  python3 -m sheregesh.link.videoselect [--link-config …] synthetic | rtsp:0 | v4l2:534d:2109 [--wait 15]

Запускать на пульте от пользователя, которому доступен ключ канала (sheregesh-ui). Блок держит один сеанс управления:
подключение команды на 1–3 с заменяет сеанс GUI, GUI переподключается сам (выбор на блоке сохраняется).
Вывод — JSON: {"result": "ok"|<код отказа>, "detail", "active", "source": {...}, "sources": [...]}.
Код выхода: 0 — источник выбран и стал active; 1 — отказ блока (unknown, unavailable, unsupported) или не стал
active за --wait; 2 — нет связи или неверные аргументы."""
import argparse
import json
import sys
import time
from pathlib import Path

from . import tls
from .client import PeerClient


def _video(pc):
    return (pc.snapshot()["telemetry"] or {}).get("video") or {}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m sheregesh.link.videoselect", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source_id", nargs="?", help="id из status.video.sources")
    ap.add_argument("--list", action="store_true", help="только показать список источников блока")
    ap.add_argument("--link-config", default=str(Path("~/.config/sheregesh/link.json").expanduser()))
    ap.add_argument("--wait", type=float, default=15.0, help="ждать active у выбранного источника, с")
    ap.add_argument("--connect-timeout", type=float, default=10.0)
    a = ap.parse_args(argv)
    if not a.list and not a.source_id:
        ap.error("нужен id источника или --list")
    try:
        cfg = tls.LinkConfig.load(a.link_config)
    except tls.LinkConfigError as e:
        print(json.dumps({"result": "not-connected", "detail": f"link config: {e}"}, ensure_ascii=False))
        return 2
    pc = PeerClient(None, None, heartbeat_timeout_s=3.0, tls_cfg=cfg).start()
    try:
        end = time.monotonic() + a.connect_timeout
        while time.monotonic() < end and not (pc.snapshot()["phase"] == "connected" and _video(pc).get("sources")):
            time.sleep(0.1)
        snap = pc.snapshot()
        if snap["phase"] != "connected":
            print(json.dumps({"result": "not-connected", "detail": f"{snap['phase']}: {snap['detail']}"},
                             ensure_ascii=False))
            return 2
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
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0 if out["result"] == "ok" else 1
    finally:
        pc.stop()


if __name__ == "__main__":
    sys.exit(main())
