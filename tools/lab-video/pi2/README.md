<!--
SPDX-FileCopyrightText: 2026 Sheregesh contributors
SPDX-License-Identifier: GPL-3.0-or-later
-->
# П. 7: эмулированные источники видео на блоке pi2 (проект для dev-образа)

**Цель.** Чтобы на pi2 (`sheregesh-ui`, без root и sudo) можно было включать источники, которые эмулирует
shcam ([../README.md](../README.md), [../CONTRACT.md](../CONTRACT.md)):
- **USB/IP-устройства** (HDMI→USB `534d:2109`, CVBS→USB `534d:0021`, камера `1d6b:0102`) — на pi2 работает
  настоящий `uvcvideo`, есть подключение и отключение USB;
- **vivid на самой pi2** — аналоговый вход с PAL/NTSC и флагами `NO_SIGNAL`/`NO_H_LOCK` и HDMI-вход с DV timings,
  картинка с камеры shcam.

Root нужен для трёх вещей: `modprobe vivid`, `modprobe vhci-hcd` и записи в
`/sys/devices/platform/vhci_hcd.0/attach|detach`. Всё остальное `sheregesh-ui` делает сам (группа `video`).

Это **проект**: образ, `tools/image/**` и `os/layer/**` здесь не менялись. Встраивает их владелец образа (сессия S5)
вместе с исправлением S5-D1. OTA и запись карт — только по решению владельца.

## Состав и куда ставить в образе

Машиночитаемо — [`manifest.tsv`](manifest.tsv): по нему dev-слой раскладывает файлы, а `verify_image` проверяет
лабораторную границу (готовый фрагмент — [`../proposals/verify_image-lab-video.md`](../proposals/verify_image-lab-video.md)).

| Файл | Путь в образе | Права |
|---|---|---|
| `bin/sheregesh-lab-video` | `/usr/lib/sheregesh/lab-video/sheregesh-lab-video` | 0755 root |
| `bin/lab-vivid` | `/usr/lib/sheregesh/lab-video/lab-vivid` | 0755 root |
| `../usbip_attach.py` | `/usr/lib/sheregesh/lab-video/usbip_attach.py` (помощник импортирует его из своего каталога) | 0644 root |
| `etc/lab-video.conf` | `/etc/sheregesh/lab-video.conf` | 0644 root |
| `etc/modprobe-sheregesh-lab-vivid.conf` | `/etc/modprobe.d/sheregesh-lab-vivid.conf` | 0644 |
| `etc/99-sheregesh-lab-vivid.rules` | `/etc/udev/rules.d/99-sheregesh-lab-vivid.rules` | 0644 |
| `etc/51-sheregesh-lab-video.rules` | `/etc/polkit-1/rules.d/51-sheregesh-lab-video.rules` | 0644 |
| `units/sheregesh-lab-vivid.service`, `units/sheregesh-lab-vivid-feed@.service`, `units/sheregesh-lab-usbip@.service` | `/usr/lib/systemd/system/` | 0644, **не включать** (`enable` не нужен) |

Только для dev-слоя (`sheregesh-dev-access`): в рабочем образе этих файлов быть не должно. Каждый юнит дополнительно
стоит на `ConditionPathExists=/etc/sheregesh/DEV-MODE`. Пакеты не нужны: Python 3, `gstreamer1.0-plugins-{base,good,bad}`,
шрифты и модули `vivid`, `vhci-hcd`, `usbip-core` в образе pi2 уже есть (ядро 6.18.50+rpt-rpi-v8). `v4l-utils` нет, поэтому
`lab-vivid` работает через ioctl.

## Как пользоваться на pi2 (`sheregesh-ui`)

```
systemctl start sheregesh-lab-usbip@hdmi          # «вставить» HDMI→USB 534d:2109 (cvbs | camera — так же)
systemctl stop sheregesh-lab-usbip@hdmi           # «выдернуть»
systemctl restart sheregesh-lab-usbip@hdmi        # вставить заново после shcam-sig usb hdmi-ms2109 unplug

systemctl start sheregesh-lab-vivid sheregesh-lab-vivid-feed@cvbs sheregesh-lab-vivid-feed@hdmi
/usr/lib/sheregesh/lab-video/lab-vivid status
/usr/lib/sheregesh/lab-video/lab-vivid cvbs nosignal|nolock|cable-out|testpattern|ok
/usr/lib/sheregesh/lab-video/lab-vivid cvbs ntsc  # и pal; подача перезапускается сама
/usr/lib/sheregesh/lab-video/lab-vivid hdmi 1920x1080p30
systemctl stop sheregesh-lab-vivid                # выгрузить vivid (подачи останавливаются раньше — PartOf)
```

