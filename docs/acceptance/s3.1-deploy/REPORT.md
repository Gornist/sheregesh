# S3.1-DEPLOY — обновления без перестановки microSD (протокол)

Испытания механизма, а не приёмка требований: статусы Must не меняются, релизный гейт REQ-REL-001 — NO-GO.
ADR-0009 (механизм A/B) и ADR-0010 (DEV deploy) — **предложения**, ждут владельца.

Разработка — ветка `s3.1-deploy` (база `52c6438`, 9 коммитов: c437844, 85c0bde, 4ab7cde, fb03598, 944b8e3, b773676,
afb773d, 2bb1c3b, b8bb2c1), влита в `v2` слиянием `1caac7b` без конфликтов. До интеграции на платах Pi ничего не
выполнялось; факты о разметке — из протоколов S2-BOOT и исходников rpi-image-gen `6c9cea1`.

## Что сделано

| Часть | Файлы |
|---|---|
| Пакет выпуска приложения (tar.xz из коммита, SHA-256 каждого файла) | `software/sheregesh/deploy/bundle.py` |
| Подпись `ssh-keygen -Y`, пространства имён `sheregesh-deploy`, `sheregesh-ota`, `sheregesh-ota-enable` | `software/sheregesh/deploy/sig.py` |
| Установщик (root): проверка, установка, ожидание READY, автоматический откат | `software/sheregesh/deploy/installer.py`, `tools/image/units/sheregesh-deploy@.service` |
| Launcher: выпуск или заводской код при каждом старте службы | `software/sheregesh/deploy/launcher.py`, обёртки в `tools/image/build_deb.py` |
| OTA A/B: слоты image-rota, запись неактивного слота, агент | `software/sheregesh/update/{slots,ota,ota_client}.py`, `sheregesh-ota.socket`, `sheregesh-ota@.service`, `sheregesh-ota-boot.service` |
| Инструмент владельца | `tools/deploy/shdeploy.py`: `status`, `deploy/rollback/factory pi1|pi2`, `build`, `ota-*` |
| Образ | `os/build.sh`, `.github/workflows/image.yml`, `os/layer/sheregesh-dev-access.*`, `os/layer/sheregesh-lab.yaml`, `tools/image/verify_image.py` (`deploy.*`) |
| Испытания | `tests/unit/test_deploy.py`, `tests/unit/test_ota.py`, `tests/tools/test_deploy_cli.py`, `tests/tools/test_polkit_rule.py`, `DeployImageTest`, стенд `tools/deploy/systemd-check/` |
| Решения | `docs/adr/ADR-0010-dev-deploy.md`, дополнение ADR-0009 (варианты A/B/C) |

## Модель привилегий DEV deploy

- `sheregesh-ui` может только положить пакет и подпись в `~/.cache/sheregesh-deploy/incoming` и **запустить** (polkit: `verb == "start"`) один из экземпляров `sheregesh-deploy@{install,rollback,factory}`. Параметров, путей и команд установщик извне не принимает.
- Установщик, launcher, `sheregesh-health`, `sheregesh-link`, ретранслятор sd_notify и агент OTA всегда работают из `/usr` (корень только для чтения). Присланный код от root **не исполняется и не компилируется** (исправлено по аудиту, см. ниже); работает он в тех же непривилегированных службах.
- Выпуск ставится в `/var/lib/sheregesh-deploy/releases/<id>` (владелец root, 0755/0644), `current` переключается через `rename`. Launcher перед каждым стартом сверяет владельца, права, SHA-256 всех файлов и отсутствие посторонних файлов; иначе запускает заводской код.
- Перезапускаются только службы роли: пульт — `sheregesh-console`, блок — `sheregesh-remote-net`, без роли — эмулятор и пульт. Нужны READY (Type=notify) и 40 с без перезапусков, иначе возврат на прежний выпуск (или заводской код) с повторным перезапуском служб.
- Питание пропало до подтверждения → launcher по отметке `pending` с другим `boot_id` выбирает прежний выпуск.
- Ключ подписи — **отдельный** от ключа входа SSH (при интеграции ужесточено): без `DEPLOY_PUBKEY` или при совпадении с ключом входа dev-образ не собирается, `verify_image` даёт FAIL, `shdeploy` ключ входа не подставляет. Закрытый ключ — только на Mac владельца (`~/.ssh/sheregesh-deploy`, `SHA256:5wJfw7QwUm7iVV+NCXlvgxqxgvXx5qLPjpDwoRlOZUw`); в образе — открытый ключ в `/etc/sheregesh/deploy/allowed_signers`.
- Обход через `~/.local/lib/python3.13/site-packages/*.pth` (подмешивание кода в службы без подписи) найден и закрыт: все команды `sheregesh-*` — `python3 -s`/`-I`.

