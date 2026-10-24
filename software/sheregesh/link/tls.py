# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Сетевой лабораторный профиль SHRG-LAB (S3.1, ADR-0007): TLS 1.3 с взаимной аутентификацией устройств.

Идентичность — в сертификате, не в IP-адресе (адреса выдаёт DHCP): SAN URI «urn:sheregesh:<роль>:<device_id>»,
device_id — серийный номер платы. Каждая сторона проверяет цепочку до закреплённого CA стенда (только он, без
системных корней) и точное совпадение URI второй стороны. Приватный ключ создаётся на самой плате и не покидает её
(sheregesh.link.pki); в Git, CI и образы ключи не попадают.

Конфигурация — JSON (по умолчанию ~/.config/sheregesh/link.json):
  {"role": "console", "device_id": "10000000568152de", "peer_id": "remote:100000009d497e91",
   "dir": "~/.local/share/sheregesh/link", "port": 7443, "video_port": 7444,
   "peer_hosts": ["192.168.1.169"], "discover": "auto"}
Без конфигурации и файлов ключей сетевой режим не включается — остаётся loopback (127.0.0.1).
"""
import ipaddress
import json
import os
import ssl
import stat
from dataclasses import dataclass, field
from pathlib import Path

URI_PREFIX = "urn:sheregesh:"
ROLES = ("console", "remote")
DEFAULT_CONFIG = Path("~/.config/sheregesh/link.json")
DEFAULT_DIR = Path("~/.local/share/sheregesh/link")
HANDSHAKE_TIMEOUT_S = 5.0


class LinkConfigError(Exception):
    """Сетевой профиль не настроен или настроен небезопасно — сетевой режим не включается."""


def device_uri(role: str, device_id: str) -> str:
    return f"{URI_PREFIX}{role}:{device_id}"


def is_lab_address(addr: str) -> bool:
    """Только локальный лабораторный контур: loopback, частные сети RFC 1918/ULA и link-local; не интернет."""
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_link_local or (ip.is_private and not ip.is_global)


@dataclass(frozen=True)
class LinkConfig:
    role: str
    device_id: str
    peer_id: str                       # «<роль>:<device_id>» второй стороны
    dir: Path
    port: int = 7443
    video_port: int = 7444
    video: bool = True                  # защищённый видеоканал (S3.1-VIDEO, ADR-0011)
    peer_hosts: tuple = ()
    discover: str | None = None        # "auto" — подсеть eth0; CIDR — явно; None — не искать
    source: Path | None = field(default=None, compare=False)

    @property
    def ca(self):
        return self.dir / "ca.crt"

    @property
    def cert(self):
        return self.dir / "device.crt"

    @property
    def key(self):
        return self.dir / "device.key"

    @property
    def own_uri(self):
        return device_uri(self.role, self.device_id)

    @property
    def peer_uri(self):
        return URI_PREFIX + self.peer_id

    @classmethod
    def load(cls, path=DEFAULT_CONFIG):
        path = Path(path).expanduser()
        if not path.is_file():
            raise LinkConfigError(f"нет конфигурации сетевого профиля {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise LinkConfigError(f"{path}: не JSON: {e}") from None
        try:
            cfg = cls(role=raw["role"], device_id=str(raw["device_id"]), peer_id=str(raw["peer_id"]),
                      dir=Path(raw.get("dir", str(DEFAULT_DIR))).expanduser(), port=int(raw.get("port", 7443)),
                      video_port=int(raw.get("video_port", 7444)), video=bool(raw.get("video", True)),
                      peer_hosts=tuple(str(h) for h in raw.get("peer_hosts", [])), discover=raw.get("discover"),
                      source=path)
        except (KeyError, TypeError, ValueError) as e:
            raise LinkConfigError(f"{path}: неверное поле {e}") from None
        cfg.validate()
        return cfg

    def validate(self):
        if self.role not in ROLES:
            raise LinkConfigError(f"роль {self.role!r}: ожидается {ROLES}")
        peer_role = self.peer_id.split(":", 1)[0]
        if peer_role not in ROLES or peer_role == self.role or ":" not in self.peer_id:
            raise LinkConfigError(f"peer_id {self.peer_id!r}: ожидается «<другая роль>:<device_id>»")
        if not (1024 <= self.port <= 65535 and 1024 <= self.video_port <= 65535):
            raise LinkConfigError("порт вне 1024…65535")
        for h in self.peer_hosts:
            if _looks_like_ip(h) and not is_lab_address(h):
                raise LinkConfigError(f"адрес второй стороны {h} вне лабораторного контура")
        for f in (self.ca, self.cert, self.key):
            if not f.is_file():
                raise LinkConfigError(f"нет файла {f}: ключи и сертификаты не настроены (sheregesh-link keygen/install)")
        mode = stat.S_IMODE(self.key.stat().st_mode)
        if mode & 0o077:
            raise LinkConfigError(f"{self.key}: права {oct(mode)} — ключ доступен другим (нужно 0600)")
        own = cert_uris(self.cert)
        if self.own_uri not in own:
            raise LinkConfigError(f"{self.cert}: в сертификате {own}, ожидается {self.own_uri}")


def _looks_like_ip(h):
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def cert_info(path) -> dict:
    """Разбор PEM-сертификата средствами модуля ssl (без сторонних библиотек)."""
    return ssl._ssl._test_decode_cert(str(path))         # noqa: SLF001 — стабильный внутренний API CPython


def cert_uris(path_or_dict) -> list[str]:
    info = path_or_dict if isinstance(path_or_dict, dict) else cert_info(path_or_dict)
    return [v for k, v in info.get("subjectAltName", ()) if k == "URI"]


def make_context(cfg: LinkConfig, server: bool) -> ssl.SSLContext:
    """TLS 1.3 только; взаимная проверка сертификатов; доверие — только CA стенда."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = False                 # имя хоста по DHCP не стабильно: проверяется URI устройства
    ctx.verify_flags |= ssl.VERIFY_X509_STRICT
    ctx.load_verify_locations(cafile=str(cfg.ca))
    ctx.load_cert_chain(str(cfg.cert), str(cfg.key))
    return ctx


