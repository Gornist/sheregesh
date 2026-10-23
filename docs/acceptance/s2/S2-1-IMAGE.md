# S2-1 — загрузочный образ Raspberry Pi 4B

**Дата:** 2026-10-23 · **Уровень доказательств:** L2 (сборка и автоматическая проверка структуры). **Загрузка на плате — NOT TESTED.**

Отчёт не меняет статусы реестра: все Must остаются `NOT TESTED`, гейт REQ-REL-001 — NO-GO. Проверки `tryboot`, A/B-отката, дисплея, времени загрузки и производительности выполняются только на оборудовании (`PROBES.md`).

## Что собирается

| | Строгий `sheregesh-lab-pi4` | Dev `sheregesh-lab-dev-pi4` |
|---|---|---|
| Назначение | изолированный стенд ADR-0007 | пробы S2 на платах в локальной сети владельца |
| Сеть | eth0 192.168.77.1/24, без шлюза | eth0 DHCP |
| Вход | нет (паролей и SSH нет) | SSH только по открытому ключу, передаётся при сборке; root и пароли заблокированы, sudo-прав нет; polkit: только две службы «Шерегеша» и перезагрузка |
| Сборка | CI `image.yml` и `os/build.sh` | `os/build.sh` с `SSH_PUBKEY`; в CI — только при переменной `SHEREGESH_DEV_PUBKEY` |

Общее для обоих вариантов:
- **База.** Debian 13 trixie arm64 + архив Raspberry Pi, слои `rpi-image-gen`, закреплённого на `6c9cea1` (`os/rpi-image-gen.ref`). Ядро `6.18.50+rpt-rpi-v8`, `raspi-firmware 1:1.20260915-1`, `rpi-eeprom 28.34-1`. Wi-Fi и Bluetooth выключены оверлеями.
- **ПО.** Пакет `sheregesh_*_all.deb` (детерминированный, `tools/image/build_deb.py`): пульт под cage от `sheregesh-ui` и эмулятор удалённого блока (`DynamicUser`, только 127.0.0.1, `IPAddressDeny=any`). Также `sheregesh-probe` и `sheregesh-soak`. Зависимости только из Debian и Raspberry Pi OS: PySide6 6.8.2.1, GStreamer 1.26.2 (`-good`, `-bad` для `h264parse`, `-x`), cage 0.3.1, seatd.
- **Разметка ADR-0006** (`image-rota`, ext4) — по `verify.json` dev-образа:

| № | GPT | ФС | Метка | Размер | Смещение | ADR-0006 |
|---|---|---|---|---|---|---|
| 1 | bootconfig | FAT | BOOTCONFIG | 32 МиБ | 8 МиБ | autoboot: `autoboot.txt` (`tryboot_a_b=1`) |
| 2 | boot_a | FAT | BOOT | 256 МиБ | 40 МиБ | boot-A |
| 3 | boot_b | FAT | BOOT | 256 МиБ | 296 МиБ | boot-B (побайтово = boot-A) |
| 4 | system_a | ext4 | SYSTEM | 2560 МиБ | 552 МиБ | root-A, только чтение |
| 5 | system_b | ext4 | SYSTEM | 2560 МиБ | 3112 МиБ | root-B (побайтово = root-A) |
| 6 | persistent | ext4 | PERSISTENT | 1024 МиБ | 5672 МиБ | data: `/home`, журнал, `/var` каждого слота |

## Результаты

