# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Адрес второй платы сеанса для chrony (ADR-0017): `systemctl start sheregesh-time-peer@<адрес>.service`.

Пульт вызывает при подключении сеанса mTLS (адрес блока), агент блока — после совместимого hello (адрес пульта).
Помощник sheregesh-time (root) на пульте разрешает блоку запросы NTP, на блоке делает пульт источником времени.
Вызов блокирующий (до 10 с) — из рабочего потока, не из цикла событий и не из потока интерфейса. Ошибка не мешает
сеансу: время — вспомогательное, итог только в журнал. Loopback и не-адреса не передаются (эмуляция в процессе)."""
import ipaddress
import os
import subprocess

SYSTEMCTL_ENV = "SHEREGESH_SYSTEMCTL"          # испытания: подменная команда вместо systemctl (как videotx)
TIMEOUT_S = 10.0


def instance(addr) -> str | None:
    """Строгий IPv4/IPv6 для имени экземпляра; None — не передавать (нет адреса, loopback, мусор)."""
    try:
        a = ipaddress.ip_address(str(addr or "").split("%")[0])
    except ValueError:
        return None
    return None if a.is_loopback or a.is_unspecified or a.is_multicast else a.compressed


def start(addr, run=subprocess.run) -> tuple[bool, str]:
    """(успех, пояснение). Без исключений: сеанс от времени не зависит."""
    inst = instance(addr)
    if inst is None:
        return False, f"не адрес второй платы: {str(addr)[:64]!r}"
    cmd = os.environ.get(SYSTEMCTL_ENV) or "systemctl"
    try:
        r = run([cmd, "start", f"sheregesh-time-peer@{inst}.service"], capture_output=True, text=True,
                timeout=TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"systemctl не выполнен: {type(e).__name__}"
    if r.returncode != 0:
        return False, f"systemctl код {r.returncode}: {(r.stderr or '').strip()[:160]}"
    return True, inst