## OTA A/B (подготовлено, переключение не активировано)

- Агент — root, сокет `0660 root:sheregesh-ui`, только локальный.
- Активный слот определяется по четырём признакам (`/dev/disk/by-slot`, метки GPT, `mountinfo`, раздел из device-tree); любое расхождение — отказ.
- Запись только в неактивные boot/system (`O_EXCL`, проверка «не смонтирован»), затем чтение и сверка SHA-256. `/var` целевого слота пересоздаётся из нового образа с переносом `/var/lib/sheregesh`; `/home`, ключи связи, хост-ключи SSH и machine-id не трогаются.
- **`tryboot` и `confirm` выключены**, пока владелец не подпишет разрешение для серийного номера платы (`sheregesh-ota-enable`). `confirm` — только на пробной загрузке и только при `sheregesh-health` без FAIL; автоподтверждения нет.

## Почему нужна последняя запись карты

На образах до `1caac7b` код приложения — в `/usr` (только чтение), root нет, блочные устройства недоступны, polkit разрешает четыре службы и перезагрузку. Подготовить установщик через SSH без записи нельзя. После записи единого образа приложение обновляется через `shdeploy deploy`, система — через `shdeploy ota-*`. Цена: раздел persistent пишется заново, роли и ключи связи S3.1 выпускаются после записи.

## Испытания до плат

| Уровень | Что | Результат |
|---|---|---|
| L2 | pytest на Mac, `b773676` | 355 passed (до работы 277) |
| L2 | `unittest` tests/tools | 45 OK |
| CI | `ci.yml` на `2bb1c3b`, https://github.com/Gornist/sheregesh/actions/runs/40086569119 | success: 357 passed, 45 OK, сканер секретов чист |
| CI | `ci.yml` на `b773676`, https://github.com/Gornist/sheregesh/actions/runs/40085698231 | упал на сканере секретов из-за строки в тесте, исправлено в `afb773d` |
| CI | `image.yml` на `2bb1c3b`, https://github.com/Gornist/sheregesh/actions/runs/40086571422 | success: dev 70/70 (с `deploy.*`), строгий 68/68 |
| L3 | Debian trixie с systemd 257 в контейнере (devbox-lan), `b8bb2c1` — [`container-systemd-check.log`](container-systemd-check.log) | 27 PASS / 0 FAIL: установка, READY, откат при невыходе в READY и при падении в окне, чужая подпись, подменённый пакет, rollback/factory, битый выпуск → заводской код; polkit — SKIP (нет polkitd) |
| L2 | `test_polkit_rule.py` (правило исполняется в node) | `sheregesh-ui` — только `start` трёх юнитов установщика, прежние права не расширены |
| L2 | Поток OTA из образа ci-52c6438-dev — [`ota-payload-ci-52c6438.manifest.json`](ota-payload-ci-52c6438.manifest.json) | сборка 115 с, 306 МиБ (boot 256 → 29 МиБ xz, system 2,5 ГиБ → 277 МиБ xz); запись кодом агента в файлы-разделы с повторным чтением — OK |

Пульт в контейнере — подставной (тот же launcher и Type=notify, модуль `sheregesh_remote`, без Qt). Базой контейнера взят образ armbian-debian-trixie: Docker Hub и зеркала Debian с devbox были недоступны.