| Сборка | Где | Проверка структуры | Образ | Сжатый (.img.zst) |
|---|---|---|---|---|
| строгий, `d4ab0ee` | CI `ubuntu-24.04-arm`, 8 мин 35 с (https://github.com/Gornist/sheregesh/actions/runs/39988276518) | **50/50 PASS** | — | артефакт 651 МБ |
| строгий, `e35d1f3` | CI `ubuntu-24.04-arm`, 7 мин 11 с (https://github.com/Gornist/sheregesh/actions/runs/39992323130) | **50/50 PASS** | — | артефакт 784 МБ |
| строгий, `e35d1f3` | Docker trixie arm64 (Apple M1), чистое дерево | **50/50 PASS** (`image/sheregesh-lab-pi4/verify.json`) | 7 021 285 376 Б, SHA-256 `04d206b6…7324fe` | 784 МБ, `c5ec40e6…6ae30` |
| dev, `b094339` | Docker trixie arm64 (Apple M1); постобработка повторена исправленным `os/build.sh` (`post_only`) | **51/51 PASS** (`image/sheregesh-lab-dev-pi4/verify.json`) | 7 021 285 376 Б, SHA-256 `701426be…5ebcff` | 790 МБ, `d0d82316…3706cbf` |

Повторная сборка строгого варианта даёт другой SHA-256 образа: от сборки к сборке меняются UUID разделов, метки времени ФС и состав пакетов зеркала. Побитовая воспроизводимость образа не заявляется — только закреплённые входы и манифест.

Проверка `tools/image/verify_image.py` включает:
- защитный MBR и GPT (CRC, резервный заголовок);
- порядок, типы, размеры, выравнивание, ФС и метки шести разделов;
- побайтовую идентичность слотов A/B;
- корень `ro`;
- наличие ПО и включённые службы;
- блокировку учётных записей и отсутствие прав sudo;
- SSH по варианту и отсутствие хост-ключей в образе;
- `config.txt` (`disable-wifi`/`disable-bt`) и `autoboot.txt`;
- полноту манифеста относительно `dpkg status`.

Манифест: `/usr/share/sheregesh/image-packages.tsv` — 466 пакетов с SHA-256 из индекса репозитория; без хеша только собственный `sheregesh`, его хеш — в `SHA256SUMS`. Дополнительно в артефактах лежит SBOM `rpi-image-gen` (`*.sbom.zst`).

## Найдено при сборке и исправлено

1. В trixie стек Raspberry Pi OS маскирует `fake-hwclock`; заменён файлом часов `systemd-timesyncd` (ADR-0002, раздел реализации).
2. Генератор видео падал на чистом Debian: `timeoverlay`/`textoverlay` находятся в `gstreamer1.0-x` (`1db7c59`).
3. На платах `v4l2h264enc/dec` не выбирались: `h264parse` находится в `gstreamer1.0-plugins-bad` (`f628af5`).
4. Проверка образа сначала искала `/home` и `/var` в корневой ФС, а `image-rota` держит их на разделе данных. Кроме того, `sudo` в состоянии «purge ok installed» не считался установленным. Исправлено до фиксации результатов.
5. Постобработка сборки: SHA-256 образа в памяти (убита при 4 ГБ), пустое поле `commit`, пустой массив в bash 3.2 (`c544466`).

## Ограничения и открытые пункты

- **NOT TESTED:** загрузка на Pi 4, tryboot и A/B-откат (S2-5), дисплей и cage (S2-3/S2-4б), время загрузки (S2-2).
- `sudo` остаётся в образе: от него зависит `raspinfo`, штатный хук `rpi-image-gen` не может его удалить. Прав ни у кого нет: root заблокирован, `sheregesh-ui` не входит в группу `sudo`. Проверяется `users.nosudo`.
- RAUC, `sheregesh-health.service` и `mark-good` в образ не входят — S2-5 и S5.
- Воспроизводимость ограничена: архива использованных `.deb` (REL-010) пока нет, а archive.raspberrypi.com не хранит старые версии.
- `Type=notify`/`WatchdogSec` (ADR-0002) в пульте не реализованы — `Type=simple` до S3.
- Артефакт CI 651 МБ занимает хранилище Actions приватного репозитория (хранение 7 дней).
- Перед публичным релизом (D5): в корне репозитория нет файла лицензии, хотя заголовки SPDX указывают GPL-3.0-or-later.
