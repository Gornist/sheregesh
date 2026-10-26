# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Кадры CRSF (ADR-0014, раздел 3.1): CRC8, сборка и разбор, потоковый разборщик.

Формат кадра (спецификация CRSF, tbs-fpv/tbs-crsf-spec): [синхробайт/адрес, длина, тип, полезная нагрузка, CRC8].
Длина считает тип, нагрузку и CRC (2…62), CRC8 — полином 0xD5, начальное значение 0, по типу и нагрузке. Многобайтовые
поля — big endian, кроме упаковки каналов 0x16 (биты подряд от младшего). Типы от 0x28 — расширенные: нагрузка
начинается с адресата и отправителя.

Каналы: 16 × 11 бит, 172…1811, центр 992. Оси пульта −1000…1000 переводятся как 992 ± 820 с округлением от нуля и
ограничением 172…1811 (+1000 → 1811). Байт состояния после каналов (ELRS 4.x, длина кадра 25): бит 0 — «взведено»;
бит 1 («взведение по CH5») не ставится. Без байта (armed=None) — длина 24, как в спецификации.
"""
from dataclasses import dataclass

# адреса устройств (спецификация CRSF)
ADDR_BROADCAST = 0x00
ADDR_FC = 0xC8                 # полётный контроллер; он же синхробайт последовательной линии
ADDR_RADIO = 0xEA              # пульт (хост модуля)
ADDR_RX = 0xEC                 # приёмник
ADDR_TX = 0xEE                 # передающий модуль
SYNC = ADDR_FC

# типы кадров
T_LINK_STATS = 0x14
T_CHANNELS = 0x16
T_PING = 0x28
T_DEVICE_INFO = 0x29
T_RADIO = 0x3A
SUB_TIMING = 0x10              # 0x3A.0x10 — синхронизация (CRSFShot)

MIN_LEN, MAX_LEN = 2, 62       # поле длины
MAX_FRAME = MAX_LEN + 2        # 64 байта с синхробайтом и длиной

CH_MIN, CH_CENTER, CH_MAX = 172, 992, 1811
CH_SCALE = 820                 # отсчётов на 1000 единиц оси
CHANNELS = 16
STATUS_ARMED = 0x01

# мощность из 0x14 (индекс → мВт), спецификация CRSF
TX_POWER_MW = (0, 10, 25, 100, 500, 1000, 2000, 250, 50)

# синхробайты, которые принимает хост (как EdgeTX: 0xC8 и 0xEA) и модуль (кадры хоста идут с 0xEE)
HOST_RX_SYNCS = frozenset((ADDR_FC, ADDR_RADIO))
MODULE_RX_SYNCS = frozenset((ADDR_TX, ADDR_FC, ADDR_RADIO))


def _crc_table(poly):
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = ((c << 1) ^ poly) & 0xFF if c & 0x80 else (c << 1) & 0xFF
        table.append(c)
    return bytes(table)


_CRC_D5 = _crc_table(0xD5)


def crc8(data, crc: int = 0) -> int:
    """CRC8 с полиномом 0xD5 (x⁷+x⁶+x⁴+x²+1), начальное значение 0."""
    for b in data:
        crc = _CRC_D5[crc ^ b]
    return crc


class BadFrame(ValueError):
    pass


@dataclass(frozen=True)
class Frame:
    sync: int                  # первый байт: синхробайт 0xC8 или адрес
    type: int
    payload: bytes             # без типа и CRC

    @property
    def extended(self) -> bool:
        return self.type >= T_PING

    @property
    def dest(self):
        return self.payload[0] if self.extended and len(self.payload) >= 2 else None

    @property
    def origin(self):
        return self.payload[1] if self.extended and len(self.payload) >= 2 else None

    def encode(self) -> bytes:
        return build(self.type, self.payload, self.sync)


def build(ftype: int, payload: bytes = b"", sync: int = ADDR_TX) -> bytes:
    """Кадр [sync, длина, тип, нагрузка, CRC8]. Длина вне 2…62 — BadFrame."""
    body = bytes((ftype & 0xFF,)) + bytes(payload)
    n = len(body) + 1
    if not MIN_LEN <= n <= MAX_LEN:
        raise BadFrame(f"frame length {n} outside {MIN_LEN}…{MAX_LEN}")
    return bytes((sync & 0xFF, n)) + body + bytes((crc8(body),))


def decode(data: bytes) -> Frame:
    """Один целый кадр (ровно), с проверкой длины и CRC; иначе BadFrame."""
    data = bytes(data)
    if len(data) < 4:
        raise BadFrame("frame too short")
    n = data[1]
    if not MIN_LEN <= n <= MAX_LEN or len(data) != n + 2:
        raise BadFrame("bad length")
    if crc8(data[2:-1]) != data[-1]:
        raise BadFrame("bad crc")
    return Frame(data[0], data[2], data[3:-1])


# ---------- каналы 0x16 ----------
def axis_to_crsf(x) -> int:
    """Ось −1000…1000 → отсчёт CRSF: 992 ± 820, округление от нуля, ограничение 172…1811."""
    x = max(-1000, min(1000, int(x)))
    q = (abs(x) * CH_SCALE + 500) // 1000
    return max(CH_MIN, min(CH_MAX, CH_CENTER + (q if x >= 0 else -q)))


def crsf_to_axis(v: int) -> int:
    """Отсчёт CRSF → ось −1000…1000 (обратное к axis_to_crsf с точностью до шага 11 бит)."""
    if int(v) >= CH_MAX:
        return 1000
    d = int(v) - CH_CENTER
    q = (abs(d) * 1000 + CH_SCALE // 2) // CH_SCALE
    return max(-1000, min(1000, q if d >= 0 else -q))


def pack_channels_raw(raw16, armed=None, sync: int = ADDR_TX) -> bytes:
    """Кадр 0x16 из 16 отсчётов 0…2047 (как есть). armed не None — байт состояния, длина 25."""
    raw16 = list(raw16)
    if len(raw16) != CHANNELS:
        raise BadFrame(f"need {CHANNELS} channels, got {len(raw16)}")
    acc = 0
    for i, v in enumerate(raw16):
        v = int(v)
        if not 0 <= v <= 0x7FF:
            raise BadFrame(f"channel {i + 1} value {v} outside 11 bits")
        acc |= v << (11 * i)
    payload = acc.to_bytes(22, "little")
    if armed is not None:
        payload += bytes((STATUS_ARMED if armed else 0,))
    return build(T_CHANNELS, payload, sync)


def pack_channels(ch16, armed=None, sync: int = ADDR_TX) -> bytes:
    """Кадр 0x16 из 16 значений осей −1000…1000 (лишнее ограничивается); armed не None — длина 25 (ELRS 4.x)."""
    ch16 = list(ch16)
    if len(ch16) != CHANNELS:
        raise BadFrame(f"need {CHANNELS} channels, got {len(ch16)}")
    return pack_channels_raw([axis_to_crsf(x) for x in ch16], armed, sync)


def unpack_channels(payload: bytes):
    """Нагрузка 0x16 → (16 отсчётов, armed: bool | None). armed None — байта состояния нет (22 байта)."""
    payload = bytes(payload)
    if len(payload) < 22:
        raise BadFrame("channels payload shorter than 22 bytes")
    acc = int.from_bytes(payload[:22], "little")
    raw = [(acc >> (11 * i)) & 0x7FF for i in range(CHANNELS)]
    armed = bool(payload[22] & STATUS_ARMED) if len(payload) > 22 else None
    return raw, armed


# ---------- служебные кадры ----------
def ping(dest: int = ADDR_BROADCAST, origin: int = ADDR_RADIO, sync: int = ADDR_TX) -> bytes:
    """0x28 Device Ping: [sync, 4, 0x28, адресат, отправитель, CRC]."""
    return build(T_PING, bytes((dest, origin)), sync)


@dataclass(frozen=True)
class DeviceInfo:
    dest: int
    origin: int
    name: str
    serial: int
    hw_id: int
    sw_id: int
    params: int
    param_version: int

    @property
    def serial_text(self) -> str:
        """Серийный номер как 4 символа ASCII (у ELRS — «ELRS»), иначе шестнадцатеричное число."""
        b = self.serial.to_bytes(4, "big")
        return b.decode("ascii") if all(0x20 < c < 0x7F for c in b) else f"{self.serial:08X}"

    @property
    def is_elrs(self) -> bool:
        return self.serial_text == "ELRS"

    @property
    def sw(self) -> str:
        """Версия: байты 1…3 идентификатора прошивки — старшая, младшая, исправление (как у ELRS)."""
        b = self.sw_id.to_bytes(4, "big")
        return f"{b[1]}.{b[2]}.{b[3]}"


def device_info(payload: bytes) -> DeviceInfo:
    """Нагрузка 0x29: адресат, отправитель, имя (до 0), серийный, hw, sw (u32 BE), число параметров, версия."""
    payload = bytes(payload)
    if len(payload) < 3:
        raise BadFrame("device info too short")
    end = payload.find(b"\x00", 2)
    if end < 0 or len(payload) < end + 1 + 14:
        raise BadFrame("device info truncated")
    name = payload[2:end].decode("utf-8", "replace")[:32]
    p = end + 1
    u32 = [int.from_bytes(payload[p + 4 * i:p + 4 * i + 4], "big") for i in range(3)]
    return DeviceInfo(payload[0], payload[1], name, u32[0], u32[1], u32[2], payload[p + 12], payload[p + 13])


def device_info_frame(name: str, serial: int = int.from_bytes(b"ELRS", "big"), hw_id: int = 0, sw=(4, 1, 0),
                      params: int = 0, param_version: int = 0, dest: int = ADDR_RADIO, origin: int = ADDR_TX,
                      sync: int = ADDR_RADIO) -> bytes:
    sw_id = (sw[0] << 16) | (sw[1] << 8) | sw[2]
    body = (bytes((dest, origin)) + name.encode("utf-8") + b"\x00" + serial.to_bytes(4, "big")
            + hw_id.to_bytes(4, "big") + sw_id.to_bytes(4, "big") + bytes((params, param_version)))
    return build(T_DEVICE_INFO, body, sync)


@dataclass(frozen=True)
class LinkStats:
    up_rssi_ant1: int          # дБм со знаком минус (в кадре — модуль)
    up_rssi_ant2: int
    up_lq: int                 # %
    up_snr: int                # дБ
    active_antenna: int
    rf_mode: int
    tx_power_idx: int
    down_rssi: int
    down_lq: int
    down_snr: int

    @property
    def rssi(self) -> int:
        """RSSI активной антенны, дБм."""
        return self.up_rssi_ant2 if self.active_antenna == 1 else self.up_rssi_ant1

    @property
    def tx_power_mw(self):
        return TX_POWER_MW[self.tx_power_idx] if 0 <= self.tx_power_idx < len(TX_POWER_MW) else None


def _i8(b):
    return b - 256 if b > 127 else b


def link_stats(payload: bytes) -> LinkStats:
    """Нагрузка 0x14 (10 байт): RSSI в кадре — модуль дБм; здесь — отрицательные дБм."""
    p = bytes(payload)
    if len(p) < 10:
        raise BadFrame("link statistics too short")
    return LinkStats(-p[0], -p[1], p[2], _i8(p[3]), p[4], p[5], p[6], -p[7], p[8], _i8(p[9]))


def link_stats_frame(rssi1=-40, rssi2=-42, lq=100, snr=10, antenna=0, rf_mode=4, power_idx=3, down_rssi=-45,
                     down_lq=100, down_snr=9, sync: int = ADDR_RADIO) -> bytes:
    body = bytes((abs(rssi1) & 0xFF, abs(rssi2) & 0xFF, lq & 0xFF, snr & 0xFF, antenna & 0xFF, rf_mode & 0xFF,
                  power_idx & 0xFF, abs(down_rssi) & 0xFF, down_lq & 0xFF, down_snr & 0xFF))
    return build(T_LINK_STATS, body, sync)


@dataclass(frozen=True)
class Timing:
    interval_us: float         # период радиопакетов модуля
    offset_us: float           # > 0 — кадры хоста приходят рано, < 0 — поздно


def timing(payload: bytes):
    """Нагрузка 0x3A: адресат, отправитель, подтип, … — Timing для подтипа 0x10, иначе None. Единицы кадра — 0,1 мкс."""
    p = bytes(payload)
    if len(p) < 11 or p[2] != SUB_TIMING:
        return None
    interval = int.from_bytes(p[3:7], "big")
    offset = int.from_bytes(p[7:11], "big", signed=True)
    return Timing(interval / 10.0, offset / 10.0)


def timing_frame(interval_us: float, offset_us: float, dest: int = ADDR_RADIO, origin: int = ADDR_TX,
                 sync: int = ADDR_RADIO) -> bytes:
    body = (bytes((dest, origin, SUB_TIMING)) + int(round(interval_us * 10)).to_bytes(4, "big")
            + int(round(offset_us * 10)).to_bytes(4, "big", signed=True))
    return build(T_RADIO, body, sync)


# ---------- потоковый разборщик ----------
class Parser:
    """Байты линии → кадры. Ресинхронизация как у EdgeTX: при неверной длине или CRC сдвиг на один байт.

    garbage_bytes — байты, отброшенные вне кадров (не синхробайт или неверная длина); crc_errors — заголовки с
    неверной CRC; crc_run — неверных CRC подряд с последнего верного кадра; frames — верных кадров.
    """

    def __init__(self, syncs=HOST_RX_SYNCS, max_buffer: int = 4096):
        self.syncs = frozenset(syncs)
        self.max_buffer = int(max_buffer)
        self._buf = bytearray()
        self.frames = self.crc_errors = self.crc_run = self.garbage_bytes = 0

    def feed(self, data: bytes) -> list:
        buf = self._buf
        buf += data
        out = []
        while buf:
            if buf[0] not in self.syncs:
                i = 1
                while i < len(buf) and buf[i] not in self.syncs:
                    i += 1
                self.garbage_bytes += i
                del buf[:i]
                continue
            if len(buf) < 2:
                break
            n = buf[1]
            if not MIN_LEN <= n <= MAX_LEN:
                self.garbage_bytes += 1
                del buf[0]
                continue
            if len(buf) < n + 2:
                break
            if crc8(memoryview(buf)[2:n + 1]) != buf[n + 1]:
                self.crc_errors += 1
                self.crc_run += 1
                del buf[0]
                continue
            out.append(Frame(buf[0], buf[2], bytes(buf[3:n + 1])))
            del buf[:n + 2]
            self.frames += 1
            self.crc_run = 0
        if len(buf) > self.max_buffer:                 # не должно случаться: незавершённый заголовок ждёт ≤ 63 байт
            self.garbage_bytes += len(buf)
            buf.clear()
        return out

    def reset(self) -> None:
        self._buf.clear()
