# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Профиль RadioMaster Nomad: модуль формата JR, семейство ExpressLRS, интерфейс UART/CRSF (сведения — из открытых
источников, docs/hardware/RADIOMASTER-NOMAD.md). Бэкенд lab-emulator представляет модуль лабораторным эмулятором
состояния (sheregesh_emulators.jrmodule); бэкенды crsf-pty-lab и crsf-uart (ADR-0014) говорят с ним по CRSF 400 000
бод, модуль опознаётся по имени из device info («RM Nomad …»)."""
from . import ModuleProfile

NOMAD = ModuleProfile(id="radiomaster-nomad", vendor="RadioMaster", model="Nomad", name="RadioMaster Nomad",
                      family="elrs", interface="uart-crsf", init_steps=("identify", "read-config", "check-version"),
                      init_timeout_s=5.0, ping_interval_s=1.0, response_timeout_s=3.0,
                      baud=400000, no_response_s=2.0, crsf_name="RM Nomad")
