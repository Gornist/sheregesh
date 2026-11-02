<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# ADR-0019. Сборочный хост shbuild на Raspberry Pi 5 с NVMe

- **Статус:** Принято (запрос владельца 2026-11-02: «нужен образ, чтобы использовать этот rpi5 как конвейер по сбору
  arm образов»; план — владелец 2026-10-30, `docs/development/ROADMAP.md`). Реализация — `os/`, `tools/build/`.
- **Связано:** ADR-0018 (сервис сборки), ADR-0002 (образ), `docs/development/BUILDHOST-PI5.md` (порядок действий).

## Решение

1. **Образ хоста — отдельная конфигурация** `os/config/sheregesh-buildhost-pi5.yaml` + слой
   `os/layer/sheregesh-buildhost.yaml`. Это не продуктовый образ: ни пульта, ни A/B, ни OTA, ни пакета sheregesh.
   Разметка Raspberry Pi OS (MBR: `BOOT` vfat 512 МиБ + `ROOT` ext4 6 ГиБ), корень при первой загрузке растёт до
   конца носителя (`shbuild-growroot`). ext4 с блоком 4 КиБ, а не 16 КиБ по умолчанию для Pi 5: такой корень
   монтируется и ядром Pi 5, и ядром Debian загрузочного теста.
2. **Состав:** Docker (`docker.io`, `docker-cli`), git, rsync, zstd, python3, `nvme-cli`, `smartmontools`, `rpi-eeprom`
   (обновление EEPROM замаскировано — только явной процедурой), zram-подкачка, аппаратный сторож systemd, журнал на
   диске, mDNS (`pi5.local`). Вход — только по ключу (root тоже, как у DEV-образов, решение 2026-10-30);
   пользователь `shbuild` в `docker` и `sudo`. PCIe x1 для NVMe включён в `config.txt` (`dtparam=pciex1`, Gen 2:
   для сборочной машины устойчивость важнее скорости).
3. **Собирается только через shbuild** (правило ADR-0018 не меняется): `shbuild.py submit --config
   sheregesh-buildhost-pi5`. Задание получает свой каталог артефактов (`<коммит>.<конфигурация>`), свою проверку
   структуры (`tools/image/verify_buildhost.py`) и свой загрузочный сценарий `buildhost` (QEMU virt: нет упавших
   юнитов, имя, DHCP, расширение корня, sshd по ключу, Docker отвечает, инструменты). Гейты owners-эталона и порядка
   юнитов относятся к A/B-образу «Шерегеша» и к хосту не применяются; SBOM образа обязателен.
4. **Сервис переносится, а не дублируется:** на arm64-хосте `preflight` не требует `binfmt qemu-aarch64`; остальной
   код сервиса тот же. Установка — `SHBUILD_HOST=pi5 SHBUILD_BULK_HOST=pi5 python3
   tools/build/shbuild.py install`. Переключение с devbox — отдельный шаг после приёмки хоста на железе.

## Не проверено (остаётся за платой)

Прошивка EEPROM и порядок загрузки NVMe; PCIe/NVMe на Waveshare M.2 HAT C; температура и скорость сборки;
загрузочный тест на KVM (`/dev/kvm` на Pi 5) — сейчас TCG, как на devbox; размер памяти платы против `MIN_MEM_GB`.
