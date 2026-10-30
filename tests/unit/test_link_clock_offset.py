# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Смещение часов блок − пульт для задержки видео (PeerClient.clock_offset): выборка с наименьшей задержкой и
половина наименьшего RTT сеанса — выбросы задержки и RTT оценку не сдвигают (как фильтр часов NTP)."""
from sheregesh.link.client import PeerClient

TRUE_OFFSET = 0.010          # часы блока впереди пульта на 10 мс


def client_with(delays, rtts_ms):
    c = PeerClient("127.0.0.1", 9)
    for d in delays:                                   # ts блока − время приёма = смещение − задержка доставки
        c._clock_samples.append(TRUE_OFFSET - d)
    for r in rtts_ms:                                  # status блока: его измерения RTT по ack
        c._on_rtt(r)
    c._st.telemetry = {"link": {"rtt_avg_ms": sum(rtts_ms) / len(rtts_ms) if rtts_ms else None}}
    return c


def test_outliers_do_not_shift_offset():
    # проводная сеть: задержка ~1 мс, но два heartbeat пришли с опозданием 200 мс, один RTT — 1,5 с (как на стенде)
    c = client_with([0.001, 0.0012, 0.2, 0.0011, 0.2, 0.001, 0.0013], [2.0, 2.2, 1483.0, 2.1, 2.0])
    assert abs(c.clock_offset() - TRUE_OFFSET) < 0.0005, c.clock_offset()


def test_no_samples_or_rtt_is_none():
    assert client_with([], [2.0]).clock_offset() is None
    assert client_with([0.001], []).clock_offset() is None
