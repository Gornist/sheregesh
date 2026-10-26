# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Заводская тестовая конфигурация пульта и удалённого блока."""

CONSOLE_DEFAULTS = {
    "system.yaml": "device_name: Шерегеш-пульт\nlanguage: ru\ntheme: dark\nui_scale: 100\nscreen_timeout_s: 300\n",
    "diagnostics.yaml": "temp_warning_c: 70\ntemp_critical_c: 80\nbattery_warning_pct: 20\nbattery_critical_pct: 10\n",
    "link.yaml": "peer_address: 192.168.77.2\nport: 7700\nheartbeat_timeout_s: 3\n",
}


def console_defaults():
    return dict(CONSOLE_DEFAULTS)


# Удалённый блок (схема remote-1, ADR-0012 раздел 8): внешний модуль — лабораторный эмулятор Nomad.
REMOTE_DEFAULTS = {
    "module.yaml": "enabled: true\nprofile: radiomaster-nomad\nbackend: lab-emulator\nauto_init: true\n"
                   "auto_recover: true\n",
}


def remote_defaults():
    return dict(REMOTE_DEFAULTS)
