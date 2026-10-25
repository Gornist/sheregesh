<!--
SPDX-FileCopyrightText: 2026 Sheregesh contributors
SPDX-License-Identifier: GPL-3.0-or-later
-->
# S5: лабораторные источники видео pi2 и S5-D1 — интеграция и безопасность (агент B)

Ветка `s5-ota-b` от `origin/v2` `f7801a4`, 2026-10-25. Проверены коммиты `59dd462` (tools/lab-video, shcam),
`b7534a4` (root-помощник pi2, `lab-vivid`, юниты, polkit), `5811ab5` (`manifest.tsv`, граница), их встраивание
`bd74c8c` (`os/build.sh`, `os/layer/sheregesh-dev-access.yaml`, `verify_image` labvideo) и S5-D1 `3ee69dd`.
Код не менялся: только проверка. Предлагаемые исправления — в §3, ни одно не применено.

Образ: devbox, `~/shimg/src/build/image/sheregesh-lab-dev-pi4/sheregesh-lab-dev-pi4.img.zst` (сборка `bd74c8c`,
`sha256sum -c SHA256SUMS` — OK). Разделы `system_a` и `persistent` монтировались `ro,noload` в одноразовом привилегированном
контейнере, как в `tools/image/owners_check.sh` ([`lab-security/inspect.sh`](lab-security/inspect.sh),
[`inspect2.sh`](lab-security/inspect2.sh)). На devbox удалялись только свои временные файлы.

## 1. Итог

| Пункт | Итог |
|---|---|
| `manifest.tsv` ↔ файлы dev-образа | **PASS**: 10 из 10, права по манифесту, `root:root`, sha256 совпадают с `origin/v2` |
| Лабораторное только в dev-образе | **PASS** по механизму, а не по совпадению |
| Root-помощник и `lab-vivid` от root | **PASS** с замечаниями P2 (LS-2, LS-3, LS-4) |
| Ядерная поверхность USB/IP (vhci) | **P2** (LS-1): устройство из сети стенда без авторизации интерфейсов |
| polkit 51 вместе с 50 | **PASS**: 231 случай на JS, расширения прав нет; у 50 есть старое замечание P2 (LS-6) |
| Юниты `sheregesh-lab-*` | **PASS**; дополнительное ужесточение — P2 (LS-7) |
| S5-D1 на юните из образа, systemd 257 | **PASS**: link.json, роль и ключи только для чтения; замечание P2 (LS-5) |
| Зависимости в образе | **PASS** (одно условие: `v4l2h264dec` регистрируется только на плате) |
| `scan.py secrets` / `scope` | **PASS**: 0 / 0 |

**P0 и P1 нет.** С точки зрения безопасности образ годится для лабораторного dev-использования в сети стенда
владельца. Перед работой в сети, где есть недоверенные узлы, нужно закрыть LS-1. В строгий образ лабораторное не
попадает. В пакет приложения (OTA) тоже: в `.deb` 0 таких файлов.

## 2. Проверки

