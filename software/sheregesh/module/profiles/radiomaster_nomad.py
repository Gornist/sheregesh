# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Профиль RadioMaster Nomad: модуль формата JR, семейство ExpressLRS, интерфейс UART/CRSF (сведения — из открытых
источников, docs/hardware/RADIOMASTER-NOMAD.md). В S6 интерфейс не открывается: модуль представлен только
лабораторным эмулятором (sheregesh_emulators.jrmodule)."""
from . import ModuleProfile

NOMAD = ModuleProfile(id="radiomaster-nomad", vendor="RadioMaster", model="Nomad", name="RadioMaster Nomad",
                      family="elrs", interface="uart-crsf", init_steps=("identify", "read-config", "check-version"),
                      init_timeout_s=5.0, ping_interval_s=1.0, response_timeout_s=3.0)
