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
  должен быть IPv4-литерал из частной `LAB_NET`, иначе отказ (rc 2). Слоты фиксированы: `hdmi`→`1-1 534d:2109`,
  `cvbs`→`1-2 534d:0021`, `camera`→`1-3 1d6b:0102`. Плюс `IPAddressDeny=any` / `IPAddressAllow=192.168.1.0/24` в юните
  (должно совпадать с `LAB_NET`).
- **Подменённый сервер в той же сети** (LAB-SECURITY LS-1) — три рубежа:
  1. до attach: заявленный сервером VID:PID должен совпасть со слотом, иначе сокет закрывается, attach не делается;
  2. `98-sheregesh-lab-usbip.rules`: на корневых хабах vhci `interface_authorized_default=0`; udev авторизует
     только интерфейсы класса `0e` (видео) и запускает `drivers_probe` — HID, накопитель и т. п. остаются без драйвера;
  3. после attach: на порту наш `devid`, VID:PID и все классы интерфейсов в sysfs = слот и `0e`, иначе немедленный detach.
- **Порт vhci** (LS-2, LS-3): `state.json` хранит порт и `devid`; `stop` отключает порт, только если там всё ещё
  наше устройство (после «выдернуть» со стороны shcam порт мог занять другой слот). Выбор порта, attach и detach —
  под `flock` на каталоге контроллера vhci, одновременный `start` двух слотов берёт разные порты.
- **Узлы vivid** (LS-4): udev-ссылки и `lab-vivid` опознают узел по имени **и** драйверу `vivid` — UVC-устройство
  по USB/IP может назваться `vivid-000-vid-cap`.
- **Юниты** (LS-7): `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `NoNewPrivileges`, `RestrictAddressFamilies`,
  `ProtectControlGroups/Clock/Hostname/KernelLogs`, `RestrictNamespaces/Realtime/SUIDSGID`, `LockPersonality`,
  `SystemCallArchitectures=native`. Юниты от root — `CapabilityBoundingSet=CAP_SYS_MODULE` (только `modprobe`; запись
  в sysfs vhci и узлы — по правам владельца-root). `ProtectKernelModules` нигде у root-юнитов (нужен `modprobe`),
  `ProtectKernelTunables` — у vivid и подачи, но не у usbip (нужна запись в `/sys/…/vhci_hcd.0`). Подача vivid — от
  `sheregesh-ui`, без привилегий, `Nice=10`.
- vivid — тестовый драйвер ядра без внешних входов. Загружается только по `start`, не при загрузке.

## Что проверено (2026-10-25)

- `python3 -m unittest tools/lab-video/pi2/tests/test_pi2_lab_video.py` — 27 тестов без root, на подменах:
  - правило polkit на настоящем движке JavaScript (`node`), 15 случаев; манифест ↔ файлы ↔ правило;
  - отказы конфига; цикл `usbip start/stop` против настоящего эмулятора с подменённым sysfs vhci;
  - LS-1: сервер заявил HID `046d:c52b` → отказ до attach; проверка после attach (VID:PID, класс 03, чужой devid);
  - LS-2: на переиспользованном порту чужой devid → не отключается; LS-3: два одновременных `start` → порты 0 и 1;
  - LS-4: узел с именем vivid, но драйвером uvcvideo, не принимается; LS-7: привилегии юнитов;
  - разбор DV timings (720p30, 1080i50) и имён управлений.
- **На живом ядре shcam** (Pi 5, ядро 6.18, systemd; набор поставлен по `manifest.tsv` в настоящие пути, юниты
  запускались systemd с `DEV-MODE`, после проверки всё снято):
  - `systemctl start sheregesh-lab-usbip@cvbs` и `@camera` одновременно → порты 0 и 1 (LS-3);
  - корневые хабы vhci `interface_authorized_default=0`, видеоинтерфейсы `authorized=1` и привязаны к `uvcvideo`.
    Без `drivers_probe` в правиле драйвер **не** привязывался — найдено на этой проверке и исправлено;
  - **трафик USB/IP после выхода помощника идёт** (90 кадров MJPEG) при `IPAddressDeny=any`/`IPAddressAllow` юнита —
    бывший пункт 2 списка ниже;
  - LS-2: камеру «выдернули» со стороны shcam, порт 0 занял `cvbs`, `stop` камеры → «занят devid 0x10003, наш 0x10004 —
    не отключаю», устройство cvbs на месте;
  - `sheregesh-lab-vivid`: старт в песочнице с `CapabilityBoundingSet=cap_sys_module` (modprobe, `udevadm settle`,
    `lab-vivid init`), ссылки `/dev/sheregesh-lab/*` только на узлы vivid; `stop`, пока узлы заняты подачами, →
    «Module vivid is in use», юнит failed (ожидаемо, бывший пункт 5);
  - `lab-vivid`: статусы `0x2`/`0x100`/`ENOLCK` как в CONTRACT, NTSC и 1080p30, подача даёт картинку камеры;
    отказ помощника для `SHCAM_HOST=8.8.8.8` (rc 2).

**Осталось проверить на pi2 после установки образа:**
1. polkitd применяет `51-…`: `systemctl start sheregesh-lab-usbip@hdmi` от `sheregesh-ui` проходит без пароля, `enable` — нет.
2. Подача на Pi 4: `v4l2h264dec` → `videoconvert` → `interlace` укладывается по CPU рядом с видеоприёмом блока.
3. Свежая загрузка vivid на pi2 сразу даёт `/dev/sheregesh-lab/*` (на shcam vivid был загружен заранее — проверено через
   повторное событие udev).