SHA-256: пакет `sheregesh_0.1.0~dev0+g2bb1c3b_all.deb` — `6e425f444092842b710ed96127da864e6a03d4f83e7ce7eb990ee7a13ddab667`; выпуски контейнера good1/foreign `fe5722700db0…2c86`, good2 `47876f9001fc…8427`, bad `3cc6a4d9dab0…ab9e`, crash `2d66eef6b2bb…d8e5`; журнал контейнера `be95f0e1acdb…83ad1`; манифест OTA `ce78075f9f62…2cc`; поток `sheregesh-ota_sheregesh-lab-dev-pi4_0.1.1792830886.shota` (в репозиторий не входит) `97022de25d36…a404`, из образа `image_sha256 8e51d838…e837`.

## Не проверено

- На Pi: polkit вместе с polkitd, пульт под cage и ретранслятором через launcher, время старта, `remote-net` с mTLS после deploy — испытания на платах ниже.
- OTA на железе: реальные блочные устройства, `mount` + `rsync` для `/var`, `autoboot.txt` на FAT, поведение прошивки при tryboot, скорость записи microSD.
- Аппаратный watchdog не включён: зависший при tryboot слот возвращается только выключением питания (решение владельца).
- Испытания с отключением питания не проводились. AUD-014 (защита от отката версии) для dev-пути не решён: агент отвергает только версию ниже активной.

## План испытаний на платах

По одной плате, вторая работает; без отключения питания; без записи в активные разделы; после каждого шага — `shdeploy status piN` в протокол.

1. Запись единого образа на pi1 (pi2 работает), загрузка, службы, watchdog, `ac64aad`; `systemctl is-active sheregesh-ota.socket sheregesh-ota-boot`, `/etc/sheregesh/deploy/allowed_signers`, `sheregesh-deployd status` (`code_root` = `/usr/lib/sheregesh`).
2. DEV deploy на pi1: `status` → `deploy` (перезапущена только служба роли) → отказы polkit (`start sheregesh-deploy@shell`, `stop …@install`, запись в `releases/`) → выпуск с `raise SystemExit(3)` → `ROLLED_BACK` → `rollback`, `factory` → `systemctl reboot`, служба из того же выпуска.
3. pi2 — то же после подтверждения pi1 (штатное выключение перед заменой карты).
4. Роли и сертификаты обеим платам, mTLS; deploy/rollback не трогают роли и ключи связи.
5. OTA только запись на pi2 (`ota-install` → слот B `INSTALLED`, autoboot и хеши `authorized_keys`/ключей связи прежние, `ota-tryboot` → `REFUSED`). Пробная загрузка — только после отдельного решения владельца.

## Аудит привилегий установщика (2026-10-24, после интеграции)

Цепочка, процессы с UID 0 и что они делают с данными, которые контролирует отправитель:

| Шаг | UID 0 | Обработка недоверенных данных | Итог |
|---|---|---|---|
| 1. Приём | `sheregesh-deployd` | каталог входящих — `O_DIRECTORY|O_NOFOLLOW`, владелец sheregesh-ui; файлы через fd каталога (`O_NOFOLLOW`, обычный файл, владелец, предел размера), чтение в память, удаление через fd каталога | безопасно (каталог-ссылка — отказ, исправлено) |
| 2. Подпись | `ssh-keygen -Y verify` из `/usr` | данные через stdin, подпись — во временный файл в `PrivateTmp`; до любого разбора | безопасно |
| 3. Распаковка | `sheregesh-deployd` | `tarfile` только из памяти подписанных байтов; только обычные файлы, пути по белому списку, без `..`, ссылок, `__pycache__`/`.pyc`; запись `O_EXCL|O_NOFOLLOW` в каталог root 0755 (`ProtectSystem=strict`, доступна на запись только `/var/lib/sheregesh-deploy`) | безопасно |
| 4. Компиляция | **была:** `python3 -m compileall -j 0` по присланному коду | компилятор CPython от root на недоверенном входе: ошибки разбора, исчерпание памяти без `MemoryMax` (OOM-killer мог задеть `sshd`), пул процессов root. Объявления кодировки безопасны (поиск только в `encodings` стандартной библиотеки) | **риск — устранён:** компиляции нет (`5b85bc6`) |
| 5–6. Проверка, установка | `sheregesh-deployd` | сверка SHA-256 launcher, `rename`, `current` через `rename` | безопасно |
| 7. Запуск | нет | launcher из `/usr`, проверка владельца/прав/SHA-256/отсутствия посторонних файлов (теперь и `.pyc`), `exec` под sheregesh-ui | безопасно |
| 8. Откат | `systemctl` из `/usr` | имена служб — из таблицы установщика по роли | безопасно |

