# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Видеослужба «Шерегеш» (ADR-0016): SRTP по UDP внутри GStreamer, Python только управляет.

Контракт интерфейсов — docs/architecture/VIDEO-SERVICE-CONTRACT.md (версия 1). Состав пакета по трекам:
pipelines (трек A: конвейеры SRTP TX и RX), session, ctl, state (трек B).
"""
