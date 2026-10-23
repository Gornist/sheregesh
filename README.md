# Шерегеш 1.0

Открытая воспроизводимая DIY-платформа для безопасных лабораторных испытаний: **пульт** и **удалённый блок** на Raspberry Pi 4B, связанные проводным Ethernet; работа офлайн, только с программными эмуляторами и синтетическими данными. Версия 1.0 не является средством управления действующими летательными аппаратами.

**Статус:** этап S1 — программный прототип на эмуляторах. Ни одно требование не имеет статуса PASS; гейт релиза — NO-GO.

## Где что лежит

| Путь | Содержание |
|---|---|
| `docs/srs/SHEREGESH-SRS-1.0.md` | Системные требования, Rev B (раздел 4 генерируется) |
| `docs/requirements/requirements.json` | **Единственный источник** требований, испытаний и изменений |
| `docs/requirements/TEST-MATRIX.csv`, `CHANGELOG-REQ.md` | Генерируются из реестра |
| `docs/tests/TEST-SPEC.md` | Спецификация испытаний (генерируется) |
| `docs/hardware/` | Аппаратная спецификация Rev B, `PRE-CAD-CHECKLIST.md` |
| `docs/adr/` | Архитектурные решения ADR-0001…0008 |
| `docs/decisions/DECISIONS.md` | Журнал решений владельца (D1–D5) и Tech Lead |
| `docs/audit/` | Первичный аудит и реестр проблем AUD-NNN с текущими статусами |
| `docs/security/` | Секреты, компоненты вне объёма, проверка прежней разработки |
| `docs/baseline/rev-a/` | Неизменяемый комплект Rev A (SHA256SUMS) |
| `hardware/bom/` | BOM v0.4 |
| `tools/reqs/reqgen.py` | Проверка реестра, генерация, гейт релиза |
| `tools/security/scan.py` | Базовые проверки секретов и границ объёма |

## Запуск на эмуляторах (x86/Linux, без Raspberry Pi)

```sh
tools/dev/sheregesh-emu --demo                 # GUI пульта, демонстрационный сценарий (окно 1920×1200)
tools/dev/sheregesh-emu --demo --speed 10      # ускорить модельное время
tools/dev/sheregesh-emu --screenshots shots/   # снимки экранов сценария без дисплея и выход
tools/dev/sheregesh-emu --test                 # все автотесты (offscreen)
```

При первом запуске создаётся `.venv` из `tools/ci/requirements-dev.txt` (закреплённые версии: PySide6 6.12, PyYAML, jsonschema, pytest).
Нужны Python 3.11+ и системные библиотеки Qt (на Ubuntu: `libegl1 libgl1 libxkbcommon0 libfontconfig1`).
Данные эмулятора по умолчанию — во временном каталоге; `--data-dir DIR` сохраняет конфигурацию между запусками.

Демонстрационный сценарий (200 с модельного времени): штатное состояние → рост нагрузки и температуры, переход на батарею →
предупреждение TEMP-HIGH → потеря связи с удалённым блоком (PEER-LOST) → восстановление. Настройки меняются на экране
«Настройки» через проверку и подтверждение; после перезапуска с тем же `--data-dir` они загружаются из сохранённой ревизии.

Управление без сенсора: `Ctrl+1…3` или `F1…F3` — экраны; `Esc` — к вкладкам; стрелки — между вкладками и плитками;
`Tab` — по полям; `Пробел` или `Enter` — кнопки; в диалоге `Enter` — подтвердить, `Esc` — отменить.

## Программные модули

| Путь | Содержание |
|---|---|
| `software/sheregesh/config/` | Схема и хранилище ревизий конфигурации (ADR-0004) |
| `software/sheregesh/hal.py`, `monitor.py`, `faults.py`, `protocol.py` | Интерфейсы оборудования, монитор состояния, каталог событий RU/EN, совместимость протокола |
| `software/emulators/sheregesh_emulators/` | Эмуляторы и детерминированные сценарии, демонстрационный сценарий |
| `software/console/sheregesh_console/` | GUI пульта: бэкенд, QML, переводы |
| `tests/unit`, `tests/system`, `tests/gui`, `tests/tools` | Автотесты L2/L3; отчёт `reports/evidence.json` по TEST-ID |

## Проверки

```sh
python3 tools/reqs/reqgen.py validate          # согласованность реестра
python3 tools/reqs/reqgen.py generate          # обновить производные файлы
python3 tools/reqs/reqgen.py gate              # GO/NO-GO (REQ-REL-001)
python3 tools/reqs/reqgen.py evidence          # автотесты против реестра: уровень CI против требуемого
python3 -m unittest discover -s tests/tools -p 'test_*.py'
python3 tools/security/scan.py secrets && python3 tools/security/scan.py scope
```

Инструменты реестра и сканеры используют только стандартную библиотеку Python 3.11+.
Обычный CI показывает NO-GO как информацию; релизный конвейер (`.github/workflows/release.yml`) блокируется гейтом.

Прежняя разработка (RK3399, радиотракт) хранится в ветке `archive/main-2026-10-22` и будет перенесена в отдельный приватный архив (решение D5). В 1.0 ничего из неё автоматически не переносится.
