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
- Установщик, launcher, `sheregesh-health`, `sheregesh-link`, ретранслятор sd_notify и агент OTA всегда работают из `/usr` (корень только для чтения). Присланный код от root **не исполняется**, только компилируется в байт-код; работает он в тех же непривилегированных службах.
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