Почему без компиляции: весь код приложения компилируется в памяти на Pi 4 за 0,21 с (измерено на pi2) — старт службы почти не меняется, а компилятор от root больше не трогает присланный код. Непроверенный `.pyc` в выпуске launcher отвергает, потому что Python предпочёл бы его исходнику.

polkit: правило разрешает sheregesh-ui только `verb == "start"` трёх экземпляров `sheregesh-deploy@{install,rollback,factory}`; аргументов у юнита нет, путь установщика — `/usr/bin/sheregesh-deployd` (корень только для чтения). Transient-юнит с именем установщика systemd не создаёт (есть фрагмент на диске); остальные действия (`set-environment`, `daemon-reload`, `enable`, другие юниты) правилом не разрешены. Проверено тестом правила (node) — **это не испытание с настоящим polkitd**; оно — в `tools/probe/check_pi_deploy.py --only polkit` на плате.

Аппаратный watchdog BCM2835 есть, systemd его не использует (`RuntimeWatchdogUSec=0`, pi2 52c6438): зависание ядра плату не перезагрузит. Включение — решение владельца, не менялось. `ssh.service` не зависит от служб Шерегеша (`Requires/Wants/After/BindsTo/PartOf`).

Испытания после исправления: pytest 391 passed / 1 skipped (Mac), CI `cac9f4e` — success (392 passed, tools 46 OK), контейнер systemd 257 на `5b85bc6` — [27 PASS / 0 FAIL](container-systemd-check-5b85bc6.log) (в том числе «байт-кода в выпуске нет», пульт работает из выпуска).

Единый образ для записи: коммит `57e8fe4` (после него менялся только `tools/probe/check_pi_deploy.py`, в образ не входит), `image.yml` https://github.com/Gornist/sheregesh/actions/runs/40091950426 — success, проверка структуры 70/70, пакет `0.1.0~dev0+g57e8fe4`, `image_sha256 ca5e81eaf30784153d45c7fbb7199a4516de28e779e4fa3ad6602a6926799297`, `sheregesh-lab-dev-pi4.img.zst` `bab6d3521daca9606be76582a9a8a9d09d189ccf25131b13a8d8a974ff37625e`. Установщик в пакете образа совпадает с репозиторием, `compileall` в нём нет. Образы `722210d` и `ac64aad` устарели — не записывать.

## Аппаратная приёмка DEV deploy на pi1 (2026-10-24)

**Носитель.** `/dev/disk5` — внешний USB, 31 914 983 424 Б, «STORAGE DEVICE», GPT image-rota. Опознан как рабочая карта pi1: GUID первого раздела `33530462-6500-5391-FF64-09FD65520036` совпадает с образом `ee56bf1` и ни с одним другим из девяти скачанных образов. Владелец подтвердил запись именно на `disk5`. Исходные microSD и карта pi2 не затрагивались.

**Запись.** Образ `57e8fe4`, CI image run 40091950426. До записи — SHA-256 сжатого образа `bab6d352…625e` OK. После записи карта прочитана тем же дескриптором: `ca5e81eaf30784153d45c7fbb7199a4516de28e779e4fa3ad6602a6926799297` = `image_sha256`, карта извлечена. Журнал: [`../s3.1/pi1/flash-57e8fe4.log`](../s3.1/pi1/flash-57e8fe4.log).

