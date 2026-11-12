# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Память использованных SSRC и ключей видеосеанса ограничена (ревью 2026-11-12)."""
from sheregesh.videosvc import session as S


def test_used_sets_are_bounded_and_generations_stay_unique(monkeypatch):
    monkeypatch.setattr(S, "USED_LIMIT", 8)
    tx = S.TxSessions()
    tx.new_control_session()
    offers = [tx.begin("main", "aes-128-gcm") for _ in range(50)]
    assert len(tx._used_keys) == 8 and len(tx._used_ssrc) == 8
    assert len({o.ssrc for o in offers[-8:]}) == 8 and len({o.key for o in offers}) == 50
    assert [o.gen for o in offers] == list(range(1, 51))


def test_recent_forgets_oldest():
    r = S._Recent(2)
    for v in (1, 2, 3):
        r.add(v)
    assert 1 not in r and 2 in r and 3 in r
