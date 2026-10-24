# RAUC A/B на разметке image-rota — прототип варианта C ADR-0009 (испытание на файловых образах)

Ветка `feat/rauc-ab` от `v2` (6353c70). **ADR-0009 не утверждён**; прототип в образ не входит. На платах pi1/pi2 ничего не делалось (без SSH к ним), RAUC на платы не ставился; текущий OTA-агент, загрузчики, `os/layer/*`, `tools/image/units/*` и поведение `update/{ota,slots,confirm,model}.py` не менялись — только новые модули рядом. Ключи владельца не использовались: тестовые CA/подписанты создаются во временном каталоге и удаляются.

## Что сделано и работает

| Компонент | Файл | Суть |
|---|---|---|
| Бэкенд `bootloader=custom` | `software/sheregesh/update/rauc_backend.py` | `get-primary/set-primary/get-state/set-state/get-current` поверх `autoboot.txt` (формат `rpi-slot-tryboot`, `slots.render_autoboot`) и device-tree |
| Хуки и health | `software/sheregesh/update/rauc_hooks.py` | `pre-install`, `post-install` (`slots.detect` + `VarSeeder` + `confirm.expect_from_board`), `tryboot`, `on-boot`, `health` (`confirm.gates` → `rauc status mark-good/mark-bad`) |
| Один менеджер | `software/sheregesh/update/guard.py` | общая блокировка `flock(/persistent/update-manager.lock)` + заявка; `GuardedAgent` — отдельный подкласс `ota.Agent` |
| Конфигурация | `tools/rauc/system.conf`, `manifest.raucm.example` | compatible `sheregesh-lab-pi4-dev-1`; `rootfs.0/1` (p4/p5, bootname A/B) + зависимые `boot.0/1` (p2/p3, `parent=`); `data-directory=/persistent/rauc`; только verity; keyring `check-purpose=codesign` |
| Подпись и бандл | `tools/rauc/testpki.py`, `tools/rauc/build_bundle.py` | тестовый CA, подписант codeSigning, «без codeSigning», посторонний CA; бандл verity из `boot.vfat` + `system.ext4` |
| Испытание | `tools/deploy/rauc-check/{run,check}.py` | настоящий `rauc service` (D-Bus) в привилегированном контейнере на devbox-lan, диск GPT image-rota на loop, эмуляция прошивки tryboot |

Отображение операций RAUC на tryboot (RAUC — единственный, кто решает; бэкенд исполняет и держит инварианты):
- `set-state X bad` (RAUC делает это до записи слота и по `mark-bad`) — X плохой, назначенная проба X снимается; **основной слот плохим не помечается** (отказ).
- `set-primary X` (после записи) — проба X: `[tryboot] boot_partition=X`, `[all]` **не меняется**; перезагрузку `0 tryboot` делает `sheregesh-rauc tryboot` (перенос свежей конфигурации пульта, попыток ≤ 2 — UPD-004).
- `set-state X good` (`mark-good`) — только если X загружен **пробно** (флаг tryboot прошивки): `[all]=X`; иначе отказ.
- `get-primary` = `[all]`; `get-state`: основной/подтверждённый — good, иначе bad (в т. ч. «ждёт пробы»); `get-current` — раздел из `/proc/device-tree/chosen/bootloader/partition`.
- `health`: на пробной загрузке назначенного слота — `confirm.gates` (переиспользуется; критерий сокета агента через `RaucProbe` проверяет `rauc.service`) → PASS: `rauc status mark-good booted`; FAIL или нет записи установки: `rauc status mark-bad booted` + обычная перезагрузка → прошивка грузит `[all]`.
- Охранник: есть `/etc/rauc/system.conf` или назначенная проба RAUC → агент отказывает в install/tryboot/confirm (status/abort/enable/disable — можно); агент в VERIFIED…HEALTH_PENDING (`state.json` читается напрямую — агент 57e8fe4 об охраннике не знает) или живая заявка агента → RAUC отказывает в pre-install, `set-primary`, `mark-good`.

## Испытания и числа

- **Модульные (Mac, файлы-разделы `test_ota.Board`):** 44 теста — бэкенд 12, хуки/health 17, охранник 15; весь `tests/unit` + `tests/tools`: 476 passed, 2 skipped. Сканеры `secrets`/`scope`: 0 находок.
- **Файловый образ, RAUC 1.15.2 (debian:forky), бандл verity: 69/69 PASS** за 28 с — [`rauc-1.15-verity.log`](rauc-1.15-verity.log), [`.json`](rauc-1.15-verity.json).
- **Файловый образ, RAUC 1.13 (debian:trixie), бандл plain: 69/69 PASS** — [`rauc-1.13-plain.log`](rauc-1.13-plain.log), [`.json`](rauc-1.13-plain.json).
- RAUC 1.13 + verity на ядре devbox 7.0: 41/50 — бандл не монтируется (риск 1) — [`rauc-1.13-verity-kernel7.log`](rauc-1.13-verity-kernel7.log).