**Плата.** pi1, серийный номер `10000000568152de`, 192.168.1.92, пакет `0.1.0~dev0+g57e8fe4`, `build.json` commit `57e8fe4`. Корень `ro` (`/dev/disk/by-slot/active/system`), `/persistent`, `/home` и `/var` — `rw` с `mmcblk0p6`. Загрузка 2,4 с ядро + 18,2 с пространство пользователя, `system running`, отказавших юнитов нет. `sheregesh-console` — `active`, READY («first frame shown»), launcher: «заводской код». `sheregesh-ota.socket` слушает, `sheregesh-ota-boot` — oneshot: слот A, `IDLE → IDLE`, без переключения. health: 10 PASS / 3 SKIP (сеть не настроена — не PASS). Из сети открыт только TCP 22 ([`ports-standalone.json`](../s3.1/pi1/deploy/ports-standalone.json)).

**Результаты** `tools/probe/check_pi_deploy.py` — протоколы в [`../s3.1/pi1/deploy/`](../s3.1/pi1/deploy/), сводка [`summary-all.json`](../s3.1/pi1/deploy/summary-all.json): **20 PASS, 1 SKIP, 0 FAIL, 0 ERROR**.

| Сценарий | Итог | Доказательство |
|---|---|---|
| image | PASS | ключ подписи в образе = `~/.ssh/sheregesh-deploy.pub`, ≠ ключ входа; установщик — заводской код |
| polkit (настоящий polkitd) | PASS | `start sheregesh-deploy@install` разрешён — установщик выполнился от root (REJECTED: нет входящих). Запрещены (`Interactive authentication required`): другой экземпляр, stop/restart установщика, transient-юниты (своё имя, `User=root`, произвольный), `set-environment`, `daemon-reload`, `enable`, `mask`, агент OTA, stop/restart/kill `ssh`. Transient-юнит с именем установщика systemd отверг сам («already loaded or has a fragment») |
| tamper | PASS | sheregesh-ui не может писать в `releases/`, `current`, `state.json`, файлы выпуска, `allowed_signers`, `/usr/lib/sheregesh` |
| app-stop / app-kill / app-restart | PASS ×3 | служба восстановлена; новые соединения SSH каждые 2 с — без отказов |
| watchdog-freeze | PASS | SIGSTOP → `Watchdog timeout (limit 20s)` → перезапуск; старых ретранслятора, cage и пульта нет, группа пуста, tty1 свободен |
| relay-kill-frozen (`ac64aad`) | PASS | пульт заморожен, SIGKILL ретранслятора → ни cage, ни пульта, служба перезапущена |
| deploy-good | PASS | `OK`, launcher: «выпуск 0.1.0~dev0+g339f6d4854e5» |
| no-bytecode | PASS | в выпусках нет `__pycache__`/`.pyc`, владелец root, 755 |
| foreign-sig / tampered / login-key-sig | PASS ×3 | `REJECTED` «подпись не принята», `current` и PID службы не изменились; ключ входа не даёт права установки |
| deploy-good2 | PASS | `OK`, предыдущий = первый выпуск |
| deploy-bad-start | PASS | `ROLLED_BACK` (в `state.json`, не «успех юнита»), `current` прежний |
| deploy-crash-window | PASS | падение через 15 с после READY → `ROLLED_BACK` |
| rollback / factory | PASS ×2 | `OK`; после factory launcher — «заводской код» |
| reboot | PASS | штатная перезагрузка → SSH, служба `active`, тот же выпуск |
| corrupt | SKIP | повредить выпуск на плате можно только от root (см. tamper); отказ launcher на посторонний `.pyc`/файл — контейнер systemd 257 и тесты |

Во всех сценариях с отказами новые соединения SSH открывались каждые 2 с — **ни одного отказа**.

