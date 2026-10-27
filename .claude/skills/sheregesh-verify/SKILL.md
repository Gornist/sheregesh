---
name: sheregesh-verify
description: Use when verifying a Sheregesh code change or preparing its evidence report.
---

<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

Выбери профиль по области задачи из `docs/development/VERIFICATION.md`.
Запусти `python3 tools/dev/verify.py <profile> --python <python-with-dependencies>` из рабочей копии.
Прочитай summary.json и журналы, включая SKIP. Не называй отсутствующую зависимость или эмулятор аппаратным PASS.
Сопоставь критерии задачи и контракт с реально выполненными проверками; приложи оставшиеся пробелы.
Не запускай аппаратные пробы и release gate вместо выбранного локального профиля.
