# S3.2-OTA шаг 5 на pi2 — confirm: **FAILED → STOP** (2026-10-24/25)

- Предусловия перед confirm выполнены: тот же boot_id, что у проверок шага 4 (`bd962e88…`), `HEALTH_PENDING`, B в пробной загрузке, упавших юнитов нет.
- `ota-confirm pi2` → **FAILED**, «не пройдены health, health.network; health FAIL, повтор» ([`confirm.json`](confirm.json)). Остальные 17 критериев PASS. Детали: `config.link`, `link.state`, `certs.device` = FAIL, `certs.ca` и `certs.key-mode` отсутствуют.
- **Причина — дефект `confirm.py`.** Агент OTA работает от root (`sheregesh-ota@.service`), а `Probe.health()` вызывал `sheregesh-health` без `--link-config`. Путь по умолчанию `~/.config/sheregesh/link.json` раскрывался в `/root/...`, профиля там нет, поэтому здоровая плата получала FAIL. Сухой прогон шага 4 шёл от `sheregesh-ui`, где путь правильный, поэтому показал PASS. На образе диска и в модульных тестах Probe подменялся, так что дефект мог проявиться только на плате.
- **Состояние после отказа** ([`state-after-confirm-fail.txt`](state-after-confirm-fail.txt)) безопасное:
  - pi2 работает на B в пробной загрузке;
  - OTA `TRYBOOT`, попытка 1;
  - `autoboot.txt` `[all]=2` — основной **A**, SHA `f336517a…` прежний;
  - любая перезагрузка или выключение питания вернёт A.
- Повторный confirm агент отвергнет: ему нужен `HEALTH_PENDING`. Повторный tryboot запрещён, и с тем же `confirm.py` он снова дал бы FAIL. **Слот B этим пакетом подтвердить нельзя.** DEV Deploy на B (P0.1) не выполнялся.
- **Исправление** (`v2`): `Probe.health()` передаёт `--role` и `--link-config /home/sheregesh-ui/.config/sheregesh/link.json` — тот же файл, отпечаток которого снимает `expect_from_board`. Тест — `tests/unit/test_confirm_probe.py`. Чтобы довести OTA pi2 до конца, нужен новый образ и пакет с этим исправлением и новый цикл: решение владельца о `ota-abort`/возврате на A, затем шаги 1–5.