**Найдено и исправлено на плате:**
1. Исправный выпуск откатывался (`ROLLED_BACK`, история установщика — [`board-state-history.json`](../s3.1/pi1/deploy/board-state-history.json)). Установщик пишет файлы по алфавиту, `sheregesh_ru.ts` оказывается «новее» `.qm`; пульт пытался пересобрать перевод, `lrelease` на плате нет → `RuntimeError`. Контейнер этого не видел: там пульт был подставной, без Qt. Исправлено в приложении (`339f6d4`) — готовый `.qm` используется с предупреждением. Исправление доставлено **самим DEV deploy**, без перезаписи карты. Выравнивание mtime в установщике — доработка для следующего образа.
2. Записи пульта идут из `session-N.scope` под идентификатором ретранслятора, а не под юнитом службы: `journalctl -u sheregesh-console` их не показывает. Инструмент читает журнал с учётом этого (`88f1951`). `/proc/<pid>/environ` пульта не читается — выпуск определяется по строке launcher.
3. Подряд идущие сценарии исчерпали `StartLimitBurst=5/300 с`: на 6-м запуске за 300 с перезапуски прекратились («Start request repeated too quickly») — защита от цикла работает как задумано ([`start-limit-evidence-run1.*`](../s3.1/pi1/deploy/)). Инструмент перед сценарием делает `reset-failed` (действие стенда, в протоколе).
4. Ошибки инструмента: разбор `allowed_signers`, `kill` незагруженного oneshot, ожидание перезапуска после SIGKILL — исправлено (`88f1951`).

**Итог.** pi1 — первая плата, где приложение обновляется без извлечения microSD: deploy, автоматический откат, rollback, factory и перезагрузка проверены на железе. OTA tryboot/confirm на pi1 не запускались. Аппаратный watchdog BCM2835 не менялся (`RuntimeWatchdogUSec=0`). ADR-0009/0010 — proposed. Гейт — NO-GO.

## Аппаратная приёмка DEV deploy на pi2 (2026-10-24)

**Носитель.** `/dev/disk5` — внешний USB, 31 914 983 424 Б. Опознан как рабочая карта pi2: GUID первого раздела `37570220-3200-2961-FF98-08FD48540093` = образ `52c6438`, стоявший только на pi2. Запись подтверждена владельцем после штатного выключения pi2 (`systemctl poweroff`, серийный номер сверен). pi1 всё это время работала.

**Запись.** Образ `57e8fe4`: SHA-256 до записи OK, после записи чтение карты — `ca5e81ea…9297` = `image_sha256`. Журнал: [`../s3.1/pi2/flash-57e8fe4.log`](../s3.1/pi2/flash-57e8fe4.log).

**Плата.** pi2, серийный номер `100000009d497e91`, 192.168.1.171, пакет `0.1.0~dev0+g57e8fe4`. Корень `ro`, загрузка 2,3 + 17,8 с, `system running`, пульт `active` (READY), SSH работает. Прежний ключ связи стёрт вместе с persistent. health: 10 PASS / 3 SKIP. Из сети открыт только TCP 22.

**Результаты** — [`../s3.1/pi2/deploy/`](../s3.1/pi2/deploy/), [`summary-all.json`](../s3.1/pi2/deploy/summary-all.json): **19 PASS, 1 SKIP (corrupt), 0 FAIL, 0 ERROR**, с первого прогона. Тот же набор сценариев, что на pi1: настоящий polkitd (14 запретов), tamper, SSH при остановке/SIGKILL/перезапуске пульта, watchdog, гибель ретранслятора при замороженном пульте, deploy/второй выпуск, отсутствие байт-кода, чужая подпись/изменённый пакет/ключ входа → REJECTED, неудачный старт и падение в окне → ROLLED_BACK, rollback, factory, перезагрузка. Новые соединения SSH во время отказов — без единого сбоя. История установщика — [`board-state-history.json`](../s3.1/pi2/deploy/board-state-history.json).

**Итог.** Обе платы обновляют приложение без извлечения microSD. Обе — на одном образе `57e8fe4` и одном выпуске приложения `0.1.0~dev0+g339f6d4854e5`. OTA tryboot/confirm не запускались, аппаратный watchdog не менялся, ADR-0009/0010 — proposed, гейт — NO-GO.
