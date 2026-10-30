<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Работа с «Шерегешем»

Перед изменениями прочитай [процесс разработки](docs/development/AGENT-WORKFLOW.md),
[состояние проекта](docs/development/PROJECT-STATE.md) и относящиеся к задаче ADR/контракты.

- Требования и испытания: `docs/requirements/requirements.json`; решения владельца: `docs/decisions/DECISIONS.md`.
- Работа ведётся от `v2`. Каждая независимая задача — отдельный worktree; общий стенд согласуется отдельно.
- Перед коммитом проверь diff и индекс; добавляй только явные пути своей задачи.
- Успех эмуляторов L2/L3 не заменяет приёмку L4 и не переводит требования в PASS.
- **Образы — только через сервис сборки shbuild** (ADR-0018): `python3 tools/build/shbuild.py submit [РЕВИЗИЯ]`,
  затем `wait`/`status`/`fetch`. Никаких ручных `os/docker-build.sh`, сборок на Mac, Docker/colima на Mac и обходов:
  `docker-build.sh` без задания shbuild отказывает, а `shdeploy ota-build` и `flash_sd.py` не принимают образ без
  `shbuild-report.json` с итогом PASSED. На платы (OTA или карта) — только такой образ. Подробно —
  [процесс разработки](docs/development/AGENT-WORKFLOW.md#сборка-образа-и-раскатка).
- Сведения из памяти и чатов сверяй с датированными отчётами.
- При передаче задачи используй `docs/development/SESSION-HANDOFF.md`.

Проверки без плат:

```sh
python3 tools/reqs/reqgen.py validate
python3 tools/reqs/reqgen.py generate --check
python3 -m unittest discover -s tests/tools -p 'test_*.py'
```

Среда Qt/GStreamer, профили проверок и работа со стендом описаны в процессе разработки.
