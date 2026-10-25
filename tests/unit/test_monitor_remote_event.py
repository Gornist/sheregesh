# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Событие удалённого блока REMOTE-EVENT несёт свой параметр «code»: журнал монитора принимает его без конфликта с
кодом события (pi1 2026-10-25: TypeError «got multiple values for argument 'code'» в каждом такте)."""
from sheregesh.monitor import EventLog


def test_remote_event_with_own_code_param_is_logged():
    log = EventLog()
    ev = log.raise_(1.0, "REMOTE-EVENT", code="RU-PWR-LOW")
    assert ev is not None and ev.code == "REMOTE-EVENT" and ev.params == {"code": "RU-PWR-LOW"}


def test_monitor_drains_remote_event_from_link():
    from sheregesh import monitor as m
    log = EventLog()
    for code, params in [("REMOTE-EVENT", {"code": "RU-TEMP-HIGH"}), ("LINK-HB-TIMEOUT", {"age": 3.1})]:
        log.raise_(2.0, code, **params)                  # тот же вызов, что в Monitor._peer
    assert [e.code for e in log.events] == ["REMOTE-EVENT", "LINK-HB-TIMEOUT"]
    assert m.CATALOG["REMOTE-EVENT"]
