# OTA CLOSURE — релизный гейт QA (2026-10-25, база `v2` c4e2d33)

Независимый аудит. Статус ADR не меняется; RAUC (`feat/rauc-ab`) вне объёма. Уровни: U — модульные тесты, L3 — файловый образ (`tools/deploy/ota-check`), L5 — плата.

## 1. Требования, относящиеся к закрытию A/B OTA

| Источник | Что требуется для закрытия |
|---|---|
| REQ-UPD-001 | Запись только в неактивный слот; активный остаётся загрузочным до подтверждения; данные persistent не меняются |
| REQ-UPD-002 (dev-путь) | Подписанный пакет принимается офлайн; неподписанный, изменённый и с посторонней подписью отвергаются. Отзыв ключа (4-й вариант) и X.509 по ADR-0005 к dev-агенту не относятся |
| REQ-UPD-004 | Откат на прежний слот не более чем за 2 попытки загрузки; активна конфигурация прежнего слота (REQ-CFG-006) |
| AUD-014 / ADR-0009 п. 3 | Версия ниже активной отвергается. Отзыва подписанных dev-пакетов нет |
| ADR-0006/0009 | Подтверждение (mark-good) — только по health PASS на пробной загрузке; основной слот меняется только в COMMITTED |
| ADR-0010 | Переключение слотов только по подписанному разрешению для серийного номера платы; агент работает от root только из `/usr`; `/var` у каждого слота свой, общие части persistent не трогаются |

REQ-UPD-003, -005…-009 (корень X.509, dev-режим с физической кнопкой, CI-подпись, компрометация, блокировка релиза) — выпускной путь, не dev-OTA. Для закрытия dev-OTA они не обязательны.

## 2. Минимальная матрица

| ID | Требование | Доказательство | Статус |
|---|---|---|---|
| OTA-01 | Старый агент (57e8fe4) читает исходный state.json | L5: шаги 1–3 на pi2 агентом 57e8fe4 (`s3.2-ota/step1`, `step3`) | PASS (L5). Отдельного U-теста нет; закреплено за STATE |
| OTA-02 | Новый агент читает старое состояние | `test_ota_s32.py::test_confirm_after_old_agent_install_checks_presence_not_comparison`, `::test_old_agent_install_with_missing_key_is_not_confirmed`; L3 `run.py --agent-rev 57e8fe4` (a7f1b74: 38/39) | PASS (U). L3 на кандидате — MISSING |
| OTA-03 | Старый агент читает состояние после нового | `test_update_state_compat.py` (4); `test_ota_gate.py::test_pi2_return_to_a_needs_state_repair_and_repair_restores_the_cycle` (код 57e8fe4: on_boot → ROLLED_BACK → новый пакет VERIFIED) | PASS (U). L5 — NOT TESTED |
| OTA-04 | health корректен от root | `test_confirm_probe.py` (5); L3 проверка «health с профилем стенда»; L5 шаг 5 — **FAIL** (a7f1b74, исправлено в 6353c70) | U PASS, L5 FAIL→исправлено, не перепроверено (HEALTH) |
| OTA-05 | Исправная система без ложного FAILED | `test_ota_s32.py::test_confirm_commits_console_when_all_gates_pass`, `test_ota.py::test_full_cycle_tryboot_health_commit` (Probe подменён); pi1 dry-run `confirm-gates-dry.json` 17 PASS + 2 N/A | U PASS; на плате с исполняемым health от root — MISSING (HEALTH) |
| OTA-06 | Успешный OTA подтверждается | `test_ota.py::test_full_cycle_tryboot_health_commit`; L3 до confirm не доходит; у `slot_guard` нет режима «после подтверждения» | U PASS; **L5 MISSING** |
| OTA-07 | Сбой → предусмотренный откат | `test_ota.py::test_health_fail_never_commits_and_returns_to_previous_slot`, `::test_power_loss_returns_to_committed_slot`, `::test_abort_on_trial_reboots_normally`; `test_ota_s32.py::test_rollback_keeps_previous_system_and_shared_data`; L3 abort → A; `slot_guard --mode rollback` (28 тестов) | U/L3 PASS; **L5 MISSING** |
| OTA-08 | После отката доступно следующее обновление | `test_ota.py::test_install_refused_when_state_disagrees_with_slots` (abort → install OK); `test_ota_gate.py` (57e8fe4 после ROLLED_BACK принимает новый пакет) | PARTIAL: установка агентом после ROLLED_BACK на уровне агента явно не проверена (STATE) |
| OTA-09 | Пользовательская конфигурация сохраняется | `test_ota.py::test_install_writes_only_inactive_slot_and_keeps_persistent`, `::test_full_cycle…` (хеши persistent); `test_ota_s32.py::test_config_carried_fresh_at_tryboot_and_not_copied_back`, `::test_rollback_keeps…`, `::test_var_seed_fills_missing_and_keeps_existing` (только GNU tar: CI, на Mac SKIP); L3 (persistent, `/var` B из образа, перенос конфигурации, var-seed) — PASS на a7f1b74 | PASS (U, L3 a7f1b74). L3 на кандидате — MISSING |
| OTA-10 | Повторные запуски не портят состояние | `test_ota_s32.py::test_reinstall_replaces_only_inactive_slot`, `::test_state_survives_agent_restart_between_steps`, `::test_interrupted_install_never_becomes_bootable`; `test_ota.py::test_serve_protocol_and_bad_requests` | PASS (U) (STATE) |
| OTA-11 | Неподписанный / повреждённый / чужой / несовместимый / более старый пакет отвергается | `test_ota.py::test_bad_updates_never_touch_active_slot` (7 вариантов), `::test_switching_is_disabled_without_signed_enable_token` (другой серийный номер); `test_ota_s32.py::test_bad_signature_rejected_before_images_are_read`, `::test_other_layout_rejected`; `test_update_model.py::test_rejected_bundles_never_write`; **новые** в `test_ota_gate.py`: пустая и мусорная подпись, подпись другого назначения, манифест изменён после подписи (версия, SHA), разрешение с подписью OTA/APP, повтор старого подписанного пакета после COMMITTED, числовое сравнение версий | PASS (U) |
| OTA-12 | Проверяются целостность и версии образов | `test_ota.py` tampered/truncated/oversize; `test_ota_s32.py::test_readback_mismatch_fails_and_blocks_tryboot`; **новые**: SHA-256 образа проверяется независимо от SHA-256 сжатого; слот с версией, отличной от манифеста, не подтверждается (единственный FAIL — критерий `version`); L3 «build.json = коммит и версия манифеста» | PASS (U, L3 a7f1b74) |

