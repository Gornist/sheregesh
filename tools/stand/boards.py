# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Платы стенда: адрес по серийному номеру и единый вызов SSH — для shdeploy и всех скриптов приёмки.

Адреса плат меняются (DHCP), поэтому ни один скрипт не держит их по умолчанию: значение «auto» (и пустое) ищется,
явный адрес проверяется. Порядок кандидатов: явный адрес → SHEREGESH_PI1_HOST / SHEREGESH_PI2_HOST → кэш
~/.cache/sheregesh/boards.json → поиск по MAC в SHEREGESH_LAN (по умолчанию 192.168.1.0/24). Принимается только адрес,
где /proc/device-tree/serial-number = серийный номер платы — DHCP не подменит пульт блоком; найденный адрес — в кэш.

  from stand import boards
  boards.add_args(ap)            # --console / --remote со значением по умолчанию «auto»
  boards.resolve_args(a)         # после parse_args: a.console, a.remote — проверенные адреса
  rc, out, err = boards.ssh(host, "uptime")                 # sheregesh-ui; user="root" — DEV-образ

Реестр плат — локальный файл ~/.config/sheregesh/boards.json (или SHEREGESH_BOARDS): имя → серийный номер, MAC, роль.
Свои платы добавляются командой (серийный номер и MAC читаются с платы по SSH; labpki pair делает это сам):

  python3 tools/stand/boards.py add pi1 --host 192.168.1.50 --role console
  python3 tools/stand/boards.py add pi2 --host 192.168.1.51 --role remote
  python3 tools/stand/boards.py list
Без файла действуют платы эталонного стенда проекта (REFERENCE) — у чужого стенда они не найдутся, и сообщение
об ошибке подскажет команду add.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROLE_TEXT = {"console": "пульт", "remote": "удалённый блок"}
# Эталонный стенд проекта (Raspberry Pi 4: pi1 — пульт, pi2 — блок): действует, пока нет своего реестра
REFERENCE = {"pi1": {"serial": "10000000568152de", "mac": "d8:3a:dd:69:0e:90", "kind": "console"},
             "pi2": {"serial": "100000009d497e91", "mac": "d8:3a:dd:69:0f:6e", "kind": "remote"}}
REGISTRY = Path(os.environ.get("SHEREGESH_BOARDS") or Path.home() / ".config/sheregesh/boards.json")
AUTO = "auto"
USER = "sheregesh-ui"
CACHE = Path.home() / ".cache/sheregesh/boards.json"


def load(path=None):
    """Платы из реестра {имя: {serial, mac, kind}}; нет файла — эталонный стенд. Поле role — текстом для сообщений."""
    try:
        data = json.loads(Path(path or REGISTRY).read_text())
        source = "registry"
    except (OSError, ValueError):
        data, source = REFERENCE, "reference"
    out = {}
    for name, b in data.items():
        kind = b.get("kind") if b.get("kind") in ROLE_TEXT else "console"
        out[name] = {"serial": str(b["serial"]), "mac": str(b.get("mac", "")).lower(), "kind": kind,
                     "role": ROLE_TEXT[kind], "source": source}
    return out


BOARDS = load()


def arg_board(boards_=None):
    """--console / --remote → имя платы: первая плата реестра с этой ролью (pi1 / pi2 на эталонном стенде)."""
    boards_ = boards_ or BOARDS
    out = {}
    for kind in ("console", "remote"):
        out[kind] = next((n for n, b in sorted(boards_.items()) if b["kind"] == kind), "pi1" if kind == "console" else "pi2")
    return out


ARG_BOARD = arg_board()
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new"]
SERIAL_CMD = "tr -d '\\0' < /proc/device-tree/serial-number"


class BoardNotFound(Exception):
    pass


def ssh(host, cmd, user=USER, timeout=60, data=None):
    """(код, stdout, stderr) текстом. Сбой SSH или тайм-аут — код ≠ 0, а не исключение: решает вызывающий."""
    try:
        r = subprocess.run(["ssh", *SSH_OPTS, f"{user}@{host}", cmd], input=data, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"тайм-аут {timeout} с"


def _cache(path=None):
    try:
        return json.loads((path or CACHE).read_text())
    except (OSError, ValueError):
        return {}


def _remember(board, host, path=None):
    path = path or CACHE
    c = _cache(path)
    c[board] = host
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(c, indent=1))


