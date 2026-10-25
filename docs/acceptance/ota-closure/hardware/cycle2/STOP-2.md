# Цикл 2 (образ abd9d68 после пересборки, пакет c30de791…5a82) — STOP на шаге 3 (2026-10-25)

- Шаги 0–2 PASS: ota-disable, снимки «до», install агентом 57e8fe4 (slot_guard install 44/0/0, pi1 unchanged 44/0/0),
  leftovers PASS, pre-enable 44/0/0, ota-enable.
- Шаг 3: `ota-tryboot` 05:00 → OK, перезагрузка в B. **SSH не появился за 10 мин.** Плата отвечает на ping по новому
  адресу 192.168.1.172 (MAC d8:3a:dd:69:0f:6e = pi2), порты 22, 7443, 7444 закрыты; пульт pi1 — `connecting`.
  Смена адреса DHCP указывает, что на B не применился machine-id с persistent либо загрузка остановилась до служб.
- confirm не выполнялся. Основной слот A не тронут (slot_guard 1a: p1/p2/p4 — 0 записанных секторов).
- **Восстановление требует человека:** выключить и включить питание pi2 → прошивка загрузит основной A
  (`[all] boot_partition=2`; tryboot одноразовый). Удалённого управления питанием нет. Других действий с платой
  по сети не предпринималось.
- После загрузки A: `ota-status` (ожидается ROLLED_BACK), `slot_guard --mode rollback`, health; журнал загрузки B
  (`journalctl -b -1` с A недоступен — журнал B на persistent слота B) снять для разбора причины.

## Восстановление (2026-10-25, после цикла питания владельцем)
- pi2 загрузилась в **A** (p2, 57e8fe4) на прежнем адресе 192.168.1.171; `running`, упавших юнитов нет.
- OTA: `TRYBOOT → ROLLED_BACK (прошивка загрузила прежний слот)`, active A, consistent
  ([`ota-status-after-powercycle.json`](ota-status-after-powercycle.json)); `slot_guard --mode rollback` 36/0/0
  ([`pi2-compare-rollback.txt`](pi2-compare-rollback.txt)); health 14/14.

## Разбор отказа B (цикл 2): установлено / исключено / не установлено
**Установлено:**
- B не смонтировал `/persistent`: в истории OTA нет `TRYBOOT → HEALTH_PENDING` (юнит `sheregesh-ota-boot`,
  `RequiresMountsFor=/persistent`, не выполнялся); в общем журнале на persistent нет ни одной записи этой загрузки
  (`journalctl --list-boots` на A: после `16965fa7` сразу текущая `76ef59e0`); `/persistent/slots/system_b/var`
  последний раз изменён 01:59 UTC — до tryboot (02:00 UTC).
- Userspace на B работал: в initramfs сети нет (`ipconfig` есть, но в cmdline нет `ip=`), а адрес DHCP выдан.
  Новый адрес (.172) — следствие случайного machine-id: `machine-id-sync` берёт его с persistent.
- `/persistent` монтирует сгенерированный `persistent.mount` (`What=/dev/disk/by-slot/persistent`,
  `Requires=systemd-fsck@…persistent`); его отказ → local-fs → emergency: сеть могла остаться, SSH — нет.

**Исключено (сравнение образов цикла 1, который загружался до HEALTH_PENDING, и цикла 2):**
- system_a: 24 199 путей в обоих; различия метаданных — только владелец 1000 → 0 (41 путь) и права одного хука
  (`rpi-image-crypt-install` 775 → 755); различия содержимого — `initrd.img` (только владелец в cpio),
  `/etc/shadow` (дата смены пароля), `os-release`, `rpi-issue`, `var-seed.tar.gz` (время сборки)
  ([`analysis/`](analysis/)).
- initramfs: 564 записи, содержимое побайтно одинаково; отличается только владелец `90-rpi-ab-root` (в цикле 1 —
  uid 1000). Правила udev `by-slot`, генераторы, fstab — одинаковы по содержимому.

