# app-stop: анализ кода выхода ретранслятора (S3.1, дефект OPEN)

Ветка `fix/app-stop` от `v2` a06f9c6. Платы не трогались; OTA, образ, PKI и юниты не менялись.

## Причина и исправление

`systemctl stop sheregesh-console` → главный PID (`sheregesh-notify-relay`, код из `/usr/lib/sheregesh`)
возвращал `128+15 = 143` → `Failed with result 'exit-code'`. Исправлено в 02a9b64 (код 0 на SIGTERM/SIGINT).
Проверено: 02a9b64 — предок a7f1b74, `notify_relay.py` и юнит в a7f1b74 и v2 совпадают; в 57e8fe4 исправления нет.

## Проверка путей (код `notify_relay.main`, тесты `tests/unit/test_notify_relay_stop.py`)

| Путь | Код | Группа после выхода |
|---|---|---|
| SIGTERM, cage выходит по TERM (с кодом 0 или 1) | 0 | пуста |
| SIGTERM, cage игнорирует TERM / заморожен → SIGKILL через GRACE_S | 0 | пуста |
| SIGTERM, cage вышел, выживший в группе игнорирует TERM | 0, без ожидания GRACE_S | пуста |
| SIGTERM, ребёнок уже мёртв (зомби, не собран) | 0 | пуста |
| SIGINT; повторный SIGTERM | 0 | пуста |
| SIGABRT (watchdog), в т.ч. с игнором TERM; SIGTERM после SIGABRT | 134 | пуста |
| ребёнок упал сам (код N) / убит сигналом S | N / 128+S | пуста |

На 57e8fe4 падают 11 из 18 новых тестов (143), на a7f1b74 — 1 (дыра ниже), на ветке — 0.

**Найденная дыра (исправлена в e067380).** Уборка своего сокета в `finally` (`os.unlink`/`os.rmdir`) при
исчезнувшем каталоге бросала `FileNotFoundError` и подменяла код 0 на 1 — юнит снова failed. Каталог —
`RuntimeDirectory=/run/sheregesh-ui`, он же `XDG_RUNTIME_DIR` сеанса cage. Уборка теперь не влияет на код; добивание
группы SIGKILL перенесено в `finally` (срабатывает и при исключении в цикле). Вероятность на плате низкая; a7f1b74
для закрытия app-stop достаточно, правка — в следующий образ.

**Остаточные (не дыры).** (1) SIGTERM до установки обработчиков (старт интерпретатора, сразу после `Popen`):
ретранслятор умирает от SIGTERM — systemd считает это чистым выходом, cage добивается PDEATHSIG. (2) Ребёнок в
D-состоянии не умирает и от SIGKILL: ретранслятор ждёт, через TimeoutStopSec systemd убивает его → failed (timeout);
это честный отказ. (3) Ребёнок упал сам в доли секунды до SIGTERM — вернётся код ребёнка (гонка, окно — микросекунды).

## Юнит (только предложение, не изменён)

- `SuccessExitStatus=143` — защита в глубину от регресса ретранслятора; но приходит тем же образом, что и сам
  ретранслятор, т. е. не ускоряет закрытие, и маскирует как успех гибель cage от чужого SIGTERM (Restart=always
  всё равно перезапустит). Рекомендация: не добавлять сейчас; регресс ловят новые тесты.
- `KillMode` оставить по умолчанию: cage и приложение в session-N.scope, KillMode до них не дотягивается; группу
  останавливает ретранслятор.
- `TimeoutStopSec=15` > GRACE_S 5 + опрос 0,5 с — запас достаточен; `TimeoutAbortSec` наследует 15 с. Инвариант
  закреплён тестом `test_unit_stop_timeout_covers_relay_grace`.

## Доставка и закрытие на плате

DEV Deploy исправление не доставляет: `/usr/bin/sheregesh-notify-relay` всегда исполняет
`/usr/lib/sheregesh/software/sheregesh/notify_relay.py` (launcher выбирает выпуск только для самого пульта).
Путь — образ: OTA a7f1b74 (или новее) в слот B → tryboot → confirm (COMMITTED).

Проверку делать **после COMMITTED**: останов пульта в HEALTH_PENDING может провалить критерии подтверждения.
Команда (от машины владельца; pi2 — после разрешения основной сессии):

```
python3 tools/probe/check_pi_deploy.py --board pi2 --host 192.168.1.171 --out docs/acceptance/s3.1/pi2/deploy-ota-b --only image,app-stop
```

или вручную на плате:

```
T=$(date +%s); systemctl stop sheregesh-console; sleep 8
systemctl show sheregesh-console -p ActiveState,SubState,Result,ExecMainStatus
journalctl --since @$T -o cat --no-pager -u sheregesh-console + SYSLOG_IDENTIFIER=sheregesh-notify-relay
pgrep -a 'cage|sheregesh_console'; systemctl start sheregesh-console
```

Ожидается: `image` — пакет из a7f1b74+; `ActiveState=inactive`, `SubState=dead`, `Result=success`,
`ExecMainStatus=0`; в журнале `notify-relay: signal SIGTERM — stopping process group N`,
`sheregesh-console stopped: result=success code=exited status=0`, `Deactivated successfully`; нет `status=143` и
`Failed with result`; `pgrep` пуст; после start — `active`. app-stop.json: `stopped_state: "inactive"`,
`verdict: "PASS"` → дефект закрыт.
