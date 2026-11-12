# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Поиск второй стороны канала в подсети (сетевой профиль, этап 7.3): открытый порт → TLS → URI из конфигурации.

Кэш найденного адреса — peer-cache.json в каталоге ключей устройства: при следующем запуске адрес пробуется первым.
Проверка TLS и URI — у клиента канала (verify(raw, host) → TLS-сокет или None), здесь только перебор адресов."""
import ipaddress
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor

from . import tls

MAX_HOSTS = 1024
PROBE_TIMEOUT_S = 0.4
CONNECT_TIMEOUT_S = 2.0
DEADLINE_S = 20.0           # весь поиск (пробы портов и проверка TLS) — не дольше
CACHE_MAX_AGE_S = 30 * 86400   # кэш адреса старше месяца не используется


def candidates(spec, networks=None):
    """Адреса для перебора: discover=CIDR или (auto) подсети интерфейсов с префиксом ≥ /22, не больше MAX_HOSTS."""
    nets = [ipaddress.ip_network(spec, strict=False)] if spec and spec != "auto" else \
        [n.network for n in (tls.local_networks() if networks is None else networks) if n.network.prefixlen >= 22]
    return [str(ip) for net in nets for ip in net.hosts()][:MAX_HOSTS]


def discover(spec, port, verify, connect=socket.create_connection, deadline_s=DEADLINE_S, stop=None,
             clock=time.monotonic):
    """Первый адрес, где порт открыт и verify(сокет, адрес) подтвердил вторую сторону; None — не найдено, время
    deadline_s вышло или stop() вернул True. Вызывается из отдельного потока (PeerClient), не из цикла канала."""
    hosts = candidates(spec)
    if not hosts:
        return None
    end = clock() + deadline_s

    def over():
        return clock() >= end or (stop is not None and stop())

    def probe(h):
        if over():
            return None
        try:
            with connect((h, port), timeout=PROBE_TIMEOUT_S):
                return h
        except OSError:
            return None
    with ThreadPoolExecutor(64) as ex:
        found = [h for h in ex.map(probe, hosts) if h]
    for h in found:
        if over():
            return None
        try:
            raw = connect((h, port), timeout=CONNECT_TIMEOUT_S)
        except OSError:
            continue
        sock = verify(raw, h)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
            return h
    return None


def read_cache(cfg):
    try:
        d = json.loads((cfg.dir / "peer-cache.json").read_text())
        t = d.get("time")
        if isinstance(t, (int, float)) and time.time() - t > CACHE_MAX_AGE_S:
            return None
        return d.get("host")
    except (OSError, ValueError, AttributeError):
        return None


def write_cache(cfg, host):
    try:
        (cfg.dir / "peer-cache.json").write_text(json.dumps({"host": host, "time": time.time()}))
    except OSError:
        pass