**Не установлено:** почему `/persistent` не смонтировался. Журнал загрузки B не сохранился: при несмонтированном
persistent journald пишет только в `/run`, а оно потеряно при цикле питания. Различий образа, объясняющих отказ,
нет; остаются версии, зависящие не от образа: (1) состояние persistent после цикла 1 / отката (fsck, данные слота B);
(2) недетерминированный отказ при загрузке B (`by-slot` симлинки udev, порядок устройств); (3) отличие, которое
не видно в метаданных файлов (атрибуты/xattr, права каталогов внутри overlay).

**Что нужно для установления причины:** вывод консоли загрузки B — последовательная консоль `ttyAMA0` 115200
(GPIO14/15, USB-UART) или HDMI-монитор у pi2 во время следующей пробной загрузки. Новых tryboot без этого не
выполнять: два цикла исправления, разрешённые спринтом, исчерпаны.

## Причина установлена (E2E-13, 2026-10-25): цикл упорядочения systemd из-за `sheregesh-var-seed.service`
- Юнит (`tools/image/units/sheregesh-var-seed.service`, есть в образах цикла 1 и 2, нет в 57e8fe4):
  `After=local-fs.target` + `Before=… systemd-sysusers.service`. В trixie `systemd-sysusers` → Before
  `systemd-tmpfiles-setup-dev` → Before `local-fs-pre.target` → Before `local-fs.target` → Before var-seed → Before
  sysusers: **цикл**. systemd разрывает его, удаляя одно задание из цикла; какое — зависит от порядка обхода и от
  загрузки к загрузке разное.
- **На плате (цикл 1, B, журнал загрузки `672c5bc6` в журнале A):** `Found ordering cycle … Job
  systemd-sysusers.service/start deleted` — удалено безвредное задание, B дошёл до HEALTH_PENDING
  ([`analysis/b-cycle1-journal-order.txt`](analysis/b-cycle1-journal-order.txt)). На A (57e8fe4) циклов нет.
- **Без платы (образ цикла 2, `systemd-analyze verify`, 8 прогонов):** цикл в каждом прогоне, удаляемое задание
  меняется: sysusers, sheregesh-var-seed, tmpfiles-setup-dev, local-fs-pre.target и **local-fs.target**
  ([`analysis/cycle-c2.txt`](analysis/cycle-c2.txt)); образ CI 57e8fe4 — циклов нет
  ([`analysis/cycle-ci.txt`](analysis/cycle-ci.txt)); образ цикла 2 с исправленным юнитом — циклов нет
  ([`analysis/cycle-c2-patched.txt`](analysis/cycle-c2-patched.txt)).
- Это объясняет, почему побайтно одинаковые (кроме владельцев) образы циклов 1 и 2 повели себя по-разному.
  Удаление `local-fs.target`/`local-fs-pre.target` ломает порядок монтирования `/persistent`, `/var` и привязок
  (machine-id-sync не выполнился → случайный machine-id → новый адрес DHCP; журнал не попал на persistent; службы,
  ssh — нет). Какое именно задание удалено в загрузке цикла 2 — не восстановить (журнал был только в `/run`).
- Атрибуты образов (полный дамп [`analysis/attr_dump.sh`](analysis/attr_dump.sh)): xattr, capabilities (`getcap -r`:
  одна запись `gst-ptp-helper`), suid/sgid/sticky (19 путей), `lsattr`, initramfs внутри `boot.img`, ядро, cmdline —
  в циклах 1 и 2 идентичны ([`analysis/attr-summary.txt`](analysis/attr-summary.txt)); `chown -R` атрибутов не сбросил.
- **Исправление:** `systemd-sysusers.service` убран из `Before=` (sysusers на корне только для чтения всё равно
  пропускается — `ConditionNeedsUpdate=/etc` — и пишет /etc, не /var). Гейты: `verify_image` `boot.order`
  (статически, в сборке и CI) и `tools/image/order_check.sh` (systemd-analyze на корне образа, обязательный шаг
  перед пакетом). L3 (файловое испытание) загрузку не выполняет и цикл видеть не может.
