---
name: sheregesh-bench
description: Use when running an authorized Sheregesh hardware acceptance or benchmark on the shared Pi stand.
---

<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

Прочитай `docs/development/STAND-WORKFLOW.md` и относящуюся к задаче методику.
Уточни полномочия из текущей задачи; сохраняй уже предоставленную авторизацию в её области.
Выбери run-id и все ресурсы. На одном координирующем хосте перед прогоном:
`python3 tools/dev/stand_lease.py acquire --run-id <id> --resource pi1 --resource pi2`.
При занятости продолжай независимую локальную работу. Не перехватывай даже истёкшую аренду без сверки.
Сними состояние до/после, ограничь время, восстанавливай изменённое в finally; отдельно отчитай ошибку восстановления.
После завершения всех дочерних операций освободи аренду: `python3 tools/dev/stand_lease.py release --run-id <id>`.
Аренда advisory и только для одного POSIX-хоста: прямой SSH её не проверяет; несколько хостов требуют общего координатора.
Обычный DEV-пакет не обновляет ro-установщик и обёртки. Проверяй выпуск фактических процессов.
