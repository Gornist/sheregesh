# app-stop: закрытие без платы (S3.1)

Ветка `fix/app-stop-2` от `v2` 6353c70; платы, `update/*`, `tools/deploy/*`, PKI не трогались. Итог: **PASS на уровне кода, пакета и настоящего systemd; на плате — OPEN до OTA образа ≥ 6353c70.**

## 1. Модульные тесты ретранслятора — PASS

`tests/unit/test_notify_relay_stop.py`: было 18, добавлено 5 (3406ab7) — чужой SIGTERM ребёнку → 143 (не маскируется),
чистый выход ребёнка → 0, код watchdog 134, юнит не переопределяет `KillSignal`/`WatchdogSignal`/`SendSIGKILL`/
`SuccessExitStatus`, GRACE_S по умолчанию (5 с) — зависшая группа убита, останов < `TimeoutStopSec=15`.
С `test_notify_relay.py`: macOS 28 PASS + 1 skip (PDEATHSIG), Linux (trixie, Python 3.13) 29/29 PASS.

## 2. Настоящий systemd — PASS (114/114)

`tests/systemd/app_stop/host.sh devbox-lan`: docker `debian:trixie`, systemd 257 как PID 1, рабочий каталог
`/srv/build/appstop-<метка>` (удалён вместе с контейнером и образом). Юниты генерируются из
`tools/image/units/sheregesh-console.service` (Type=notify, NotifyAccess=main, WatchdogSec=20, TimeoutStopSec=15,
Restart=always, ExecStopPost); ретранслятор и обёртка — как в пакете. Вариант a — без PAM; вариант b — `User=` +
`PAMName=login`, как на плате (ретранслятор и cage в `session-cN.scope`). Фиктивный cage (sh) + приложение на
`sheregesh.sdnotify` (READY, WATCHDOG, `adopt_pdeathsig`). Протокол: `app-stop-systemd.log`.

| Случай (a и b) | Результат |
|---|---|
| `systemctl stop` после 25 с работы (> WatchdogSec, NRestarts=0) | 0,5 с; `Result=success`, `ExecMainStatus=0`, inactive; группа пуста |
| cage и приложение игнорируют TERM / приложение заморожено | 6,0 с (SIGKILL по GRACE_S) < 15; success/0 |
| `systemctl kill --kill-whom=main -s SIGABRT` | failed, `exit-code`, 134; группа пуста |
| приложение молчит (watchdog 20 с) | failed, `Result=watchdog`, 134; группа пуста |
| при `Restart=always`: watchdog | `Failed with result 'watchdog'`, перезапуск, старой группы нет |
| cage упал (70) | failed, `exit-code`, 70; группа пуста |

Попутно: под PAM ретранслятор и ExecStopPost не видны в `journalctl -u`, а `-u X + FIELD=…` journalctl 257 отвергает —
исправлены `check_pi_deploy.journal` (+`SYSLOG_IDENTIFIER=sh`) и ручная команда в `app-stop-analysis.md` (406e20f).

## 3. Образ из v2 — исправления внутри

02a9b64, e067380, 79069f3 — предки 6353c70; `notify_relay.py` в 6353c70 = blob 5e14f7e (как в 79069f3), юнит
не менялся с a7f1b74. Путь в образ: `os/build.sh` → `tools/image/build_deb.py` (SOURCES `software/sheregesh` →
`/usr/lib/sheregesh/software/sheregesh/`, WRAPPERS `/usr/bin/sheregesh-notify-relay` = `python3 -s -m
sheregesh.notify_relay`, юниты `tools/image/units/` → `/usr/lib/systemd/system/`) → `os/layer/sheregesh-lab.yaml`
(`apt-get install pkgs/sheregesh_*_all.deb`, enable `sheregesh-console`). Локально собранный пакет: sha256
`notify_relay.py` = `61381d34…a122` (тот же файл испытан в п. 2), юнита = `0787042c…2918`; в a7f1b74 ретранслятор
`c991991c…9bb3`. CI 6353c70 (run 40132447440): пакет `g6353c70` собран, `console.relay` PASS, но **загрузка
артефактов упала — исчерпана квота хранилища Actions**: образа 6353c70 из CI нет, нужна локальная сборка/перезапуск.

## 4. Закрытие на плате (после OTA ≥ 6353c70 и COMMITTED; выполняет основная сессия)

```
sha256sum /usr/lib/sheregesh/software/sheregesh/notify_relay.py   # ожидается 61381d34…a122
T=$(date +%s); systemctl stop sheregesh-console; sleep 8
systemctl show sheregesh-console -p ActiveState,SubState,Result,ExecMainStatus
journalctl --since @$T -o cat --no-pager _SYSTEMD_UNIT=sheregesh-console.service + UNIT=sheregesh-console.service \
  + SYSLOG_IDENTIFIER=sheregesh-notify-relay + SYSLOG_IDENTIFIER=sh
pgrep -a 'cage|sheregesh_console'; systemctl start sheregesh-console
```

Ожидается: `inactive/dead`, `Result=success`, `ExecMainStatus=0`; журнал — `signal SIGTERM — stopping process group N`,
`stopped: result=success code=exited status=0`, `Deactivated successfully`; нет `status=143`/`Failed with result`;
`pgrep` пуст; после start — active. Либо `check_pi_deploy.py --only image,app-stop` → `verdict: "PASS"`.

**Остаётся аппаратным:** настоящие cage/Qt/DRM/tty1 и seatd, `TTYPath`, `Conflicts=getty@tty1`, освобождение DRM
после SIGKILL; доставка через OTA/tryboot/confirm. PASS для службы console на pi1 этим документом не выдаётся.