def resolve(board, host=None, run=None, finder=None, cache=None):
    """Проверенный адрес платы pi1 | pi2. run(host, cmd) → (код, вывод) и finder(mac, net) — подмена в испытаниях;
    cache — файл кэша (по умолчанию CACHE)."""
    if board not in BOARDS:
        raise BoardNotFound(f"неизвестная плата {board!r} (в реестре: {', '.join(sorted(BOARDS)) or 'пусто'}); "
                            f"добавить: python3 tools/stand/boards.py add {board} --host АДРЕС --role console|remote")
    if host == AUTO:
        host = None
    run = run or (lambda h, cmd: ssh(h, cmd, timeout=30)[:2])
    want = BOARDS[board]["serial"]
    tried = []
    for source in (("arg",) if host else ("env", "cache", "mac")):     # явный адрес — только он
        if source == "arg":
            h = host
        elif source == "env":
            h = os.environ.get(f"SHEREGESH_{board.upper()}_HOST")
        elif source == "cache":
            h = _cache(cache).get(board)
        else:
            if finder is None:
                sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "probe"))
                import collect_boot
                finder = collect_boot.find
            net = os.environ.get("SHEREGESH_LAN", "192.168.1.0/24")
            print(f"{board}: поиск по MAC {BOARDS[board]['mac']} в {net}…", file=sys.stderr)
            h = finder(BOARDS[board]["mac"], net)
        if not h or h in tried:
            continue
        tried.append(h)
        rc, out = run(h, SERIAL_CMD)
        if rc == 0 and out.strip() == want:
            _remember(board, h, cache)
            return h
        if rc == 0:
            print(f"{board}: {h} — другая плата (серийный {out.strip()}), пропуск", file=sys.stderr)
    hint = ("; это плата эталонного стенда проекта — свои платы добавьте: python3 tools/stand/boards.py add "
            f"{board} --host АДРЕС --role {BOARDS[board]['kind']}") if BOARDS[board].get("source") == "reference" else ""
    raise BoardNotFound(f"{board} ({BOARDS[board]['role']}, серийный {want}) не найдена"
                        + (f"; проверены {', '.join(tried)}" if tried else "") + hint)


MAC_CMD = "cat /sys/class/net/eth0/address"


def register(name, host, kind, path=None, run=None):
    """Записать плату в реестр: серийный номер и MAC читаются с платы по SSH. → запись реестра."""
    if kind not in ROLE_TEXT:
        raise BoardNotFound(f"роль {kind!r}: console или remote")
    run = run or (lambda h, cmd: ssh(h, cmd, timeout=30)[:2])
    rc, serial = run(host, SERIAL_CMD)
    if rc != 0 or not serial.strip():
        raise BoardNotFound(f"{host}: серийный номер не прочитан (SSH sheregesh-ui по ключу работает?)")
    rc, mac = run(host, MAC_CMD)
    path = Path(path or REGISTRY)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    for other, b in data.items():
        if other != name and b.get("serial") == serial.strip():
            raise BoardNotFound(f"плата {serial.strip()} уже записана как {other}")
    data[name] = {"serial": serial.strip(), "mac": mac.strip().lower() if rc == 0 else "", "kind": kind}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    BOARDS.clear()
    BOARDS.update(load(path))
    ARG_BOARD.clear()
    ARG_BOARD.update(arg_board())
    _remember(name, host)
    return data[name]


def add_args(ap, console=True, remote=True):
    if console:
        ap.add_argument("--console", default=AUTO, help="адрес пульта; auto — найти по серийному номеру из реестра")
    if remote:
        ap.add_argument("--remote", default=AUTO, help="адрес блока; auto — найти по серийному номеру из реестра")


def resolve_args(a, mapping=None):
    """Заменить в разобранных аргументах адреса плат проверенными (auto — поиск). Не найдена — SystemExit."""
    for attr, board in (mapping or ARG_BOARD).items():
        if hasattr(a, attr):
            try:
                setattr(a, attr, resolve(board, getattr(a, attr)))
            except BoardNotFound as e:
                raise SystemExit(f"{attr}: {e}") from None
    return a


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="boards.py", description="Реестр плат стенда (~/.config/sheregesh/boards.json).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a_ = sub.add_parser("add", help="добавить или заменить плату: серийный номер и MAC — с платы по SSH")
    a_.add_argument("name", help="имя платы, например pi1")
    a_.add_argument("--host", required=True, help="текущий адрес платы")
    a_.add_argument("--role", required=True, choices=sorted(ROLE_TEXT), help="console — пульт, remote — блок")
    sub.add_parser("list", help="платы реестра и откуда они")
    f = sub.add_parser("find", help="найти и проверить адрес платы")
    f.add_argument("name")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "add":
            b = register(a.name, a.host, a.role)
            print(f"{a.name}: {ROLE_TEXT[b['kind']]}, серийный {b['serial']}, MAC {b['mac'] or '—'} → {REGISTRY}")
        elif a.cmd == "list":
            for n, b in sorted(BOARDS.items()):
                print(f"{n}\t{b['role']}\t{b['serial']}\t{b['mac'] or '—'}\t"
                      f"{'реестр' if b['source'] == 'registry' else 'эталонный стенд проекта'}")
        else:
            print(resolve(a.name))
    except BoardNotFound as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