Узлы vivid: `/dev/sheregesh-lab/{cvbs,hdmi}-{cap,out}` (номера `/dev/videoN` плавают, `by-path` у двух экземпляров
vivid совпадает). Устройства USB/IP получают обычные `by-id`/`by-path` на `vhci_hcd.0`.

## Лабораторная граница

В рабочий (строгий) образ п. 7 не попадает. Файлы ставит только hook слоя `sheregesh-dev-access`, а юниты не
запускаются без `/etc/sheregesh/DEV-MODE`. Как это проверять — `verify_image`
([`../proposals/verify_image-lab-video.md`](../proposals/verify_image-lab-video.md)):
- **строгий** вариант: ни одного пути из `manifest.tsv` и ни одного правила polkit с `sheregesh-lab-`
  (`labvideo.absent`);
- **dev** вариант: все пути на месте (`labvideo.files`), правило — ровно шесть юнитов и `start`/`stop`/`restart`
  (`labvideo.polkit`), юниты при `DEV-MODE` и не включены ни в один `*.wants` (`labvideo.units`).

## Безопасность

- **polkit** (`51-…`): `sheregesh-ui` разрешены только `start`/`stop`/`restart` шести перечисленных юнитов, экземпляры
  указаны полностью. `enable`, другие экземпляры шаблонов и другие пользователи → `NOT_HANDLED`. Правило `50-…` не меняется.
- **USB/IP — это ядерная поверхность**: подключённое по сети устройство обслуживает USB-стек ядра pi2. Поэтому сервер
  не задаётся пользователем. Помощник берёт только `SHCAM_HOST` из `/etc/sheregesh/lab-video.conf` (файл root), это
  должен быть IPv4-литерал из частной `LAB_NET`, иначе отказ (rc 2). Слоты фиксированы: `hdmi`→`1-1`, `cvbs`→`1-2`, `camera`→`1-3`.
  Плюс `IPAddressDeny=any` / `IPAddressAllow=192.168.1.0/24` в юните (должно совпадать с `LAB_NET`).
- **Юниты**: `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `NoNewPrivileges`, `RestrictAddressFamilies`.
  `ProtectKernelTunables`/`ProtectKernelModules` намеренно **не** включены: нужны запись в `/sys/…/vhci_hcd.0` и `modprobe`.
  Подача vivid работает от `sheregesh-ui`, с `Nice=10`.
- vivid — тестовый драйвер ядра без внешних входов. Загружается только по `start`, не при загрузке.

## Что проверено (2026-10-25)

- `python3 -m unittest tools/lab-video/pi2/tests/test_pi2_lab_video.py` — 11 тестов:
  - правило polkit на настоящем движке JavaScript (`node`), 15 случаев;
  - проверки конфига и отказы помощника;
  - цикл `usbip start/stop` против настоящего эмулятора с подменённым sysfs vhci;
  - разбор DV timings (720p30, 1080i50) и имён управлений.
- **На живом ядре shcam** (Pi 5, та же раскладка vivid, настоящий vhci):
  - `lab-vivid`: сигналы дают статусы `0x2` и `0x100` и `ENOLCK`, как в CONTRACT; `cvbs ntsc` и `hdmi 1920x1080p30` переключают узлы;
  - `lab-vivid feed cvbs` даёт картинку камеры на входе (там `avdec_h264`, на Pi 4 будет `v4l2h264dec`);
  - `sheregesh-lab-video usbip start/stop hdmi` подключает и отключает `534d:2109` через vhci;
  - для `SHCAM_HOST=8.8.8.8` — отказ (rc 2).

**Не проверено, проверить на pi2 после сборки образа:**
1. polkitd применяет `51-…`: `systemctl start sheregesh-lab-usbip@hdmi` от `sheregesh-ui` проходит без пароля, `enable` — нет.
2. `IPAddressAllow` для сокета, который после `attach` передан ядру: идёт ли трафик USB/IP после того, как процесс помощника
   завершился (юнит `RemainAfterExit`, cgroup жива). Если фильтр мешает — убрать `IPAddress*` из `sheregesh-lab-usbip@`
   и оставить проверку адреса в помощнике.
3. `udevadm settle` внутри песочницы `sheregesh-lab-vivid` (AF_NETLINK разрешён) и появление `/dev/sheregesh-lab/*`.
4. Подача на Pi 4: `v4l2h264dec` → `videoconvert` → `interlace` укладывается по CPU рядом с видеоприёмом блока.
5. `systemctl stop sheregesh-lab-vivid`, пока видеоприём блока держит узел vivid, — ожидается отказ `modprobe -r`.
   Сначала переключить блок на другой источник.
