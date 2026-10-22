# Шерегеш 1.0

Открытая воспроизводимая DIY-платформа для безопасных лабораторных испытаний: **пульт** и **удалённый блок** на Raspberry Pi 4B, связанные проводным Ethernet; работа офлайн, только с программными эмуляторами и синтетическими данными. Версия 1.0 не является средством управления действующими летательными аппаратами.

**Статус:** подготовка к реализации. Ни одно требование не имеет статуса PASS; гейт релиза — NO-GO.

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

## Проверки

```sh
python3 tools/reqs/reqgen.py validate          # согласованность реестра
python3 tools/reqs/reqgen.py generate          # обновить производные файлы
python3 tools/reqs/reqgen.py gate              # GO/NO-GO (REQ-REL-001)
python3 -m unittest discover -s tests/tools -p 'test_*.py'
python3 tools/security/scan.py secrets && python3 tools/security/scan.py scope
```

Нужен только Python 3.11+ (стандартная библиотека).

Прежняя разработка (RK3399, радиотракт) хранится в ветке `archive/main-2026-10-22` и будет перенесена в отдельный приватный архив (решение D5). В 1.0 ничего из неё автоматически не переносится.
