# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Быстрая доставка приложения на лабораторную плату без перестановки microSD (S3.1-DEPLOY, dev-образ).

bundle    — формат пакета приложения: tar.xz с манифестом и SHA-256 каждого файла (общий для Mac и платы);
sig       — подпись и проверка ssh-keygen -Y (открытый ключ в образе, закрытый — только у владельца);
installer — привилегированный установщик с фиксированными действиями install | rollback | factory (только /usr);
launcher  — выбор каталога кода при старте службы: проверенный выпуск или заводской /usr/lib/sheregesh.
"""
