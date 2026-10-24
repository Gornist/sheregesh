# slot_guard — проверка на платах (только чтение, 2026-10-24) и регрессионные тесты

| Файл | Что показывает |
|---|---|
| `pi2-s1.json`, `pi2-s2.json` | два годных снимка pi2 (53 поля, активный A), между ними `shdeploy ota-status pi2` (активация сокета агента) |
| `pi2-self-compare.txt` | `compare --mode install` двух снимков без записи: PASS 41, FAIL 3 (`written.p3/p5`, `ota.state` — записи не было), ERROR 0 — инструмент не выдаёт PASS без записи слота |
| `pi1-s1.json`, `pi1-s2.json`, `pi1-unchanged-compare.txt` | pi1 `compare --mode unchanged`: PASS 44 |
| `wrong-board.json`, `wrong-board.txt` | снимок pi2 под именем pi1 → ERROR (серийный номер, файл роли) |
| `unreachable.json`, `unreachable.txt` | недоступный адрес → ERROR (SSH код 255, нет маркера конца) |
| `regression-tests.txt` | `tests/tools/test_slot_guard.py` — 24 теста, 53 случая (файловые снимки и функции снимка на bash), запуск unittest как в CI и pytest |

Первый прогон на платах выдал ERROR по полю `failed`: при отсутствии упавших юнитов `sed` на пустом вводе ничего не
печатал. Строгий режим не превратил это в PASS. Исправлено (`failed_units` на awk), снимки повторены.

Замечание о прежней версии: тесты `slot_guard` из `cd8018a` были функциями в стиле pytest, а CI запускает
`tests/tools` через `unittest`, поэтому в CI они не выполнялись (зелёный CI их не доказывал). Первая редакция
строгих тестов (`e4be556`) импортировала pytest и уронила CI. Сейчас тесты — `unittest.TestCase` с подтестами;
мутационная проверка (правило discard временно ослаблено) даёт FAIL.
