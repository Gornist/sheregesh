# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Разбор RTP для видеоприёма блока (sheregesh.videoingest): поток RFC 4571 из канала GStreamer (rtpstreampay),
заголовок RTP и SPS H.264 — фактическое разрешение потока. Без сети и без состояния."""


class Rfc4571:
    """Разбор потока «2 байта длины + RTP-пакет» из канала GStreamer (rtpstreampay)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= 2:
            n = int.from_bytes(self.buf[:2], "big")
            if len(self.buf) < 2 + n:
                break
            out.append(bytes(self.buf[2:2 + n]))
            del self.buf[:2 + n]
        return out


def rtp_info(pkt):
    """(номер последовательности, маркер конца кадра, полезная нагрузка) или None для не-RTP."""
    if len(pkt) < 12 or pkt[0] >> 6 != 2:
        return None
    cc, ext = pkt[0] & 0x0F, pkt[0] & 0x10
    off = 12 + 4 * cc
    if ext and len(pkt) >= off + 4:
        off += 4 + 4 * int.from_bytes(pkt[off + 2:off + 4], "big")
    return int.from_bytes(pkt[2:4], "big"), bool(pkt[1] & 0x80), pkt[off:]


# ---------------------------------------------------------------- SPS: фактическое разрешение потока
class _Bits:
    def __init__(self, data):
        self.d, self.i = data, 0

    def u(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | ((self.d[self.i >> 3] >> (7 - (self.i & 7))) & 1)
            self.i += 1
        return v

    def ue(self):
        z = 0
        while self.u(1) == 0:
            z += 1
        return (1 << z) - 1 + self.u(z)

    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


def _unescape(nal):
    out, zeros = bytearray(), 0
    for b in nal:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def parse_sps(nal):
    """Ширина, высота и профиль из SPS H.264 (NAL type 7). Пробрасывает IndexError на усечённых данных."""
    b = _Bits(_unescape(nal[1:]))
    profile = b.u(8)
    b.u(16)
    b.ue()
    chroma = 1
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma = b.ue()
        if chroma == 3:
            b.u(1)
        b.ue(), b.ue(), b.u(1)
        if b.u(1):
            for i in range(8 if chroma != 3 else 12):
                if b.u(1):
                    last = nxt = 8
                    for _ in range(16 if i < 6 else 64):
                        if nxt:
                            nxt = (last + b.se() + 256) % 256
                        last = nxt or last
    b.ue()
    poc = b.ue()
    if poc == 0:
        b.ue()
    elif poc == 1:
        b.u(1), b.se(), b.se()
        for _ in range(b.ue()):
            b.se()
    b.ue(), b.u(1)
    w_mbs, h_map = b.ue() + 1, b.ue() + 1
    frame_mbs_only = b.u(1)
    if not frame_mbs_only:
        b.u(1)
    b.u(1)
    crop = (0, 0, 0, 0)
    if b.u(1):
        crop = (b.ue(), b.ue(), b.ue(), b.ue())
    cx, cy = (2, 2 * (2 - frame_mbs_only)) if chroma == 1 else (1, 2 - frame_mbs_only)
    width = w_mbs * 16 - cx * (crop[0] + crop[1])
    height = (2 - frame_mbs_only) * h_map * 16 - cy * (crop[2] + crop[3])
    return {"width": width, "height": height, "profile_idc": profile}


def sps_from_rtp(payload):
    """NAL SPS из полезной нагрузки RTP H.264: одиночный NAL (7) или STAP-A (24)."""
    if not payload:
        return None
    t = payload[0] & 0x1F
    if t == 7:
        return payload
    if t == 24:
        i = 1
        while i + 2 <= len(payload):
            n = int.from_bytes(payload[i:i + 2], "big")
            nal = payload[i + 2:i + 2 + n]
            if nal and nal[0] & 0x1F == 7:
                return nal
            i += 2 + n
    return None