Прогон 2026-10-25 (Mac, `.venv`): `test_ota.py test_ota_s32.py test_update_model.py test_update_state_compat.py test_confirm_probe.py test_ota_gate.py` — 96 passed, 1 skipped (var-seed без GNU tar); `tests/tools/test_slot_guard.py` — 28 passed.

## 3. P0

1. **Удержание pi2 на B не ведёт к закрытию.** Агент B (`a7f1b74`; `ota.py`/`slots.py` с тех пор не менялись, что закреплено тестом) делает следующее:
   - отвергает `install` в TRYBOOT;
   - `confirm` принимает только в HEALTH_PENDING;
   - `abort` на пробной загрузке — просто перезагрузка.
   Сетевого пути «новый пакет на pi2» без возврата на A нет. Доказательства: `test_ota_gate.py::test_pi2_on_trial_b_cannot_take_new_package_without_returning_to_a`, а также L3 «повторная установка в TRYBOOT: REJECTED».
2. **Кандидата нет:** образ ≥ d1258f3 не собран. Без пакета и L3 в обоих режимах гейт закрыть нельзя.
3. **OTA-04/05/06/07 на плате не продемонстрированы.** На L5 есть только FAIL a7f1b74. Исправления проверены только на U.

P1:
- У `slot_guard` нет режима `commit`: `compare` требует `[all]=2`, `[tryboot]=3`. Подтверждение проверяется вручную по снимку (§5 D).
- Подписанный пакет a7f1b74 (`f6e19234…6edb`) по-прежнему проходит проверку подписи и версии на pi1 и на pi2 после ремонта. Отзыва нет (AUD-014) — защищает только `--sha256`.

## 4. Восстановление pi2 (B, пробная загрузка, TRYBOOT, попытка 1; основной A, агент 57e8fe4)

Root-входа нет: root и пароли заблокированы (S2-1 `verify.json`, ADR-0002). У `sheregesh-ui` есть polkit на reboot и **power-off**.

| Путь | Без root / без перезаписи SD | Результат | Риск |
|---|---|---|---|
| R1. Держать B | да / да | Прогресса нет (P0-1) | Любой сбой питания, паника или `ota-abort` → R2 без ремонта. `ota-tryboot` → R3. DEV deploy на B теряется при возврате |
| R2. `ota-abort` → A | да / да | A работает. Падает `sheregesh-ota-boot` (TypeError на `expect`): health FAIL, `ota-*` неработоспособны | Тупик без ремонта state.json |
| R3. Повторный `ota-tryboot` на B | да / да | Попытка 2. Confirm a7f1b74 снова FAIL → ROLLED_BACK, состояние несогласовано, install REJECTED → abort → R2 | Хуже R2: лишняя перезагрузка, попытки исчерпаны (тест `…second_tryboot…`). **Запрещено** |
| **R2a. Ремонт карты (рекомендуется)** | root на маке (sudo, debugfs из e2fsprogs) / без перезаписи | `systemctl poweroff` на B (штатно) → карта в мак → `python3 tools/image/repair_ota_state.py` находит p6 по GUID раздела `45060026-…` → `--dump` (только чтение) → `--disk diskNs6 --confirm diskNs6`: проверка «TRYBOOT/B, есть expect», копия в `docs/acceptance/ota-closure/hardware/state-before-repair.json`, **переименование** `state.json` → `state.json.a7f1b74` (debugfs link+unlink, JSON вручную не правится), `e2fsck -fn`, eject → загрузка A. Агент 57e8fe4 создаёт IDLE на A, `version_active` из build.json A (тесты `test_pi2_repair_then_ota_with_v2_package[remove_file]`, `tests/tools/test_repair_ota_state.py`) | Физическое действие владельца: карта и пароль sudo. Пишется одно имя на p6; p1 `autoboot.txt` не трогается. Карта вынимается только после poweroff |
| R4. Перезапись microSD | — / нет | Чистое состояние | Потеря ключей устройства и сертификатов mTLS, ключа хоста SSH; повторная регистрация. Крайний случай |