| # | Проверка | Метод | Итог | Доказательство |
|---|---|---|---|---|
| 1 | Все 10 путей `manifest.tsv` есть в `system_a` dev-образа | `stat` в смонтированном разделе | PASS | [`image-inspect-1.txt`](lab-security/image-inspect-1.txt) §manifest-files |
| 2 | Права совпадают с манифестом: 0755 у `sheregesh-lab-video` и `lab-vivid`, 0644 у остальных; владелец `root(0):root(0)`; каталог `/usr/lib/sheregesh/lab-video` 0755 root | `stat -c '%a %U(%u):%G(%g)'` | PASS | там же |
| 3 | Содержимое файлов образа — ровно файлы репозитория (`origin/v2`, без изменений после `bd74c8c`) | sha256 образа ↔ `shasum` в рабочей копии; `git diff bd74c8c origin/v2` по путям пуст | PASS (12/12, включая `sheregesh-remote-net.service` и `50-sheregesh-dev.rules`) | там же, §manifest-files |
| 4 | `/etc/sheregesh` 0755 root, `lab-video.conf` 0644 root; корень монтируется `ro` (fstab) | `stat`, `/etc/fstab` образа | PASS: `sheregesh-ui` подменить конфиг не может, и при работе он неизменяем | `image-inspect-1.txt` §etc-sheregesh-listing, §fstab |
| 5 | Лабораторных юнитов нет ни в одном `*.wants` (`/etc` и `/usr/lib`), нет в preset; лабораторных файлов вне манифеста нет | `find … -path '*.wants/*'`, grep preset, `find -name '*sheregesh-lab*'` | PASS | §wants-all, §presets, §lab-anywhere; [`image-inspect-2.txt`](lab-security/image-inspect-2.txt) §wants-dirs |
| 6 | Строгий вариант: лабораторного нет | CI run 40216454930 (`headSha bd74c8c`): `labvideo.absent` | PASS «0 из 10». Run в статусе failure только из-за квоты upload-artifact | [`ci-and-scan.txt`](lab-security/ci-and-scan.txt) |
| 7 | Граница dev/strict держится механизмом | Чтение `os/config/*.yaml`, мета слоёв, `os/build.sh` | PASS. Файлы ставит только customize-hook слоя `sheregesh-dev-access`. Этот слой подключает только `sheregesh-lab-dev-pi4.yaml`: он подменяет `layer.net`, а строгий конфиг — `sheregesh-net-lab`. Оба слоя категории `net` и оба `Provides: sheregesh-net`, поэтому в одной сборке может быть только один. Больше никто не ссылается на `sheregesh-dev-access.d`. Подготовка `lab-video/` в `build.sh` идёт только в ветке `*-dev-*`, а для строгой сборки остаток от прошлой dev-сборки безвреден: hook не выполняется. Хук `install -o 0 -g 0 -m $mode`, ошибка → сбой сборки (пайплайн — последняя команда хука). Пакет приложения лабораторного не содержит | `os/config/sheregesh-lab{,-dev}-pi4.yaml`, `os/layer/*.yaml` (X-Env-Layer-*), `ci-and-scan.txt` (dpkg-deb) |
| 8 | Помощник: нет пользовательского ввода | Код + разбор аргументов | PASS. Аргументы фиксированы в `ExecStart/ExecStop` (`%i` из имени экземпляра, а polkit пускает только hdmi, cvbs и camera). argparse `choices` отвергает `../../etc`, `hdmi;id` и прочее (rc 2). Проверка `euid==0`. `#!/usr/bin/python3 -I`, импорт только из root-каталога на ro-разделе. Окружение юнита `sheregesh-ui` задать не может: `set-environment` и `set-property` не разрешены (п. 13) | [`conf_probe.txt`](lab-security/conf_probe.txt) |
| 9 | Подмена `/etc/sheregesh/lab-video.conf` (только root) | `usbip_target`/`read_conf` на 14 вариантах | PASS. Отвергаются: вне LAB_NET, `0.0.0.0/0`, CGNAT, имя хоста, IPv6, ведущие нули, LAB_NET с битами хоста, порт 0/70000/hex, пустой конфиг, строка без `=`. Принимаются 127/8 и 169.254/16 (`is_private`), но их отрезает `IPAddressAllow=192.168.1.0/24` юнита. `SHCAM_RTSP` попадает в конвейер gst через `shlex.split`, но конвейер работает от `sheregesh-ui` в песочнице подачи. Повышения нет | `conf_probe.txt` |
| 10 | Неверный `RUNTIME_DIRECTORY` | Код | PASS. Значение задаёт systemd (`RuntimeDirectory=sheregesh-lab-usbip-%i`, root 0755). Без него — `/run/sheregesh-lab-usbip-<slot>` (slot из `choices`). Последствие подмены (только root) — запись и удаление файла `port` в другом каталоге | `pi2/bin/sheregesh-lab-video` |
| 11 | Гонки и состояние vhci | Код + демонстрация на подменённом sysfs | **P2**: LS-2 — устаревший порт, `stop` одного слота отключает чужое устройство (воспроизведено); LS-3 — параллельный `start` двух слотов выбирает один свободный порт | [`stale_port.txt`](lab-security/stale_port.txt) |
| 12 | Ядерная поверхность USB/IP и vivid | Анализ кода, состав групп в образе | **P2** (LS-1, LS-4) — см. §3. vivid и vhci-hcd в строгом образе загрузить некому: модули без аппаратных псевдонимов, юнитов и polkit нет | `image-inspect-1.txt` §modules, §group |
| 13 | polkit 51 + 50 на движке JS | `node` + [`polkit-chain.js`](lab-security/polkit-chain.js): правила загружаются в порядке имён файлов, как в polkitd (duktape, polkitd 126) | PASS: 231 случай, 19 YES (18 = 6 юнитов × start/stop/restart; 1 = `sheregesh-deploy@install` start из 50). Отказ (не YES): set-property, kill, reset-failed, clean, freeze, reload*, try-restart, bind, mount-image, enable/mask/edit, verb/unit не заданы (например, транзиентные), другие экземпляры (`@x`, `@hdmi2`, `@`, `feed@camera`), `.socket`/`.timer`, без суффикса, регистр, пробел, `\x2d`. Действия `manage-unit-files` (enable/mask), `reload-daemon`, `set-environment`; пользователи root, `sheregesh`, nobody, `sheregesh-ui ` с пробелом, регистр. Правило 51 прав 50 не расширяет и не пересекается с ним | [`polkit-chain.txt`](lab-security/polkit-chain.txt); тесты проекта: `python3 -m unittest tools/lab-video/pi2/tests/test_pi2_lab_video.py` — 14 OK, `tests.tools.test_image_tools` — 19 OK |
| 14 | Транзиентный юнит с именем лабораторного | Анализ systemd 257 | PASS: у всех шести имён есть фрагмент на диске, поэтому `StartTransientUnit` с таким именем отвергается («already loaded or has a fragment file»). `systemctl edit` и drop-in требуют записи в `/etc` (root, ro) | — |
| 15 | Юниты: песочница | Чтение юнитов в образе | PASS. У всех трёх: `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `NoNewPrivileges`, `RestrictAddressFamilies`, `IPAddressDeny=any`. У usbip и подачи — `IPAddressAllow=192.168.1.0/24`, у vivid сети нет. `ConditionPathExists=/etc/sheregesh/DEV-MODE` у всех, у usbip и подачи ещё `…/lab-video.conf`. Секции `[Install]` нет. Подача работает от `sheregesh-ui`, `Nice=10`. Ужесточение — LS-7 | `image-inspect-1.txt` §units-lab |
| 16 | S5-D1: что даёт `ReadWritePaths` каталога конфигурации | Юнит **из образа** (sha256 `6e53bd6e…` = репозиторий) в контейнере debian:trixie с **systemd 257.13** (версия образа) как PID 1, вместо `sheregesh-remote-emu` — зонд записей | PASS. Запись `video-input.json` (tmp+rename) проходит. Запись, rename поверх, unlink, rename и hardlink `link.json`/`remote.role` — EROFS/EBUSY/EXDEV. `device.key`, каталог link, `~/.ssh`, `~/.bashrc`, `~/.config/*`, `/etc/sheregesh` — EROFS. **Создать можно**: `console.role`, подкаталог, симлинк — LS-5. Подтверждает проверку на systemd 259 (`docs/acceptance/s5-video/hw/s5-d1-sandbox-test.txt`) | [`d1-systemd257.txt`](lab-security/d1-systemd257.txt), [`d1-setup.sh`](lab-security/d1-setup.sh), [`d1-fake-emu.py`](lab-security/d1-fake-emu.py) |
| 17 | Зависимости помощника и подачи — по содержимому образа | dpkg status (`persistent/slots/system_a/var/lib/dpkg/status`), каталог плагинов gst, строки `.so`, `/usr/lib/modules` | PASS. python3 3.13.5; gstreamer1.0-plugins-base/good/bad 1.26.2; **gstreamer1.0-tools** (`gst-launch-1.0`, `gst-inspect-1.0`, нужны `lab-vivid`); polkitd 126 (duktape); udev/systemd 257.13; kmod 34.2; fonts-dejavu-core (timeoverlay). Элементы: v4l2sink (video4linux2), videoconvert/videoscale (videoconvertscale), interlace, rtspsrc, rtph264depay, h264parse (videoparsersbad), capssetter (debug), timeoverlay (pango), videorate. `v4l2h264dec` создаётся во время работы из шаблона `v4l2%sdec` в `libgstvideo4linux2.so` при наличии `/dev/video1x` модуля `bcm2835-codec.ko.xz` (есть). Статически это не проверить, на pi2 — `gst-inspect-1.0 v4l2h264dec`. `avdec_h264` (gst-libav) нет: запасной ветки на Pi 4 нет. Модули ядра 6.18.50+rpt-rpi-v8: `vivid.ko.xz` (+ v4l2-tpg и прочие по modules.dep), `vhci-hcd.ko.xz`, `usbip-core.ko.xz`, `uvcvideo.ko.xz`. Ни один не встроен и не загружается при старте. `sheregesh-ui` в группах video, render, input, plugdev, adm | `image-inspect-1.txt` §gst-plugins, §modules, §bins; `image-inspect-2.txt` §dpkg, §v4l2-strings, §polkit-js |
| 18 | Сканер | `python3 tools/security/scan.py secrets` и `scope` | PASS: 0 и 0 находок в 1282 файлах | `ci-and-scan.txt` |

## 3. Замечания и предлагаемые исправления (не применены)

**LS-1 (P2, dev). USB/IP без авторизации интерфейсов.** `usbip start` отдаёт ядру pi2 устройство, которое описывает
сервер на `SHCAM_HOST:3240`. Протокол USB/IP без аутентификации, поэтому любой узел /24, подменивший 192.168.1.193 (ARP),
или скомпрометированная shcam подставит что угодно. К устройству привяжется любой драйвер класса:
- HID — нажатия клавиш в сеансе киоска;
- usb-storage;
- usbnet — новый интерфейс (networkd настраивает только `eth0`).

Кроме того, `vhci-hcd` разбирает ответы URB из сети. `IPAddressAllow` от подмены внутри /24 не защищает. Срабатывает
только по явному `start` от `sheregesh-ui` и только в dev-образе. Минимальное исправление: правило udev в манифесте dev-слоя
(проверить на pi2):
1. для корневых хабов vhci — `interface_authorized_default=0`;
2. интерфейсы с `bInterfaceClass=="0e"` (видео) и `01` (аудио у адаптеров, если нужно) — `authorized=1`.

Дополнительно, дёшево: в `usbip_attach.attach` сверять `vid:pid` из ответа `OP_REQ_IMPORT` с ожидаемым для слота
(hdmi `534d:2109`, cvbs `534d:0021`, camera `1d6b:0102`) и отказывать до записи в `attach`. Сервер может солгать, поэтому
это только фильтр ошибок конфигурации.

**LS-2 (P2). Устаревший номер порта vhci.** `usbip_stop` читает из `$RUNTIME_DIRECTORY/port` только номер. `detach()`
не сверяет, чьё устройство на порту. Сценарий: hdmi подключён к порту 0, shcam «выдёргивает» его, cvbs `start` занимает
порт 0, затем `stop` hdmi отключает cvbs (воспроизведено: [`stale_port.txt`](lab-security/stale_port.txt)). Исправление:
писать `"{vport} {devid}"` и перед `detach` сверять колонку `dev` строки порта в `vhci_hcd.N/status`. При несовпадении —
только удалить файл.

**LS-3 (P2). Параллельный `start` двух слотов.** Оба процесса читают `status` и берут один `free[0]`. Ядро отвергнет
второй `attach` (порт занят), второй юнит уйдёт в failed — состояние не портится. Исправление: `fcntl.flock` на
`/run/lock/sheregesh-lab-usbip.lock` вокруг выбора порта и `attach` (нужен `ReadWritePaths=/run/lock`) или повтор со
следующим свободным портом.

**LS-4 (P2). Узлы vivid опознаются только по имени.** `lab-vivid` и правило udev ищут `vivid-00N-vid-{cap,out}` по
`/sys/class/video4linux/*/name`, а у `uvcvideo` это имя берётся из строки продукта USB. Устройство по USB/IP (LS-1) может
назваться `vivid-000-vid-cap`. Тогда `lab-vivid init` (от root, `ExecStartPost`) пошлёт ему фиксированные ioctl, а
`/dev/sheregesh-lab/cvbs-cap` укажет на него. Исправление: в правиле udev `DRIVERS=="vivid"`, в `node()` — проверка
`readlink(…/device/driver)` с базовым именем `vivid`.

**LS-5 (P2). S5-D1: каталог конфигурации открыт на создание.** Сетевой процесс `sheregesh-remote-net` теперь может
создать в `~/.config/sheregesh` любой новый файл, подкаталог или симлинк. Среди таких файлов — `console.role`, маркер роли:
`slot_guard` сохраняет его между слотами OTA. Сейчас последствий нет: `installer.role()` сначала смотрит `remote.role`, а
он только для чтения, и `sheregesh-remote-emu` при `remote.role` не стартует. Но после переразметки роли владельцем
оставленный `console.role` сработает. Минимальное исправление: хранить `video-input.json` в отдельном подкаталоге
(например, `~/.config/sheregesh/video/`) и давать `ReadWritePaths` только ему. `ReadOnlyPaths` для отсутствующего файла
не работает.

**LS-6 (P2, существовало до проверяемых коммитов).** `50-sheregesh-dev.rules` даёт `sheregesh-ui` любой `verb` на
`sheregesh-console`, `-remote-emu`, `-remote-net` и `-health`, в том числе `set-property`. Через него во время работы можно
поменять `IPAddressAllow/IPAddressDeny/DeviceAllow` у `sheregesh-remote-net` и снять сетевой фильтр. Правило 51 этого не
расширяет. Исправление: ограничить verb значениями start/stop/restart/kill (kill нужен для SIGHUP перечитывания
`video-input.json`, `software/sheregesh/video.py`).

**LS-7 (P2, ужесточение).** Юниты vivid и usbip работают от root со всеми capabilities. Предложение (проверить на pi2):
- `CapabilityBoundingSet=CAP_SYS_MODULE` (запись в sysfs vhci и `/dev/video*` идёт по DAC владельца root);
- `ProtectKernelLogs=yes`, `ProtectClock=yes`, `ProtectHostname=yes`, `RestrictNamespaces=yes`, `LockPersonality=yes`,
  `SystemCallArchitectures=native`, `RestrictRealtime=yes`;
- для подачи — `PrivateDevices=no` оставить, `DevicePolicy=closed` + `DeviceAllow=char-video4linux rw`.

**Мелкое (информация).**
- `verify_image labvideo.units` смотрит только `/etc/systemd/system/*.wants`, а `/usr/lib/systemd/system/*.wants` и preset
  не проверяет (проверено вручную, п. 5).
- `labvideo.files` проверяет наличие файлов, но не права (проверено вручную, п. 2). Можно сверять `%a` по третьей колонке
  манифеста.
- `IPAddressAllow` юнитов и `LAB_NET` в конфиге дублируются вручную.
- В README п. 7 указано «11 тестов», сейчас их 14.

## 4. Не проверено (нужна плата pi2 с этим образом)

- polkitd на плате: `systemctl start sheregesh-lab-usbip@hdmi` от `sheregesh-ui` проходит без пароля, `enable` и
  `set-property` — нет (на движке JS проверено, п. 13).
- `IPAddressAllow` для сокета, переданного ядру после `attach`.
- `udevadm settle` в песочнице vivid.
- Регистрация `v4l2h264dec`.
- Предложения LS-1/LS-7 — на ядре pi2.