Сценарий (одинаков для обеих версий): RAUC определяет загруженный слот через `get-current` (без `root=`) и через `root=/dev/disk/by-slot/active/system` (как на платах; `/proc/cmdline` подменён bind-монтированием); основной A по `get-primary`; boot — зависимый слот. Отказы с проверкой причины: посторонний CA (`unable to get local issuer certificate`), сертификат без codeSigning (`unsuitable certificate purpose`), чужой compatible (`Compatible mismatch`), повреждённые данные бандла (verity: `Input/output error` при записи, B помечен bad; plain: CMS до записи) — во всех случаях A, `autoboot.txt` и проба не тронуты, при отказе подписи/compatible B не тронут. Охранник: агент в INSTALLED → pre-install RAUC отказывает; при `system.conf` `GuardedAgent` отказывает в install. **Обрыв записи:** `SIGKILL rauc service` при открытом `system_b` → B bad, проба не назначена, `tryboot` отказывает, A цел, заявка прошлой загрузки не держит слоты. **Установка** (boot 32 МиБ + system 192 МиБ, бандл 127 МБ): 1,0–1,2 с; B = SHA-256 образов, A и `[all]=2` не изменены, проба B; `/var` B — из образца нового образа, конфигурация пульта перенесена с A. **Отказ health → откат:** проба 1/2 с переносом свежей конфигурации (rev 8) → RAUC видит загруженный B → `mark-bad booted` (rc 0) → `[all]=2`, загрузка A. **Повтор:** установка, пропажа питания на пробе (A, попытка учтена), проба 2/2, health PASS (версия — из `build.json` настоящего раздела B) → `mark-good booted` → **`[all]=3, [tryboot]=2`**, RAUC: основной B; после обычной перезагрузки загружен B, B и A good; A (запасной) и общие данные persistent не изменились.

Найдено испытаниями и исправлено в прототипе: RAUC 1.15 не передаёт `RAUC_MF_*` обработчикам `system.conf` — версия читается из `manifest.raucm` смонтированного бандла; `rauc bundle --keyring` проверяет цепочку с назначением smimesign — сборка передаёт `check-purpose` через временный `system.conf`; прежний основной после `mark-good` — good (запасной).

## Изменения будущего системного образа (предложения, не выполнены)

1. Пакеты: `rauc`, `rauc-service` (D-Bus-служба); ядро — `dm-verity` (модуль), `squashfs`. RAUC ≥ 1.15, если ядро платы выводит статус dm-verity с доп. полем (риск 1).
2. Файлы: `/etc/rauc/system.conf` (из `tools/rauc/system.conf`), `/etc/rauc/keyring.pem` (CA по ADR-0005, не тестовый); обёртки `/usr/libexec/sheregesh/rauc-{tryboot-backend,pre-install,post-install}` и `/usr/bin/sheregesh-rauc` (через `WRAPPERS` в `tools/image/build_deb.py`); каталоги `/persistent/rauc`, `/persistent/sheregesh-rauc` (tmpfiles).
3. Юниты (`tools/image/units/`): `sheregesh-rauc-boot.service` (oneshot `on-boot`, `RequiresMountsFor=/persistent /bootfs`), `sheregesh-rauc-health.service` (после `multi-user.target`, `health` при uptime ≥ 90 с); `rauc.service` из пакета.
4. Один менеджер: в образе с RAUC не включать `sheregesh-ota.socket`/`sheregesh-ota-boot` или запускать агент через `GuardedAgent` (нужны точка входа `main` в `guard.py` и правка `build_deb.py`).
5. `confirm.py`: параметр «юнит менеджера обновлений» вместо жёсткого `sheregesh-ota.socket` (сейчас — подмена в `RaucProbe`); `verify_image.py`: проверки `system.conf`, keyring, обёрток.

## Риски

1. **dm-verity и версия RAUC:** ядро 7.0 выводит статус `V -`; RAUC 1.13/1.14 требует ровно `V` и не монтирует verity-бандл (в 1.15 сравнивается первое поле). Ядро Pi 6.12, вероятно, выводит `V` — проверить на плате; иначе rauc 1.15 (backport) или plain (хуже: весь бандл проверяется до установки, формат устаревший).
2. RAUC 1.13 ищет загруженный слот по `root=` раньше `get-current`: на платах верно, пока cmdline `root=/dev/disk/by-slot/active/system`; без `root=` — `get-current` бэкенда.
3. Устройства `/dev/mmcblk0pN` в `system.conf`: при загрузке с другого носителя RAUC писал бы на SD — `pre-install` сверяет цель RAUC со `slots.detect()` (4 признака) и отказывает при расхождении.
4. `get-state` «ждёт пробы» = bad — отличается от grub/barebox после установки; на решения RAUC не влияет (проверено), но видно в `rauc status`.
5. Агент старого образа 57e8fe4 не знает охранника: защищают только проверка его `state.json` со стороны RAUC и отключение агента в образе с RAUC.
6. Защита от отката версии (AUD-014) и CRL не настроены — решение владельца по ADR-0005.

## Не проверено

Настоящая прошивка Pi 4 (флаг tryboot, `reboot "0 tryboot"`, device-tree), arm64/ядро 6.12, systemd-юниты и факты health на плате (в контейнере факты задаёт `StubProbe`, сами критерии `confirm.gates` настоящие), D-Bus-политика на плате, отключение питания на настоящей microSD, установка «в обратную сторону» (основной B → A) на файловом образе, HTTP-потоковая установка, шифрованные и adaptive-бандлы, скорость записи на SD.
