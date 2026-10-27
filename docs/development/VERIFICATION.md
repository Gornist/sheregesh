<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Профили локальной проверки

Запуск из корня рабочей копии: `python3 tools/dev/verify.py <profile> --python .venv/bin/python`.
Для requirements достаточно `python3`; для pytest нужны зависимости `tools/ci/requirements-dev.txt`.
Параметр --timeout ограничивает каждую команду; при timeout завершается вся группа её процессов.
Отчёт и полные журналы — в `reports/verify/<run-id>/`, `--out` может указать другой gitignored каталог.

| Профиль | Проверки |
|---|---|
| requirements | validate и generate --check |
| deploy | установщик, launcher, аппаратная проба без плат, CLI |
| video | видеослужбы: конвейеры, состояние, API, процессы и SRTP loopback |
| gui | tests/gui (offscreen) |
| control | кадры/выход CRSF, ControlReceiver, wire, цепочка |
| full | реестр, tests/tools, полный pytest, scan secrets |

Точные команды перечислены в `tools/dev/verify.py`. Профили выбираются явно; полный pytest остаётся в CI.
summary.json хранит commit, dirty, fingerprint содержимого до/после, окружение, команды, exit codes,
длительность и общий результат. Изменение исходников во время прогона даёт ERROR: доказательство устарело.
Fingerprint не учитывает gitignored зависимости и системные библиотеки — их состояние требует окружения CI.
`PASS` профиля требует успешных команд без SKIP pytest. JUnit сохраняет пропуски и причины;
при пропусках результат INCOMPLETE и exit 1, при отсутствии JUnit — ERROR.
Аппаратный L4 и релизный GO этим runner не выдаются. `reqgen.py gate` запускается отдельно.

Локальные JSON не загружай без проверки содержимого: тесты могут печатать диагностические данные.
Глобальная полная переустановка зависимостей для каждого маленького изменения не требуется.