def check_peer(peercert: dict | None, expected_uri: str) -> str | None:
    """None — вторая сторона та, что ожидается; иначе причина отказа."""
    if not peercert:
        return "no-certificate"
    uris = cert_uris(peercert)
    if expected_uri not in uris:
        return f"identity {','.join(uris) or '-'} != {expected_uri}"
    return None


def describe_ssl_error(e: Exception) -> str:
    """Короткая причина отказа TLS для журнала и интерфейса (без содержимого ключей)."""
    if isinstance(e, ssl.SSLCertVerificationError):
        return f"verify: {e.verify_message}"
    reason = getattr(e, "reason", None) or type(e).__name__
    return str(reason).lower().replace("_", "-")


def secure_umask():
    """Создаваемые файлы ключей недоступны группе и прочим."""
    return os.umask(0o077)


def local_networks() -> list:
    """IPv4-сети интерфейсов (кроме lo) по ioctl SIOCGIFADDR/SIOCGIFNETMASK; вне Linux — пусто."""
    import fcntl
    import socket
    import struct
    nets = []
    try:
        names = [n for _, n in socket.if_nameindex() if n != "lo"]
    except OSError:
        return nets
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for name in names:
            req = struct.pack("256s", name.encode()[:15])
            try:
                addr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, req)[20:24])      # SIOCGIFADDR
                mask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x891B, req)[20:24])      # SIOCGIFNETMASK
            except OSError:
                continue
            nets.append(ipaddress.ip_interface(f"{addr}/{mask}"))
    return nets


def peer_allowed(addr: str, nets=None) -> bool:
    """Входящее соединение: адрес лабораторного контура и (если сети интерфейсов известны) из их подсети."""
    if not is_lab_address(addr):
        return False
    ip = ipaddress.ip_address(addr.split("%")[0])
    if ip.is_loopback:
        return True
    nets = local_networks() if nets is None else nets
    return not nets or any(ip in n.network for n in nets)
