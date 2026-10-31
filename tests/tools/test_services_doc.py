# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""docs/software/services.md совпадает с юнитами образа (TEST-SW-001): каждый юнит описан, Restart= и WatchdogSec=
те же, у каждой постоянной службы (Type=notify) есть watchdog."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNITS = ROOT / "tools/image/units"
DOC = ROOT / "docs/software/services.md"


def unit_value(text, key):
    found = re.findall(rf"^{key}=(.*)$", text, re.M)
    return found[-1].strip() if found else "—"


def doc_rows():
    rows = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0].startswith("sheregesh-"):
            rows[cells[0]] = cells
    return rows


class ServicesDocTest(unittest.TestCase):
    def test_every_unit_is_documented_with_its_restart_and_watchdog(self):
        rows = doc_rows()
        units = sorted(p.name for p in UNITS.iterdir())
        self.assertEqual(sorted(rows), units)
        for name in units:
            text = (UNITS / name).read_text(encoding="utf-8")
            restart, watchdog = rows[name][-3], rows[name][-2]
            self.assertEqual((restart, watchdog), (unit_value(text, "Restart"), unit_value(text, "WatchdogSec")), name)

    def test_long_running_services_have_watchdog_and_restart(self):
        for p in UNITS.glob("*.service"):
            text = p.read_text(encoding="utf-8")
            if unit_value(text, "Type") == "notify":
                self.assertNotEqual(unit_value(text, "WatchdogSec"), "—", p.name)
                self.assertNotEqual(unit_value(text, "Restart"), "—", p.name)


if __name__ == "__main__":
    unittest.main()
