<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Сервис сборки образов shbuild: устройство и эксплуатация

Решения: [ADR-0018](../adr/ADR-0018-build-service.md) (сервис, гейты, раскатка), [ADR-0019](../adr/ADR-0019-build-host-pi5.md)
(хост на Raspberry Pi 5). Правило для работы — [AGENT-WORKFLOW](AGENT-WORKFLOW.md#сборка-образа-и-раскатка): образы
собирает **только** shbuild, на плату идёт только артефакт задания с итогом `PASSED`. Хост-плата — [BUILDHOST-PI5](BUILDHOST-PI5.md).

## 1. Что это

Один файл `tools/build/shbuild.py` (команды Mac и исполнитель хоста) плюс `boot_check.sh` и `vm/` (загрузочный тест),
`owners_check.sh`/`order_check.sh` (гейты), `make_builder.sh` и `make_vmkit.sh` (подготовка). На Mac ничего не
собирается: команды `submit/status/wait/logs/cancel/fetch` по SSH ставят и опрашивают задание на **хосте сборки**,
где работает фоновый исполнитель. Задание — это коммит (git bundle), не рабочее дерево; одно задание за раз
(`flock`), остальные в очереди. Конечные состояния всегда явные: `PASSED`, `FAILED` (дефект кода/образа), `ERROR`
(среда), `CANCELLED`, `TIMEOUT`.

## 2. Хосты сборки

| Хост | Железо | Как исполняется arm64 | Загрузочный тест |
|---|---|---|---|
| **devbox** (по умолчанию) | x86-64, 8 ядер, 14 ГБ ОЗУ | qemu-user (binfmt `qemu-aarch64`) | QEMU TCG (эмуляция) |
| **pi5** (ADR-0019) | Raspberry Pi 5, 4 ядра, **2 ГБ ОЗУ** + подкачка 8 ГБ на NVMe 256 ГБ | нативно | QEMU + **KVM** |

Выбор хоста — переменные окружения, ключ задания один и тот же:

```sh
export SHBUILD_HOST=shbuild@pi5.local SHBUILD_BULK_HOST=shbuild@pi5.local    # хост команд / хост больших передач
python3 tools/build/shbuild.py submit          # без переменных — devbox (devbox / devbox-lan)
```

Адрес pi5 — DHCP, закреплён в роутере (на 2026-11-02 — 192.168.1.194); вход по ключу Mac (`shbuild@`, root тоже).
Сервис ставится и обновляется с Mac на **каждый** хост отдельно: `python3 tools/build/shbuild.py install` (код, каталоги,
набор машины загрузочного теста, открытые ключи `dev.pub`/`deploy.pub`; закрытые ключи с Mac не уходят). После любого
изменения в `tools/build/` нужно переустановить сервис на обоих хостах; во время работающего задания — не ставить.

Что нужно хосту: Linux с Docker, ≥ 40 ГБ диска, **ОЗУ + свободная подкачка на диске ≥ 6 ГБ при ОЗУ ≥ 1 ГБ** (zram
не считается), x86-64 с binfmt qemu-aarch64 либо arm64 (тогда эмуляция не нужна). `PATH` исполнителя дополняется
`/usr/sbin:/sbin` (Debian: `debugfs`, `mke2fs` там; процесс, запущенный по SSH, их в `PATH` не имеет).

## 3. Этапы задания

| Этап | Что делает | Провал значит |
|---|---|---|
| `preflight` | диск, ОЗУ+подкачка, binfmt (только x86), Docker жив, нет чужой сборки | `ERROR` (среда) |
| `checkout` | `git worktree` коммита из пакета, `SOURCE_DATE_EPOCH` — время коммита | `ERROR` |
| `tests` | `unittest tests/tools` (как CI) | `FAILED` (код) |
| `build` | `os/docker-build.sh` в привилегированном контейнере `rpi-image-gen` (ревизия в `os/rpi-image-gen.ref`), затем `os/build.sh`: образ, `verify_image`/`verify_buildhost`, сжатие `zstd`, SBOM, `SHA256SUMS`, `build-report.json` | `FAILED`/`ERROR` |
| `gates` | `build-report` (verify_exit 0, `dirty: false`, тот же коммит), `sha256sum -c`, SBOM (SPDX 2.3), `owners_check` против эталона, `order_check` ×8 | `FAILED` |
| `boot` | загрузочный тест в QEMU (раздел 5) | `FAILED` |

Одинаково для всех: у `sheregesh-buildhost-*` (образ хоста) вместо owners/order и пакета sheregesh применяются свой
`verify_buildhost.py`, SBOM только образа и единственный сценарий `buildhost`.

**Параллельность.** Независимые пары идут вместе: `tests ‖ build` и `gates ‖ boot` (`owners_check ‖ order_check` внутри
`gates`). Провал любого этапа пары прерывает соседний (внутренний файл `abort`), итог берётся от первого по порядку
этапов; `shbuild-report.json` записывается, только если **все** этапы `PASS`. Журналы этапов:
`shbuild.py logs ID [этап]`, время каждого — в `status ID`.

## 4. Где что лежит на хосте (`~/shbuild`)

`bin/` код сервиса · `jobs/<id>/` состояние (`state.json` с полем `flags`), журналы, `cancel`/`abort` · `repo.git` ·
`inbox/` пакеты git · `artifacts/<коммит>[.<конфигурация>]/` результат · `keys/` · `baseline/owners.txt`
(необязательное переопределение; по умолчанию `tools/image/owners-baseline.txt` из ревизии) · `vm/` набор загрузочного
теста (ядро Debian, модули, busybox, образ-контейнер `shbuild-qemu`) · `tmp/` · `host.conf` (выключатели) · `worker.log`.

Артефакты: `*.img.zst`, `.sbom.zst`, `SHA256SUMS`, `build-report.json`, `verify.json`, `owners.txt` и (только при
`PASSED`) **`shbuild-report.json`** — именно его требуют `shdeploy ota-build`, `flash_sd.py` и `tools/build/gate.py`.
Хранятся: последние 5 `PASSED`, всё с файлом `keep`; артефакты провалов удаляются. Забрать на Mac (запись карты,
подпись OTA): `shbuild.py fetch ID` (Pi 5 → Mac идёт по LAN, образ не на Mac собирается).

## 5. Загрузочный тест

QEMU `virt` (aarch64) с **ядром Debian** и модулями из `vm/`, весь диск образа, настоящий systemd: пространства имён,
cgroup и песочница юнитов как на плате (в контейнере qemu-user они падают ложно). Контейнер без `--privileged`; сеть —
tap + мост + dnsmasq внутри контейнера. Проба `vm/probe.sh` печатает строки `SHBOOT ключ=значение`, `boot_check.sh`
сверяет их: упавшие юниты (кроме аппаратных `ALLOW_FAILED`), имя после загрузки/перезапуска hostnamed/продления DHCP,
время, sshd (root только по ключу), `/var`, у `offline` — ни одного пакета за подсеть и ни одного DNS-запроса.
Сценарии: `console`, `remote`, `fresh`, `offline` (продуктовый образ), `buildhost` (образ хоста: корень — раздел 2 MBR,
расширение корня, Docker отвечает, инструменты, подкачка).

Ускорение: образ распаковывается **один раз на задание** (разреженно, `SHBOOT_RAW`); ожидание пробы — по условию (адрес
получен + 5 с), не фиксированные паузы; на arm64-хосте с `/dev/kvm` гость идёт на KVM (`-cpu host`), иначе TCG;
`SHBOOT_NO_KVM=1` принудительно TCG. Не проверяется (остаётся за платами): ядро Pi, прошивка, tryboot, VideoCore, дисплей,
GPIO/UART, A/B-откат.

## 6. Выключатели и откат

Файл `~/shbuild/host.conf` на хосте: строки `имя=значение`, `#` — комментарий; нет строки — значение по умолчанию;
действует со следующего задания, переустановка не нужна. Действовавшие значения пишутся в `state.json` задания.

| Выключатель (по умолчанию) | Что делает | Откат |
|---|---|---|
| `parallel_tests` (1) | `tests` вместе с `build` | `parallel_tests=0` |
| `parallel_gates` (1) | `owners_check` вместе с `order_check` | `parallel_gates=0` |
| `parallel_boot` (1) | `gates` вместе с `boot` | `parallel_boot=0` |
| `zstd_level` (3) | уровень сжатия итогового образа (на Pi 5 `-10` стоил 118 с против 38 с) | `zstd_level=10` |

```sh
ssh shbuild@pi5.local 'echo parallel_boot=0 >> ~/shbuild/host.conf'     # откат одной строкой
```

Код откатывается обычно: `git revert <коммит>`, затем `shbuild.py install` на оба хоста. Метка
`shbuild-baseline-748s` — последняя проверенная версия до ускорений (Pi 5: 748 с, `PASSED`).

## 7. Замеры (продуктовый образ `sheregesh-lab-dev-pi4`, `PASSED`)

| Конфигурация | tests | build | gates | boot | Всего |
|---|---|---|---|---|---|
| devbox, эмуляция (до ускорений) | 17 с | 1890 с | 34–38 с | 290–410 с | ≈ 37 мин |
| Pi 5, TCG, sequential | 24 с | 686 с | 65 с | 675 с | ≈ 25 мин |
| Pi 5, KVM, образ один раз, пауза по условию | 24 с | 527 с | 68 с | 128 с | 748 с |
| + `zstd -3`, `owners ‖ order` | 24 с | 436 с | 56 с | 128 с | 644 с |
| + `tests ‖ build` | 25 с | 433 с | 55 с | 128 с | 617 с |
| + `gates ‖ boot` (текущее) | 25 с | 448 с | 62 с | 147 с | **597 с (≈10 мин)** |

`build` (≈7,5 мин) — главный остаток: установка пакетов в chroot ≈ 145 с (скачивание 199 МБ ≈ 40 с), сборка разметки A/B
≈ 107 с, SBOM (syft) ≈ 19 с, упаковка rpi-image-gen ≈ 55 с, `verify_image` ≈ 25 с, сжатие ≈ 38 с.

## 8. Пробовали и отвергли

- **`deploy.compression: none`** (без сжатия в хуке rpi-image-gen): копирование быстрее (26 → 11 с), но
  `deploy99-manifest` хеширует 7 ГБ несжатого образа (+14 с) — итог 0. Откачено.
- **Два загрузочных сценария одновременно**: на 2 ГБ ОЗУ при гейтах рядом `KVM_CREATE_VM: Cannot allocate memory` на
  старте; выигрыш всего ≈ 25 с (под нагрузкой сценарий вдвое медленнее). Откачено.
- **Кэш пакетов (apt-cacher-ng)**: ≈ 35 с, но новый постоянный сервис и https-зеркала Raspberry Pi — дорого. Не делалось.

Не пробовали: PCIe Gen 3 для NVMe (`dtparam=pciex1_gen=3`, вне спецификации; I/O-фазы сборки могли бы ускориться;
делать только при владельце рядом — при неудаче плату придётся загружать со спасательной карты); ускорение
`verify_image` через `debugfs` по смещению без копий разделов (≈ 10–20 с).

## 9. Частые причины провала

| Симптом | Причина / что делать |
|---|---|
| `preflight`: «память … ОЗУ + подкачка» | на хосте нет подкачки на диске (`swapon`); у pi5 её создаёт `shbuild-swap` после расширения корня |
| `tests`: «нет debugfs» | исполнитель запущен без `/usr/sbin` в `PATH` (исправлено в `worker_env`) — переустановить сервис |
| `gates`: owners отличаются от эталона | состав владельцев файлов образа изменился: проверить, изменить эталон `tools/image/owners-baseline.txt` осознанно, отдельным коммитом |
| `boot`: «нет журнала консоли машины» | машина не стартовала (KVM/ОЗУ); смотреть `logs ID boot`, запуск вручную с `SHBOOT_KEEP=1` |
| `boot`: упавший юнит | дефект образа; аппаратные юниты — только через `ALLOW_FAILED` с причиной |
| `FAILED@build`, `dirty: true` | собрано не из коммита; сервис собирает `git worktree`, ручные обходы запрещены |
| задание «зависло в очереди» | идёт другое: `status`; исполнитель один (`worker.lock`) |
