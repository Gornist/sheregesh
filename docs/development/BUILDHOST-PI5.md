<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Сборочный хост на Raspberry Pi 5 (NVMe)

Решение — [ADR-0019](../adr/ADR-0019-build-host-pi5.md). Железо владельца: Raspberry Pi 5, Waveshare M.2 HAT C,
NVMe Hynix 256 ГБ. Образ собирает devbox (shbuild), на Mac — только запись носителя.

## 1. Получить образ

```sh
python3 tools/build/shbuild.py submit --config sheregesh-buildhost-pi5   # печатает ID
python3 tools/build/shbuild.py wait ID                                   # PASSED или явный провал
python3 tools/build/shbuild.py fetch ID                                  # → build/image/buildhost-…/
```

Ключ входа в образ — `~/shbuild/keys/dev.pub` на devbox (тот же, что у DEV-образов плат). Образ не содержит паролей.

## 2. Записать на NVMe

Вариант A (проще всего): NVMe в USB-корпусе на Mac, затем в HAT. Диск найти по `diskutil list` (**внешний, 256 ГБ**).

```sh
zstd -dc build/image/buildhost-*/sheregesh-buildhost-pi5.img.zst | sudo dd of=/dev/rdiskN bs=4m
```

Вариант B: загрузиться с любой карты Raspberry Pi OS, передать `.img.zst` на плату и записать
`zstd -dc … | sudo dd of=/dev/nvme0n1 bs=4M conv=fsync` (карту потом вынуть).

## 3. Порядок загрузки (EEPROM)

У актуальной прошивки Pi 5 порядок по умолчанию `BOOT_ORDER=0xf461`: SD → NVMe → USB. Карту не вставлять — плата
загрузится с NVMe. Если не грузится: `vcgencmd bootloader_version` (нужна прошивка не старше 2024) и
`sudo rpi-eeprom-update -a` (в образе служба обновления замаскирована, команда — вручную). Строка порядка
(`sudo rpi-eeprom-config --edit`) — `BOOT_ORDER=0xf416`, если NVMe должен идти раньше SD.

## 4. Первая загрузка

- Адрес — по DHCP, имя `shbuild-pi5` (`ssh shbuild@shbuild-pi5.local` или `ssh root@…`); хост-ключи SSH создаются
  при первой загрузке.
- Корень сам растёт до конца NVMe (`systemctl status shbuild-growroot`, `df -h /`).
- Проверка носителя: `lsblk`, `sudo nvme smart-log /dev/nvme0`, `sudo smartctl -a /dev/nvme0`; скорость PCIe —
  `sudo lspci -vv | grep -i lnksta`. Нет `nvme0` — смотреть `dmesg | grep -i -E 'pcie|nvme'`; для скорости Gen 3 в
  `config.txt` ниже `[pi5]` добавить `dtparam=pciex1_gen=3` (вне спецификации, проверить стабильность).

## 5. Перенос shbuild на хост

Только после того, как хост опознан и стабилен (приёмка — отдельным отчётом):

```sh
SHBUILD_HOST=shbuild-pi5 SHBUILD_BULK_HOST=shbuild-pi5 python3 tools/build/shbuild.py install
```

Эталон owners и ключи копируются с devbox (`~/shbuild/baseline`, `~/shbuild/keys`). Пока `SHBUILD_HOST` не задан,
сервис devbox остаётся единственным путём (ADR-0018 не меняется).
