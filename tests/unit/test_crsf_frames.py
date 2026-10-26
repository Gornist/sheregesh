# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S8 (ADR-0014, раздел 3.1): кадры CRSF — CRC8 0xD5, эталонные кадры с известными байтами, 11-бит упаковка каналов,
шкала осей, device info, link statistics, синхронизация 0x3A.0x10, потоковый разборщик с ресинхронизацией.

Эталонные байты посчитаны независимо: побитовая CRC (не табличная, как в коде) и побитовая укладка каналов; CRC8 0xD5
сверена с контрольным значением CRC-8/DVB-S2 («123456789» → 0xBC)."""
import pytest

from sheregesh.crsf import frames as F

CENTER_25 = "ee1916e0031ff8c0073ef0810f7ce0031ff8c0073ef0810f7c0049"     # 16 × 992, байт состояния 0
CENTER_24 = "ee1816e0031ff8c0073ef0810f7ce0031ff8c0073ef0810f7cad"       # 16 × 992, без байта (спецификация)
MIXED = [172, 1811, 992, 0, 2047, 1, 1024, 500, 172, 1811, 992, 992, 992, 992, 992, 1500]
MIXED_ARMED = "ee1916ac9838f800f0ff0000903eac9838f8c0073ef0818fbb014e"
PING = "ee042800ea54"                                                      # адресат 0x00, отправитель 0xEA
TIMING = "ea0d3aeaee1000030d40fffffb2e6d"                                  # 20 000 мкс, −123,4 мкс


def crc_bitwise(data):
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0xD5) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def pack_bitwise(values):
    out = bytearray(22)
    bit = 0
    for v in values:
        for k in range(11):
            if (v >> k) & 1:
                out[bit // 8] |= 1 << (bit % 8)
            bit += 1
    return bytes(out)


def test_crc8_check_value_and_table():
    assert F.crc8(b"123456789") == 0xBC                     # CRC-8/DVB-S2: полином 0xD5, начальное 0
    for data in (b"", b"\x00", b"\xff" * 7, bytes(range(60))):
        assert F.crc8(data) == crc_bitwise(data)


def test_reference_channel_frames():
    assert F.pack_channels([0] * 16, armed=False).hex() == CENTER_25
    assert F.pack_channels([0] * 16).hex() == CENTER_24
    assert F.pack_channels_raw([992] * 16, armed=False).hex() == CENTER_25
    assert F.pack_channels_raw(MIXED, armed=True).hex() == MIXED_ARMED
    body = bytes.fromhex(MIXED_ARMED)[2:-1]
    assert body[1:23] == pack_bitwise(MIXED) and bytes.fromhex(MIXED_ARMED)[-1] == crc_bitwise(body)
    fr = F.decode(bytes.fromhex(MIXED_ARMED))
    assert (fr.sync, fr.type) == (F.ADDR_TX, F.T_CHANNELS)
    assert F.unpack_channels(fr.payload) == (MIXED, True)
    assert F.unpack_channels(F.decode(bytes.fromhex(CENTER_24)).payload) == ([992] * 16, None)
    assert F.unpack_channels(F.decode(bytes.fromhex(CENTER_25)).payload) == ([992] * 16, False)
    assert len(bytes.fromhex(CENTER_25)) == 27 and bytes.fromhex(CENTER_25)[1] == 25


def test_axis_scale_and_limits():
    assert [F.axis_to_crsf(x) for x in (-1000, -500, -1, 0, 1, 500, 999, 1000)] == \
        [172, 582, 991, 992, 993, 1402, 1811, 1811]
    assert F.axis_to_crsf(-5000) == 172 and F.axis_to_crsf(5000) == 1811
    assert F.axis_to_crsf(-610) == 992 - 500 and F.axis_to_crsf(610) == 992 + 500     # 610 × 0,82 = 500,2
    for x in range(-1000, 1001):
        assert abs(F.crsf_to_axis(F.axis_to_crsf(x)) - x) <= 1                       # ±1 шаг 11 бит
    assert (F.crsf_to_axis(172), F.crsf_to_axis(992), F.crsf_to_axis(1811)) == (-1000, 0, 1000)
    assert F.crsf_to_axis(0) == -1000 and F.crsf_to_axis(2047) == 1000
    frame = F.pack_channels([-1000, 1000, 0, 500, 0, 0, 0, 0] + [0] * 8, armed=True)
    raw, armed = F.unpack_channels(F.decode(frame).payload)
    assert raw[:4] == [172, 1811, 992, 1402] and raw[8:] == [992] * 8 and armed is True


def test_bad_inputs():
    with pytest.raises(F.BadFrame):
        F.pack_channels([0] * 15)
    with pytest.raises(F.BadFrame):
        F.pack_channels_raw([2048] + [992] * 15)
    with pytest.raises(F.BadFrame):
        F.build(0x7F, bytes(61))                            # длина 63 > 62
    with pytest.raises(F.BadFrame):
        F.decode(bytes.fromhex(PING[:-2] + "55"))           # неверная CRC
    with pytest.raises(F.BadFrame):
        F.unpack_channels(bytes(21))


def test_ping_and_timing_reference():
    assert F.ping().hex() == PING
    fr = F.decode(bytes.fromhex(PING))
    assert fr.extended and (fr.dest, fr.origin) == (0x00, 0xEA)
    assert F.timing_frame(20000, -123.4).hex() == TIMING
    fr = F.decode(bytes.fromhex(TIMING))
    assert (fr.sync, fr.type, fr.dest, fr.origin) == (0xEA, 0x3A, 0xEA, 0xEE)
    t = F.timing(fr.payload)
    assert (t.interval_us, t.offset_us) == (20000.0, -123.4)
    assert F.timing(bytes((0xEA, 0xEE, 0x11)) + bytes(8)) is None          # другой подтип


def test_device_info_and_link_stats():
    raw = F.device_info_frame("RM Nomad X-Band (emu)", sw=(4, 1, 0), params=12, param_version=1)
    fr = F.decode(raw)
    assert (fr.type, fr.dest, fr.origin) == (0x29, 0xEA, 0xEE)
    info = F.device_info(fr.payload)
    assert info.name == "RM Nomad X-Band (emu)" and info.serial_text == "ELRS" and info.is_elrs
    assert info.sw == "4.1.0" and info.sw_id == 0x00040100 and (info.params, info.param_version) == (12, 1)
    assert F.device_info_frame("X", serial=0x01020304).hex().count("01020304") == 1
    assert F.device_info(F.decode(F.device_info_frame("X", serial=0x01020304)).payload).serial_text == "01020304"
    with pytest.raises(F.BadFrame):
        F.device_info(fr.payload[:20])
    ls = F.link_stats(F.decode(F.link_stats_frame(rssi1=-71, rssi2=-80, lq=97, snr=-3, antenna=1, rf_mode=2,
                                                  power_idx=7, down_rssi=-60, down_lq=88, down_snr=5)).payload)
    assert (ls.up_rssi_ant1, ls.rssi, ls.up_lq, ls.up_snr, ls.rf_mode, ls.tx_power_mw) == (-71, -80, 97, -3, 2, 250)
    assert (ls.down_rssi, ls.down_lq, ls.down_snr) == (-60, 88, 5)
    assert F.link_stats(bytes(9) + b"\x00").tx_power_mw == 0


def test_parser_resync_and_counters():
    good = [F.timing_frame(20000, 0), F.link_stats_frame(), F.device_info_frame("RM Nomad")]
    stream = b"\x01\x02" + good[0] + b"\xea\x00" + good[1] + b"\xc8\x05\x14\x00" + good[2]
    p = F.Parser()
    got = [fr for i in range(len(stream)) for fr in p.feed(stream[i:i + 1])]     # побайтно
    assert [fr.type for fr in got] == [0x3A, 0x14, 0x29]
    # мусор: 01 02; EA с длиной 0 и 00; после неверной CRC заголовка C8 05 — сдвиг на байт: 05 14 00
    assert p.frames == 3 and p.garbage_bytes == 7 and p.crc_errors == 1 and p.crc_run == 0
    p2 = F.Parser()
    assert [fr.encode() for fr in p2.feed(stream)] == good                        # целиком — то же
    bad = bytearray(good[1])
    bad[-1] ^= 0xFF
    p3 = F.Parser()
    assert p3.feed(bytes(bad) + bytes(bad)) == [] and p3.crc_errors == 2 and p3.crc_run == 2
    assert [fr.type for fr in p3.feed(good[0])] == [0x3A] and p3.crc_run == 0
    p4 = F.Parser()                                                                # хост не принимает 0xEE
    assert p4.feed(F.ping()) == [] and p4.garbage_bytes > 0
    p5 = F.Parser(F.MODULE_RX_SYNCS)
    assert [fr.type for fr in p5.feed(F.ping() + F.pack_channels([0] * 16, False))] == [0x28, 0x16]
    p6 = F.Parser()
    assert p6.feed(good[0][:5]) == [] and [fr.type for fr in p6.feed(good[0][5:])] == [0x3A]
