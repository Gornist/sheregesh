# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Заводская тестовая конфигурация пульта."""

CONSOLE_DEFAULTS = {
    "system.yaml": "device_name: Шерегеш-пульт\nlanguage: ru\ntheme: dark\nui_scale: 100\nscreen_timeout_s: 300\n",
    "diagnostics.yaml": "temp_warning_c: 70\ntemp_critical_c: 80\nbattery_warning_pct: 20\nbattery_critical_pct: 10\n",
    "link.yaml": "peer_address: 192.168.77.2\nport: 7700\nheartbeat_timeout_s: 3\n",
}


def console_defaults():
    return dict(CONSOLE_DEFAULTS)
