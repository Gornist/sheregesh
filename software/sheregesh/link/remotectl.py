# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Обслуживание удалённого блока из командной строки пульта (протокол 1.6; стенд, Smoke Test R5/R6).

  python3 -m sheregesh.link.remotectl log-export [--wait 60]     журнал блока на «USB» пульта
  python3 -m sheregesh.link.remotectl shutdown --confirm         штатное выключение блока

Только через работающий пульт (локальный канал, {"cmd": "remote", …}): пульт исполняет те же слоты, что кнопки
«Журналы» → «Выгрузить журнал блока» и «Настройки» → «Выключить удалённый блок…», и пишет событие в свой журнал.
Запускать от пользователя пульта (sheregesh-ui). Вывод — JSON {"result", "detail", "file"}; код выхода: 0 — ok,
1 — отказ или таймаут, 2 — пульта нет или неверные аргументы."""
import argparse
import json
import sys

from .videoselect import ConsoleAbsent, ask_console, socket_path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="remotectl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["log-export", "shutdown"])
    ap.add_argument("--confirm", action="store_true", help="подтверждение выключения (обязательно для shutdown)")
    ap.add_argument("--wait", type=float, default=60.0)
    ap.add_argument("--socket", default=None, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.op == "shutdown" and not a.confirm:
        print(json.dumps({"result": "not-confirmed", "detail": "shutdown requires --confirm", "file": ""}))
        return 2
    req = {"cmd": "remote", "op": a.op, "wait": a.wait, **({"confirmed": True} if a.op == "shutdown" else {})}
    try:
        r = ask_console(a.socket or socket_path(), req, a.wait + 10)
    except ConsoleAbsent as e:
        print(json.dumps({"result": "no-console", "detail": str(e), "file": ""}))
        return 2
    except OSError as e:
        print(json.dumps({"result": "failed", "detail": str(e), "file": ""}))
        return 2
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r.get("result") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
