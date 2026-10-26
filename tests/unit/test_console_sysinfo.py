# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Сведения раздела «Система» (sheregesh_console.sysinfo): разбор вывода sheregesh-ota-client и sheregesh-deployd,
фоновый запрос и ограничение частоты запуска команд."""
from sheregesh_console import sysinfo
from sheregesh_console.qtwait import wait_until

OTA = {"result": "OK", "build": {"commit": "b1d95a29d5b558b238266d27d201685293833d48", "ota_version": "0.1.1792924537"},
       "slots": {"active": "B", "other": "A", "tryboot_now": False}, "state": {"state": "COMMITTED", "active": "B"},
       "consistent": True, "switching": "переключение слотов не разрешено"}
DEPLOY = {"role": "console", "current": "0.1.0~dev0+g7b1cca302359", "previous": "0.1.0~dev0+gfac5d7e096cc"}


def test_summarize_fields_and_missing_commands():
    s = sysinfo.summarize(OTA, DEPLOY)
    assert s == {"available": True, "otaState": "COMMITTED", "slotActive": "B", "slotOther": "A", "tryboot": False,
                 "imageVersion": "0.1.1792924537", "imageCommit": "b1d95a29d5b5", "consistent": True,
                 "switching": "переключение слотов не разрешено", "release": "0.1.0~dev0+g7b1cca302359",
                 "previous": "0.1.0~dev0+gfac5d7e096cc", "role": "console"}
    empty = sysinfo.summarize(None, None)
    assert empty["available"] is False and empty["otaState"] == "" and empty["consistent"] is None


def test_refresh_runs_in_background_and_is_rate_limited(qapp):
    calls = []

    def run(argv):
        calls.append(argv[0])
        return OTA if argv[0] == "sheregesh-ota-client" else DEPLOY
    si = sysinfo.SystemInfo(run=run)
    si.refresh()
    assert wait_until(lambda: si.info["otaState"] == "COMMITTED", 3) and not si.busy
    si.refresh()                                     # сразу повторно — без новых запусков
    assert calls == ["sheregesh-ota-client", "sheregesh-deployd"]
