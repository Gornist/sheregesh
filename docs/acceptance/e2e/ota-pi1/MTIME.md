# Одинаковые mtime в образах devbox → console-setup failed (разбор, 2026-10-25)

## Причина
- genimage создаёт system/persistent через `mke2fs -d` в окружении с `SOURCE_DATE_EPOCH` (rpi-image-gen: значение
  bdebstrap — время старта mmdebstrap; наш `SOURCE_DATE_EPOCH` из `os/build.sh` сюда не доходит).
- **e2fsprogs ≥ 1.47.1** при заданном `SOURCE_DATE_EPOCH` зажимает время каждого inode: `t > SDE → SDE`
  (`misc/create_inode.c: clamped_time()`, флаг `EXT2_FLAG2_USE_FAKE_TIME` из `lib/ext2fs/initialize.c`).
- devbox: контейнер debian:trixie, **mke2fs 1.47.2** → все пути, созданные или изменённые при сборке после старта
  mmdebstrap, получают одно время. CI: mke2fs раннера **1.47.0** — зажима нет.

| Образ | mke2fs | `IG_SOURCE_DATE_EPOCH` | путей с одним mtime | console-setup |
|---|---|---|---|---|
| CI 57e8fe4 | 1.47.0 | 1792843202 (12:00:02) | нет (время сборки 12:00:37…12:01:14 разное) | cached_setup_terminal.sh 12:01:14 > конфиги 12:00:37/41 |
| devbox цикл 1 (abd9d68) | 1.47.2 | 1792885888 (23:51:28) | 4806 | всё 23:51:28 → **failed** (цикл 1, тогда приписано владельцам) |
| devbox 09fca4e (build1) | 1.47.2 | 1792919412 (09:10:12) | 4807 (2437 d, 1965 f, 397 l) | всё 09:10:12 → **failed** (OTA pi1, trial) |

В CI те же ~4778 путей имеют время сборки (разные секунды); в devbox ровно их время = SDE, новее SDE нет ни одного.

## Почему падает console-setup
`/lib/console-setup/console-setup.sh`: если `cached_setup_terminal.sh` **не строго новее** (`-nt`)
`/etc/default/keyboard` или `/etc/default/console-setup` → `setupcon --save` → запись в `/etc` (корень ro) → exit 1.
Наш слой готовит кэш при сборке (`setupcon --force --save-only`), но зажим уравнивал кэш и конфиги.

## Исправление (без маскировки службы)
`os/layer/sheregesh-lab.yaml`, сразу после `setupcon --save-only`: конфиги — `SOURCE_DATE_EPOCH − 1`, `cached_*` —
`SOURCE_DATE_EPOCH` (тот же SDE, к которому зажимает mke2fs). Порядок строгий при любой версии e2fsprogs: при зажиме
значения не меняются (не новее SDE), без зажима — тоже. Гейт `verify_image console-setup.ro`: mtime по debugfs,
`cached_setup_terminal.sh` строго новее обоих конфигов (как проверяет служба) — на образах devbox цикла 1 и build1
**FAIL**, на CI 57e8fe4 **PASS**. Тесты: `tests/tools/test_image_tools.py::ConsoleSetupCacheTest`.

## Другие зависимости от порядка mtime (оценка по build1)
- **Отказы:** кроме console-setup — нет. На пробной загрузке pi1 (B, build1) failed был только console-setup
  ([`trial-facts.txt`](trial-facts.txt)); B цикла 1 — тоже только он.
- **Python .pyc:** зажатых `.py` — 0 (исходники пакетов и нашего .deb старше SDE); заголовки .pyc валидны.
- **ConditionNeedsUpdate (ldconfig, sysusers, hwdb-update, catalog-update, update-done):** `/etc` ro → условие ложно
  независимо от времени; для `/var` решает наличие `/var/.updated`, а не равенство времён — как в CI.
- **ld.so.cache, hwdb.bin, gschemas.compiled, gdk-pixbuf loaders.cache, ca-certificates, initramfs:** при работе
  время не сравнивается (зажаты, но не используются как условие).
- **fontconfig / кэши иконок:** каталоги `/usr/share/fonts`, `/usr/share/icons/hicolor` зажаты, кэш fontconfig
  хранит прежнее время каталога → кэш считается устаревшим, шрифты пересканируются при первом запуске и кэш пишется
  в `~/.cache` (persistent). Только производительность первого старта; на trial видео 30 к/с, связь установилась.
- Следствие для воспроизводимости: время в образах devbox не совпадает с CI (зажим vs нет). Для OTA не важно
  (пакет несёт образ целиком); при переходе раннеров CI на e2fsprogs ≥ 1.47.1 CI получит тот же зажим — исправление
  и гейт рассчитаны на оба случая.

## Нужна ли пересборка
Да: в подписанном образе 09fca4e кэш и конфиги одного времени — confirm на нём не пройдёт. Нужна новая сборка с этим
коммитом (≈ 45 мин devbox), гейты (verify с `console-setup.ro`, order_check, owners), пакет, L3 ×2, повтор OTA pi1.
