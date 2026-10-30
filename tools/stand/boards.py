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
"""
import json
import os
import subprocess
import sys
from pathlib import Path

BOARDS = {"pi1": {"serial": "10000000568152de", "mac": "d8:3a:dd:69:0e:90", "role": "пульт"},
          "pi2": {"serial": "100000009d497e91", "mac": "d8:3a:dd:69:0f:6e", "role": "удалённый блок"}}
ARG_BOARD = {"console": "pi1", "remote": "pi2"}
AUTO = "auto"
USER = "sheregesh-ui"
CACHE = Path.home() / ".cache/sheregesh/boards.json"
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
        raise BoardNotFound(f"неизвестная плата {board!r}: pi1 или pi2")
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
    raise BoardNotFound(f"{board} ({BOARDS[board]['role']}, серийный {want}) не найдена"
                        + (f"; проверены {', '.join(tried)}" if tried else ""))


def add_args(ap, console=True, remote=True):
    if console:
        ap.add_argument("--console", default=AUTO, help="адрес пульта pi1; auto — найти по серийному номеру")
    if remote:
        ap.add_argument("--remote", default=AUTO, help="адрес блока pi2; auto — найти по серийному номеру")


def resolve_args(a, mapping=None):
    """Заменить в разобранных аргументах адреса плат проверенными (auto — поиск). Не найдена — SystemExit."""
    for attr, board in (mapping or ARG_BOARD).items():
        if hasattr(a, attr):
            try:
                setattr(a, attr, resolve(board, getattr(a, attr)))
            except BoardNotFound as e:
                raise SystemExit(f"{attr}: {e}") from None
    return a