После R2a: снимок `slot_guard snap pi2` и `compare --mode rollback` (до — снимок шага 1). Ожидается A, `dt.tryboot=0`, OTA IDLE без target, health PASS, failed none. Затем шаги 1–5 с кандидатом. Первую установку выполняет **агент 57e8fe4**: отпечатков `expect` не будет, confirm проверит наличие, а сравнение «до/после» — по снимкам slot_guard.

## 5. Чек-лист допуска к аппаратному OTA pi2

Каждый пункт — с доказательством (файл или вывод) в `docs/acceptance/ota-closure/`. Любой FAIL или ERROR — STOP.

**A. Кандидат**
- [ ] Полный SHA коммита в `v2`. `git merge-base --is-ancestor` для 6353c70 и d1258f3 → 0. Изменения `software/sheregesh/update` после d1258f3 перечислены и проверены.
- [ ] CI на этом SHA — success. Полный pytest на ubuntu, включая var-seed с GNU tar.
- [ ] Локально на SHA — целевые тесты §2 без FAIL.
- [ ] Образ собран на devbox (решение владельца 2026-10-25: сборки образов только там) из этого SHA: в `build-report.json` `commit` = SHA, в `verify.json` нет FAIL, `image_sha256` записан.

**B. Пакет**
- [ ] `shdeploy ota-build --build-report …`. SHA-256 файла `.shota` записан и утверждён владельцем.
- [ ] Манифест:
  - `commit` = SHA;
  - `compatible` = `sheregesh-lab-pi4-dev-1`;
  - `layout` = `image-rota-gpt-ab-1`;
  - `version` = `0.1.<source_date_epoch>` > `0.1.1792865618` (a7f1b74) > `0.1.1792843040` (активная);
  - `image_verify.failed` = [];
  - `units` = юниты .deb.
- [ ] Подпись: `pi1-preflight/tools/verify_payload.py` с `allowed_signers` плат (SHA `74c95610…00c0`, ключ `SHA256:5wJfw7Qw…`) → `Good "sheregesh-ota" signature`, SHA-256 сжатых образов = манифест, лишних байт нет.
- [ ] Не a7f1b74 (`f6e19234…6edb`) и не пакет из образа < d1258f3.

**C. Файловые испытания L3** (`ota-check/run.py`, база `ci-57e8fe4-dev`, пакет кандидата)
- [ ] `--agent-rev 57e8fe4` (реальный путь pi1 и pi2) — все PASS (ожидается 39/39).
- [ ] Агент кандидата — все PASS.
- [ ] В обоих режимах PASS:
  - «health с профилем стенда, не /root»;
  - «build.json = коммит и версия манифеста»;
  - чужая подпись и обрыв — A/B не тронуты;
  - «повторная установка в TRYBOOT: REJECTED»;
  - abort → A;
  - общие данные не изменились.

**D. slot_guard и наблюдение**
- [ ] `tests/tools/test_slot_guard.py` PASS.
- [ ] Шаг 1: `install` pi2 + `unchanged` pi1.
- [ ] Шаг 4: `trial` (`build.version` = V).
- [ ] При откате: `rollback --trial`.
- [ ] После confirm (режима нет) — вручную по `snap` после обычной перезагрузки:
  - `autoboot` `[all]=3`, `[tryboot]=2`;
  - активный B по всем признакам, `dt.tryboot=0`;
  - OTA COMMITTED, `active=B`, `version_active=V`;
  - health PASS, failed none;
  - поля идентичности = снимку шага 1.

**E. Предусловия pi2**
- [ ] R2a выполнен и доказан (§4): A, ROLLED_BACK/IDLE, `consistent: true`, `ota-status` работает, health PASS, разрешение переключения есть.
- [ ] pi1 не меняется (`unchanged`).

**F. Процедура и восстановление**
- [ ] `ota-install` только с `--sha256 <утверждённый>`.
- [ ] Confirm FAIL на шаге 5: причину разобрать до любого повтора `ota-tryboot`. Повтор допустим только при понятной и устранимой без нового пакета причине. Иначе `ota-abort` → A. Возврат безопасен: агент кандидата не пишет пустой `expect` (OTA-03). Затем `slot_guard rollback`.
- [ ] Отказ на шаге 1–3: основной A не тронут (`install`/`untouched`). `ota-abort`, повтор только с тем же утверждённым SHA.
- [ ] Отключать питание запрещено только на время записи (шаг 1). На пробной загрузке сбой питания штатно возвращает A.
- [ ] Решение владельца по пути pi2 (R2a / R4) и утверждённый SHA пакета записаны.
