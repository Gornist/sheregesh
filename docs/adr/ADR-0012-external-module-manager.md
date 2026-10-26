# ADR-0012. Внешний радиомодуль удалённого блока: менеджер, профили, лабораторный эмулятор

- **Статус:** Предложено (S6), 2026-10-26
- **Затронутые решения:** D6 (управление наземными платформами допускается; канал управления ещё не спроектирован), ADR-0004 (конфигурация), ADR-0007 (SHRG-LAB, CHG-012), ADR-0008 (эмуляторы и уровни испытаний), ADR-0010 (DEV deploy)
- **Затронутые требования:** не меняет требования. Приёмка спринта — JR-01…JR-10 (раздел 11); это критерии спринта, а не TEST-ID реестра: маркер `@pytest.mark.test_id` для них **не** ставится, номер JR пишется в имени теста (`test_jr05_…`) и в docstring.
- **Исполнители S6:** B — менеджер, профиль, эмулятор, протокол, агент блока, тесты L2/L3; C — GUI пульта; D — QA, приёмка на стенде.

## 1. Контекст и границы

К удалённому блоку (pi2) подключается внешний модуль формата JR (первый — RadioMaster Nomad, семейство ExpressLRS, интерфейс UART/CRSF; сведения из открытых источников — `docs/hardware/RADIOMASTER-NOMAD.md`, если он принят в репозиторий). В S6 строится **только инфраструктура**: опознание модуля, инициализация, состояние, диагностика, восстановление, настройки и их отображение на пульте. Модуль физически не подключается: единственная реализация — лабораторный эмулятор.

Жёсткие границы S6 (подробно — раздел 13): нет радиоуправляющих сигналов, нет аппаратного backend, нет записи в UART/GPIO/`/dev`, нет TBS Crossfire (только расширяемость), нет изменений OTA, видео, systemd-юнитов и образа. Всё, что ниже, развёртывается DEV deploy (код под `software/`, службы `sheregesh-console` на pi1 и `sheregesh-remote-net` на pi2).

## 2. Решение в целом

```text
 pi1 (пульт, PySide6)                                         pi2 (агент sheregesh_remote, asyncio)
 ┌───────────────────────────────┐   SHRG-LAB 1.3 (mTLS)   ┌───────────────────────────────────────────────┐
 │ ModulePage.qml  (C)           │                          │ Agent.ticker ─ tick() ─┐                      │
 │   ▲ Property/Slot             │   status.module  ◄────── │ ModuleService (B) ─────┤                      │
 │ ModuleView (C, module.py)     │                          │   ├ SettingsStore → ConfigStore(remote-1)     │
 │   ▲ telemetry / module_command│   module-cmd     ──────► │   ├ ModuleManager (машина состояний)          │
 │ PeerClient (B, client.py)     │                          │   │   └ ModuleProfile (radiomaster-nomad)     │
 │ ControlServer cmd "module"(C) │                          │   └ ModuleBackend                              │
 │   ▲ unix-сокет 0600           │                          │       ├ LabEmulatorProcessBackend ── pipe ──► │
 │ modulectl CLI (B) ◄── D (QA)  │                          │       │     дочерний процесс                   │
 └───────────────────────────────┘                          │       │     python3 -s -m sheregesh_emulators.jrmodule
                                                            │       └ HardwareBackendStub (не создаётся в S6) │
                                                            └───────────────────────────────────────────────┘
```

- **Module Manager** (`ModuleManager`) — машина состояний, таймауты, бюджет восстановления, последняя ошибка, история, диагностика. Не знает ни модели модуля, ни способа связи с ним.
- **Module Profile** (`ModuleProfile`) — всё, что зависит от модели: идентичность, шаги и таймауты инициализации, период опроса. Новый модуль — новый файл профиля и строка в реестре.
- **Module Backend** (`ModuleBackend`) — канал к модулю. S6: `LabEmulatorProcessBackend` (дочерний процесс) и `InlineEmulatorBackend` (тот же эмулятор в процессе, для детерминированных тестов); `HardwareBackendStub` — интерфейс-заглушка, конструктор отказывает.
- **ModuleService** — склейка для агента: настройки, менеджер, бэкенд, команды SHRG-LAB, тело телеметрии. Агент вызывает только его.
- **GUI** — `ModuleView` (логика представления, без Qt-виджетов) и `ModulePage.qml`. GUI не знает о бэкенде и эмуляторе ничего, кроме полей телеметрии.

## 3. Размещение файлов и владельцы

Правило: B и C не правят одни и те же файлы. Все новые файлы — с заголовком SPDX, как существующие.

### 3.1. Новые файлы

| Путь | Владелец | Содержание |
|---|---|---|
| `software/sheregesh/module/__init__.py` | B | Реэкспорт `ModuleManager`, `ModuleService`, `State`, `ModuleSettings`, `REGISTRY` |
| `software/sheregesh/module/states.py` | B | `State`, `ALLOWED`, коды ошибок, причины переходов, `Timings` |
| `software/sheregesh/module/wire.py` | B | Константы формата (`STATES`, `OPS`, `RESULTS`, `LAB_SCENARIOS`, `ERROR_CODES`, `STATUS_V=1`), `sanitize_module(v) -> dict \| None`, `check_command(body) -> (op, args, req)`, иначе исключение `BadCommand` (`.result` = `bad-request` \| `unknown-op`, `.text`, `.req`, `.op`). Чистый Python без зависимостей (пакет `sheregesh.module` реэкспортирует лениво, PEP 562: импорт `wire` не тянет `config`/`jsonschema`): импортируют client.py (B), ModuleView (C), s6_jr_accept (D) |
| `software/sheregesh/module/profiles/__init__.py` | B | `ModuleProfile`, `REGISTRY`, `get_profile()`, `register_profile()` |
| `software/sheregesh/module/profiles/radiomaster_nomad.py` | B | Профиль `radiomaster-nomad` |
| `software/sheregesh/module/backend.py` | B | `ModuleBackend` (Protocol), `BackendEvent`, `ModuleError`, `HardwareBackendStub`, `make_backend()` |
| `software/sheregesh/module/lab.py` | B | `LabEmulatorProcessBackend`, `InlineEmulatorBackend`, протокол канала к эмулятору (раздел 7.2) |
| `software/sheregesh/module/manager.py` | B | `ModuleManager`, `RestartBudget` |
| `software/sheregesh/module/settings.py` | B | `ModuleSettings`, `SettingsStore` |
| `software/sheregesh/module/service.py` | B | `ModuleService` |
| `software/sheregesh/config/schemas/remote-1.json` | B | Схема конфигурации удалённого блока, файл `module.yaml` (раздел 8) |
| `software/emulators/sheregesh_emulators/jrmodule.py` | B | `JrModuleEmulator` (ядро на SimClock), `IDENTITIES`, `SCRIPT_FULL`, `main()` дочернего процесса |
| `software/sheregesh/link/modulectl.py` | B | CLI для стенда и QA, по образцу `videoselect.py` (раздел 9.4) |
| `software/console/sheregesh_console/module.py` | C | `ModuleView(QObject)` и чистые функции представления |
| `software/console/sheregesh_console/qml/ModulePage.qml` | C | Страница «Модуль» |
| `tools/probe/s6_jr_accept.py` | D | Приёмка JR-01…JR-10 на стенде |
| `docs/acceptance/s6-jr/ACCEPT.md`, `REPORT.md` | D | Методика и отчёт |

### 3.2. Изменяемые существующие файлы

| Файл | Владелец | Точка подключения (точно) |
|---|---|---|
| `software/sheregesh/__init__.py` | B | `PROTOCOL_VERSION = "1.3"` |
| `software/sheregesh/link/codec.py` | B | `TYPES["1.3"] = TYPES["1.2"] \| {"module-cmd"}` + комментарий в стиле 1.1/1.2 |
| `software/sheregesh/link/client.py` | B | `PeerClient.module_command()`; `_sanitize_status`: `m = wire.sanitize_module(body.get("module"))`, при `m is not None` → `out["module"] = m`; `snapshot()["module_cmd"]`; в `finally` `_session` — незавершённый `module-cmd` из `_outbox` помечается `offline` (как `video-select`); в `_flush_outbox` — пометка `sent` |
| `software/sheregesh/config/defaults.py`, `config/__init__.py` | B | `REMOTE_DEFAULTS`, `remote_defaults()`; экспорт |
| `software/remote/sheregesh_remote/agent.py` | B | Раздел 7.4 — единственная точка подключения |
| `software/emulators/sheregesh_emulators/__init__.py` | B | Только docstring: ссылка на D6, эмулятор модуля моделирует состояние, радиосигналов не формирует |
| `tests/system/test_two_processes.py:81,123`, `tests/system/test_tls_link.py:63`, `tests/system/test_e2e_link_net.py:332`, `tests/system/test_launcher.py` (строка журнала `hello from console proto=…`) | B | `"1.2"` → `PROTOCOL_VERSION` (версия сессии меняется вместе с протоколом) |
| `software/console/sheregesh_console/backend.py` | C | Раздел 10.1 |
| `software/console/sheregesh_console/app.py` | C | `PAGE_NAMES` += `"module"` (индекс 7); в `dump_ui` ключ `"module"`; ключ `dump_on_change` дополняется `(support, state, seq)` модуля (раздел 10.4) |
| `software/console/sheregesh_console/control.py` | C | Команда `{"cmd": "module", …}` (раздел 9.3) |
| `software/console/sheregesh_console/qml/Main.qml` | C | Вкладка `tabModule` **последней** (индекс 7, Ctrl+8/F8), `Repeater { model: 8 }`, `ModulePage { objectName: "modulePage" }` в `StackLayout`. Индексы 0…6 не меняются (на них опираются `tests/gui/test_gui.py`, F7 = «О системе») |
| `software/console/sheregesh_console/i18n/sheregesh_ru.ts`, `.qm` | C | Переводы раздела 10.3; `.qm` собирается `pyside6-lrelease` и коммитится |
| `software/sheregesh/faults.py` | C | Код `MODULE-FAULT` (раздел 12.2) |
| `docs/adr/README.md` | главный агент | Строка ADR-0012 при принятии |

Пересечение, которого не избежать: `client.py` (B) и `backend.py` (C) связаны вызовом `module_command()` и форматом `telemetry["module"]`. Контракт — раздел 6 и 9.2; пока B не закончил, C тестирует с подставным клиентом (как `Backend._video_peer()` для видео), D — с подставными ответами `modulectl`.

## 4. Состояния

### 4.1. Перечень

| Код | Смысл | Текст GUI (RU) |
|---|---|---|
| `DISCONNECTED` | Канал к модулю есть или поднимается, модуль не обнаружен | Модуль отсутствует |
| `DETECTED` | Модуль обнаружен, не инициализирован | Модуль обнаружен |
| `INITIALIZING` | Идут шаги инициализации профиля | Инициализация |
| `READY` | Модуль опознан, соответствует профилю и отвечает на опрос | Готов |
| `FAULT` | Ошибка модуля или канала (эмулятора); код — в `error` | Ошибка (для `MOD-IFACE-ERROR` — «Ошибка интерфейса») |
| `RECOVERING` | Перезапуск канала и/или повторное обнаружение | Восстановление |
| `DISABLED` | Модуль выключен настройкой `enabled: false`; бэкенд остановлен, дочернего процесса нет | Отключён |

`READY` означает только «модуль опознан и отвечает». Выход управления в S6 отсутствует при любом состоянии (раздел 12.3).

### 4.2. Допустимые переходы (`states.ALLOWED`)

| Из | В | Условие (причина `reason`) | Кто вызывает |
|---|---|---|---|
| — (старт) | `DISABLED` | `enabled: false` (`start`) | `ModuleManager.start()` |
| — (старт) | `DISCONNECTED` | `enabled: true` (`start`); бэкенд запускается | `ModuleManager.start()` |
| `DISABLED` | `DISCONNECTED` | `configure enabled=true` (`settings-enabled`) | команда |
| любое, кроме `DISABLED` | `DISABLED` | `configure enabled=false` (`settings-disabled`); бэкенд останавливается | команда |
| `DISCONNECTED` | `DETECTED` | бэкенд: `presence present=true` (`present`) | событие бэкенда |
| `DETECTED` | `INITIALIZING` | `auto_init: true` — на следующем такте (`auto-init`); иначе команда `reinit` (`cmd-reinit`) | менеджер / команда |
| `DETECTED`, `INITIALIZING`, `READY` | `DISCONNECTED` | `presence present=false` (`absent`) | событие бэкенда |
| `INITIALIZING` | `READY` | `init ok` и `profile.matches(identity)` (`init-ok`) | событие бэкенда |
| `INITIALIZING` | `FAULT` | `init ok=false` (`init-error`), несоответствие профилю (`profile-mismatch`), нет ответа за `profile.init_timeout_s` (`init-timeout`) | событие / таймер |
| `READY` | `FAULT` | событие `fault` (`iface-fault`); нет `pong` дольше `profile.response_timeout_s` (`no-response`) | событие / таймер |
| `DISCONNECTED`, `DETECTED`, `INITIALIZING`, `READY`, `RECOVERING` | `FAULT` | канал потерян: процесс эмулятора завершился, не ответил на keepalive, не прислал hello (`channel-down`, `start-failed`) | событие бэкенда |
| `DISCONNECTED`, `READY` | `RECOVERING` | команда `reinit` (`cmd-reinit`) | команда |
| `FAULT` | `RECOVERING` | `auto_recover: true`, истекла задержка и бюджет не исчерпан (`recover-attempt`); или команда `reset`/`reinit` (`cmd-reset`, `cmd-reinit`) | менеджер / команда |
| `RECOVERING` | `DETECTED` / `DISCONNECTED` | канал поднят, получено `presence` (`present` / `absent`) | событие бэкенда |
| `RECOVERING` | `FAULT` | нет `presence` за `recover_timeout_s` (`recover-timeout`), канал снова потерян (`channel-down`, `start-failed`), бюджет перезапусков исчерпан (`restart-limit`) | таймер / бэкенд |

Уточнения реализации: смена профиля командой `configure` — перезапуск канала двумя переходами `→ DISABLED → DISCONNECTED` с причиной `settings-profile`; исключение внутри подсистемы — `→ FAULT` с причиной `internal-error` (раздел 7.3). `FAULT → FAULT` переходом не является: потеря канала в `FAULT` (например, авария процесса во время ошибки модуля) обновляет последнюю ошибку (`code`, `text`, `since_s`) и пишется в журнал, `seq` не меняется; восстановление тогда перезапускает процесс. `error.active` становится `false` при `READY`, `DISCONNECTED (absent)` и `DISABLED`.

Любой другой переход — ошибка программы: `ModuleManager._transition()` при `strict=True` (тесты) возбуждает `AssertionError`, в работе пишет в журнал `module: illegal transition …` и переход не выполняет.

### 4.3. Таймауты (`states.Timings`, значения по умолчанию)

| Параметр | Значение | Где |
|---|---|---|
| `hello_timeout_s` | 3 с | дочерний процесс не прислал `hello` → `EMU-START-FAILED` |
| `keepalive_interval_s` / `keepalive_timeout_s` | 1 с / 3 с | канал к эмулятору; нет ответа → процесс убивается, `EMU-NO-KEEPALIVE` |
| `profile.init_timeout_s` | 5 с (Nomad) | `INITIALIZING` → `FAULT MOD-INIT-TIMEOUT` |
| `profile.ping_interval_s` / `profile.response_timeout_s` | 1 с / 3 с (Nomad) | опрос в `READY` → `FAULT MOD-NO-RESPONSE` |
| `recover_timeout_s` | 10 с | `RECOVERING` без результата → `FAULT` |
| `recover_backoff_s` | 1, 2, 4, 8, 16, 30, 30… с | задержка `FAULT → RECOVERING`; индекс сбрасывается при `READY`, `reset`, `reinit` |
| `restart_limit` / `restart_window_s` | 3 / 60 с | перезапуски процесса эмулятора (`RestartBudget`, скользящее окно) |
| `stop_timeout_s` | 1 с | остановка процесса: закрыть stdin → SIGTERM группе → SIGKILL |

Перезапуск процесса эмулятора происходит только на переходе `FAULT → RECOVERING` при упавшем канале и расходует бюджет. Бюджет исчерпан → `FAULT` с кодом `EMU-UNAVAILABLE`, `backend.state = "unavailable"`, автоматических попыток больше нет — до команды `reset` или перезапуска службы. Повторная инициализация модуля без перезапуска процесса (ошибки `MOD-*`) бюджет не расходует, но ограничена backoff (не чаще раза в 30 с в установившемся режиме) и пишется в журнал одной строкой на попытку.

Граница наихудшего времени «эмулятор недоступен»: первый запуск (бесплатный) и 3 перезапуска по бюджету после задержек 1, 2, 4 с; следующая попытка (задержка 8 с) находит бюджет исчерпанным → `FAULT EMU-UNAVAILABLE` (переход `RECOVERING → FAULT`, `restart-limit`). От отказа: 1 + 2 + 4 + 8 = 15 с плюс 3 × время обнаружения отказа запуска (код 69 при `--refuse-start` — доли секунды; процесс без `hello` — `hello_timeout_s` 3 с), то есть 15…24 с, с момента запуска агента — не более ≈ 27 с (укладывается в `unavailable_s` = 30). Ни одно ожидание не блокирует цикл агента (раздел 7.3).

### 4.4. Коды ошибок (`error.code`)

| Код | Когда | Текст GUI (RU) |
|---|---|---|
| `MOD-IFACE-ERROR` | модуль сообщил ошибку интерфейса | Ошибка интерфейса |
| `MOD-NO-RESPONSE` | нет `pong` в `READY` | Модуль не отвечает |
| `MOD-INIT-TIMEOUT` | инициализация не завершилась вовремя | Инициализация не завершилась за %1 с |
| `MOD-INIT-FAILED` | шаг инициализации вернул ошибку | Ошибка инициализации |
| `MOD-PROFILE-MISMATCH` | идентичность модуля не соответствует профилю | Модуль не соответствует профилю |
| `MOD-RECOVER-TIMEOUT` | восстановление не дало результата за 10 с | Восстановление не завершилось |
| `EMU-START-FAILED` | процесс эмулятора не запустился или не прислал `hello` | Эмулятор не запустился |
| `EMU-EXITED` | процесс эмулятора завершился (код в `error.text`) | Эмулятор аварийно завершился |
| `EMU-NO-KEEPALIVE` | процесс жив, но не отвечает | Эмулятор не отвечает |
| `EMU-UNAVAILABLE` | бюджет перезапусков исчерпан | Эмулятор недоступен |

Последняя ошибка хранится после восстановления (`error.active = false`) до следующей ошибки — GUI показывает её как «Последняя ошибка».

## 5. API Python (B)

Модель времени: менеджер, бэкенды и эмулятор получают часы инъекцией — `clock: Callable[[], float]` (монотонные секунды) и `wall: Callable[[], float]` (только для журнала). В работе — `time.monotonic`/`time.time`; в тестах — `sheregesh_emulators.devices.SimClock` (переиспользуется, новый класс часов не вводится). Системные часы внутри `sheregesh.module` и `jrmodule.JrModuleEmulator` не читаются. Все методы ниже неблокирующие, если не сказано иное, и вызываются из одного потока (цикл агента).

```python
# sheregesh/module/states.py
class State(str, Enum): DISCONNECTED, DETECTED, INITIALIZING, READY, FAULT, RECOVERING, DISABLED
ALLOWED: dict[State, frozenset[State]]
@dataclass(frozen=True)
class Timings: hello_timeout_s=3.0; keepalive_interval_s=1.0; keepalive_timeout_s=3.0; recover_timeout_s=10.0
               recover_backoff_s=(1, 2, 4, 8, 16, 30); restart_limit=3; restart_window_s=60.0; stop_timeout_s=1.0

# sheregesh/module/profiles/__init__.py
@dataclass(frozen=True)
class ModuleProfile:
    id: str                    # "radiomaster-nomad"; ^[a-z0-9][a-z0-9-]{0,31}$
    vendor: str                # "RadioMaster"
    model: str                 # "Nomad"
    name: str                  # "RadioMaster Nomad"
    family: str                # "elrs" — справочно (D6 допускает упоминание)
    interface: str             # "uart-crsf" — справочно; в S6 не открывается
    init_steps: tuple[str, ...] = ("identify", "read-config", "check-version")
    init_timeout_s: float = 5.0
    ping_interval_s: float = 1.0
    response_timeout_s: float = 3.0
    def matches(self, identity: dict) -> bool          # vendor и model совпадают
    def describe(self) -> dict                          # {"id","name","family","interface"} для телеметрии
REGISTRY: dict[str, ModuleProfile]
def get_profile(profile_id: str) -> ModuleProfile      # KeyError — нет профиля
def register_profile(p: ModuleProfile) -> None          # для будущих профилей и тестов расширяемости

# sheregesh/module/backend.py
@dataclass(frozen=True)
class BackendEvent:
    kind: str     # channel-up | channel-down | presence | init | pong | fault | lab
    data: dict    # channel-down: {"reason": "exited|no-keepalive|start-failed|protocol", "code": int|None}
                  # presence: {"present": bool}; init: {"ok": bool, "identity": dict, "code": str, "text": str}
                  # fault: {"code": str, "text": str}; lab: {"scenario": str, "ok": bool, "error": str}
class ModuleError(Exception): ...
class ModuleBackend(Protocol):
    kind: str                  # "lab-emulator" | "hardware"
    supports_lab: bool         # True только у лабораторных бэкендов
    def start(self) -> None                  # запуск канала; результат — событиями channel-up / channel-down
    def stop(self) -> None                   # неблокирующая остановка (досылается в poll)
    def poll(self) -> list[BackendEvent]     # забрать накопленные события; keepalive и таймауты канала — здесь
    def send(self, op: str, **args) -> bool  # probe | init | ping | lab; False — канала нет (ничего не отправлено)
    def lab_local(self, scenario: str) -> bool   # emu-unavailable / emu-available — на стороне родителя
    def info(self) -> dict                   # {"kind","lab","state","pid","restarts","restart_limit","last_exit"}
    def close(self) -> None                  # блокирующая, не дольше stop_timeout_s; только при выходе агента
class HardwareBackendStub:                   # kind="hardware", supports_lab=False
    def __init__(self, *a, **kw): raise ModuleError("hardware backend is not available in S6")
def make_backend(settings, profile, *, clock, log, timings, emulator_command=None) -> ModuleBackend
    # "lab-emulator" → LabEmulatorProcessBackend; всё остальное → ModuleError

# sheregesh/module/lab.py
class LabEmulatorProcessBackend:             # kind="lab-emulator", supports_lab=True
    def __init__(self, profile_id, *, clock, log, timings, command=None): ...
    # command=None → [sys.executable, "-s", "-m", "sheregesh_emulators.jrmodule", "--profile", profile_id]
class InlineEmulatorBackend:                 # тот же контракт поверх JrModuleEmulator в процессе (тесты)
    def __init__(self, emulator: "JrModuleEmulator", *, clock, timings, start_ok=True): ...
    # crash → channel-down {"reason":"exited","code":70}; emu-unavailable → последующие start() дают start-failed
    # freeze() — процесс «завис»: ответов нет, через keepalive_timeout_s — channel-down no-keepalive (тесты)

# sheregesh/module/manager.py
class RestartBudget:
    def __init__(self, limit: int, window_s: float, clock): ...
    def try_spend(self) -> bool; def reset(self) -> None; def used(self) -> int
class ModuleManager:
    def __init__(self, settings: ModuleSettings, backend_factory: Callable[[ModuleSettings], ModuleBackend], *,
                 profiles=REGISTRY, clock=time.monotonic, log=lambda s: None, timings=Timings(), strict=False): ...
    state: State                                   # только чтение
    seq: int                                       # число переходов с запуска
    def start(self) -> None
    def tick(self) -> None                         # единственная точка переходов: poll() бэкенда + таймеры
    def command(self, op: str, args: dict) -> str  # reinit | reset | lab → код результата (раздел 6.3)
    def apply_settings(self, s: ModuleSettings) -> None   # после успешного сохранения; enabled/profile меняют состояние
    def internal_error(self, text: str) -> None    # исключение подсистемы: бэкенд остановлен, FAULT (internal-error)
    # command("configure", …) → "unknown-op": configure исполняет ModuleService (сохранение, затем apply_settings)
    def snapshot(self) -> dict                     # тело status.module без "cmd" и "settings" (раздел 6.2)
    def close(self) -> None

# sheregesh/module/settings.py
@dataclass(frozen=True)
class ModuleSettings:
    enabled: bool = True; profile: str = "radiomaster-nomad"; backend: str = "lab-emulator"
    auto_init: bool = True; auto_recover: bool = True
    @classmethod
    def from_data(cls, d: dict) -> "ModuleSettings"; def to_yaml(self) -> str; def replace(self, **kw)
class SettingsStore:
    def __init__(self, root: Path | None, *, log, clock=time.time): ...
    def load(self) -> tuple[ModuleSettings, dict]  # блокирующая, при старте; info: {"source","revision","notice"}
    def save(self, s: ModuleSettings, validate: Callable[[ModuleSettings], str | None]) -> tuple[str, dict]
        # блокирующая (fsync) — только в исполнителе; → ("ok"|"invalid"|"not-saved", info)

# sheregesh/module/service.py
class ModuleService:
    def __init__(self, config_dir: Path | None, *, clock=time.monotonic, log, timings=Timings(),
                 emulator_command=None, backend_factory=None, wall=time.time): ...   # wall — имя *.corrupt-<UTC>
    def start(self) -> None          # load() настроек, менеджер, бэкенд; исключения не выпускает наружу
    def tick(self) -> None           # manager.tick() + завершение отложенного configure
    def command(self, body: dict) -> None    # тело module-cmd; итог — в status()["cmd"]
    def new_session(self) -> None    # новый сеанс управления: прежний "cmd" сбрасывается (как video_select)
    def status(self) -> dict         # полное status.module (раздел 6.2)
    def health(self) -> dict         # сжато для link-status.json (раздел 12.1)
    seq: int                         # = manager.seq (для внеочередной отправки status)
    def close(self) -> None

# sheregesh_emulators/jrmodule.py
IDENTITIES: dict[str, dict]   # "radiomaster-nomad": {"vendor":"RadioMaster","model":"Nomad","firmware":"emu-1.0","serial":"EMU-NOMAD-0001"}
LAB_SCENARIOS = wire.LAB_SCENARIOS
class JrModuleEmulator:
    def __init__(self, clock, profile_id="radiomaster-nomad", identity=None, init_s=1.0, present=True): ...
    def step(self, dt: float) -> None
    def handle(self, msg: dict) -> list[dict]     # сообщение канала → ответы (раздел 7.2)
    def drain(self) -> list[dict]                 # самопроизвольные сообщения (presence при attach/detach, fault)
    def lab(self, scenario: str) -> bool
    def restart(self) -> None                     # «новый процесс»: лабораторные флаги сброшены (InlineEmulatorBackend)
SCRIPT_FULL: list[tuple[float, str, State]]     # (t, сценарий, ожидаемое состояние) — общий сценарий unit и L3:
    # в момент t (модельные секунды от start()) подаётся сценарий ("" — ничего); состояние должно наблюдаться
    # (текущее или переход в него) в промежутке [t, t следующей строки), для последней строки — [t, t + 10)
def main(argv=None) -> int                       # дочерний процесс: --profile ID --speed 1 [--refuse-start]
```

## 6. Формат данных SHRG-LAB

### 6.1. Версия протокола: 1.3

- Новый тип **одного** сообщения пульт → блок: `module-cmd`; `TYPES["1.3"] = TYPES["1.2"] | {"module-cmd"}`; `PROTOCOL_VERSION = "1.3"`; `compat-matrix.yaml` не меняется (`min_peer_minor: 0`).
- Блок кладёт `status.module` только при сессии ≥ 1.3 (`"module-cmd" in TYPES[session]`). Сессия 1.2 (старый пульт) не видит модуль и не может им управлять; блок при этом работает как раньше, подсистема модуля живёт независимо.
- Новый пульт со старым блоком (сессия 1.2): `PeerClient.module_command()` возвращает `unsupported`, `status.module` нет → GUI «Удалённый блок не поддерживает внешний модуль (протокол 1.2)».
- `module-cmd` в сессии < 1.3 агент не исполняет, а учитывает (`ignored`), как любые сообщения новее сессии (CHG-012). Неизвестные поля тела и `args` → `bad-request`, состояние не меняется.
- Подтверждения — тем же механизмом, что `video-select`: `req` (int, 0…2³¹−1) возвращается в телеметрии `status.module.cmd`; отдельного ответного сообщения нет.

### 6.2. `status.module` (блок → пульт)

```json
{
  "v": 1,
  "state": "READY",
  "reason": "init-ok",
  "since_s": 12.4,
  "seq": 17,
  "profile":  {"id": "radiomaster-nomad", "name": "RadioMaster Nomad", "family": "elrs", "interface": "uart-crsf"},
  "profiles": [{"id": "radiomaster-nomad", "name": "RadioMaster Nomad"}],
  "backend":  {"kind": "lab-emulator", "lab": true, "state": "running", "pid": 1234, "restarts": 0,
               "restart_limit": 3, "last_exit": null},
  "identity": {"vendor": "RadioMaster", "model": "Nomad", "firmware": "emu-1.0", "serial": "EMU-NOMAD-0001"},
  "error":    {"code": "MOD-IFACE-ERROR", "text": "interface error reported by module", "since_s": 40.2,
               "state": "READY", "active": false},
  "recovery": {"auto": true, "attempt": 0, "next_s": null, "exhausted": false},
  "settings": {"enabled": true, "profile": "radiomaster-nomad", "backend": "lab-emulator", "auto_init": true,
               "auto_recover": true, "revision": "r000002", "source": "store", "notice": ""},
  "diag":     {"init_ms": 1003, "pong_age_s": 0.4, "faults": 2, "recoveries": 2, "transitions": 17},
  "output": "none",
  "history":  [{"seq": 17, "from": "INITIALIZING", "state": "READY", "reason": "init-ok", "code": "", "ago_s": 12.4}],
  "cmd":      {"req": 5, "op": "lab", "result": "ok", "error": ""}
}
```

Правила (проверяет `wire.sanitize_module`, данные второй стороны недоверенные):

| Поле | Тип и ограничения | Отсутствие / ошибка |
|---|---|---|
| `v` | только `1` | иначе весь объект отбрасывается (`None`) |
| `state` | из `STATES` | иначе `None` |
| `reason` | строка ≤ 32 | `""` |
| `since_s`, `*.since_s`, `ago_s`, `pong_age_s`, `next_s` | число 0…1e9, секунды по часам **менеджера блока** | `0` / `null` |
| `seq`, `restarts`, `restart_limit`, `attempt`, `faults`, `recoveries`, `transitions`, `init_ms`, `pid` | целое 0…1e9. `restarts` — перезапуски процесса эмулятора в текущем окне бюджета (`RestartBudget.used()`, 60 с; `reset` обнуляет), `pid` — PID процесса эмулятора из его `hello` (0 — процесса нет), `attempt` — попыток восстановления с последнего `READY`/`reset`/`reinit` | `0` |
| `profile.*`, `profiles[].*` | строки: `id` ≤ 32 по шаблону, `name` ≤ 64, прочие ≤ 16; `profiles` ≤ 8 | профиль `{"id":"","name":""}` |
| `backend.kind` | `lab-emulator` \| `hardware` | `""` |
| `backend.state` | `starting` \| `running` \| `restarting` \| `stopped` \| `unavailable` | `""` |
| `backend.last_exit` | целое или `null` (отрицательное — номер сигнала) | `null` |
| `identity` | объект из 4 строк ≤ 32 или `null` | `null` |
| `error` | `null` или `{code ∈ ERROR_CODES, text ≤ 200, since_s, state ∈ STATES, active: bool}` | `null` |
| `settings.source` | `store` \| `factory` \| `recovered` \| `reset` \| `memory` | `""` |
| `output` | только `"none"`; любое другое значение заменяется на `"unknown"` | `"unknown"` |
| `history` | ≤ 8 последних переходов, новые первыми; `code` — код ошибки для переходов в `FAULT`, иначе `""` | `[]` |
| `cmd` | `{req, op ∈ OPS ∪ {""}, result ∈ RESULTS, error ≤ 200}` (`op = ""` — тело с неизвестным/неверным `op`, итог `unknown-op`/`bad-request`); есть только после команды в этом сеансе | нет |

Время передаётся только относительными секундами (`since_s`, `ago_s`): часы плат не синхронизированы (изолированный стенд). Абсолютное время для экрана считает пульт по своим часам в момент приёма (раздел 10.2).

Размер — не более 4 КиБ (лимит строки 64 КиБ). Отправка: `status` с модулем — в обычном такте heartbeat (1 с) и **дополнительно** при смене `ModuleService.seq`, не чаще раза в 0,5 с. Кроме того, сразу после исполнения `module-cmd` (итог или `pending`), чтобы подтверждение не ждало такта heartbeat.

### 6.3. `module-cmd` (пульт → блок)

```json
{"proto": "1.3", "type": "module-cmd", "seq": 41, "ts": 1793000000.1,
 "body": {"req": 5, "op": "lab", "args": {"scenario": "detach"}}}
```

| `op` | `args` | Меняет состояние | Подтверждение на пульте | Итог |
|---|---|---|---|---|
| `status` | `{}` | нет | — | `ok` (проверка канала команд) |
| `reinit` | `{}` | да: `READY`/`DISCONNECTED` → `RECOVERING`, `DETECTED` → `INITIALIZING`, `FAULT` → `RECOVERING`; в `INITIALIZING` — инициализация заново (новый `id`, новый срок), в `RECOVERING` — новый срок и `probe`, без перехода; сброс backoff | да | `ok`, `disabled` |
| `reset` | `{}` | да: сброс бюджета перезапусков и backoff; из `FAULT` (в т. ч. `EMU-UNAVAILABLE`) → `RECOVERING`; в прочих — только сброс счётчиков | да | `ok`, `disabled` |
| `configure` | подмножество `{"enabled": bool, "auto_init": bool, "auto_recover": bool, "profile": str}` | да, сохраняется (раздел 8) | да | `pending` → `ok` \| `invalid` \| `not-saved`; сразу, без `pending`: `busy`, `invalid` (профиль не из реестра), `not-saved` (хранилище только в памяти), `ok` (значения не меняются — новой ревизии нет) |
| `lab` | `{"scenario": <LAB_SCENARIOS>}` | да (эмулятор) | да | `ok`, `forbidden`, `not-running`, `disabled` (модуль выключен); `ok` — сценарий передан эмулятору (родительские `emu-*` — исполнены) |

Коды `result` (`wire.RESULTS`): `ok`, `pending`, `bad-request` (тело/аргументы), `unknown-op`, `forbidden` (лабораторная команда при нелабораторном бэкенде), `disabled`, `busy` (уже идёт `configure`), `invalid` (значение не прошло схему или реестр профилей), `not-saved` (хранилище недоступно; значение не применено), `not-running` (эмулятор не запущен — для `lab`, кроме `emu-*`), `error`.

`configure`: агент сразу ставит `cmd.result = "pending"`, сохраняет в исполнителе (раздел 8), применяет к менеджеру и ставит итог. Клиент ждёт окончательный итог.

### 6.4. Лабораторные сценарии (`wire.LAB_SCENARIOS`)

| Сценарий | Действие эмулятора | Ожидаемая реакция менеджера |
|---|---|---|
| `attach` | модуль «вставлен»: `presence true` | `DISCONNECTED → DETECTED → INITIALIZING → READY` |
| `detach` | модуль «вынут»: `presence false`, на `ping`/`init` не отвечает | → `DISCONNECTED` |
| `iface-error` | на `ping` и `init` отвечает `fault MOD-IFACE-ERROR` | → `FAULT MOD-IFACE-ERROR`; автовосстановление по backoff, пока ошибка есть — снова `FAULT` |
| `hang` | присутствует (`probe` → `present: true`), но на `init` и `ping` не отвечает | первый переход `READY → FAULT MOD-NO-RESPONSE` (≤ 3 с); повторы восстановления → `FAULT MOD-INIT-TIMEOUT` |
| `heal` | снять `iface-error`, `hang`, `slow-init` | ближайшая попытка восстановления → `READY` |
| `slow-init` | следующая инициализация 8 с (> 5 с) | `FAULT MOD-INIT-TIMEOUT`, затем восстановление |
| `crash` | процесс эмулятора `os._exit(70)` | `FAULT EMU-EXITED` → `RECOVERING` (перезапуск, бюджет 1/3) → `READY` |
| `emu-unavailable` | **родитель**: текущий процесс SIGKILL, новые запуски отклоняются (`--refuse-start`, код 69) | `FAULT EMU-*` → попытки → `FAULT EMU-UNAVAILABLE`, процессов нет, попыток нет |
| `emu-available` | **родитель**: запуски снова разрешены | состояние не меняется; дальше — команда `reset` |

«Перезапуск службы эмулятора» и «перезапуск удалённого блока» — не лабораторные команды: на стенде это `systemctl restart sheregesh-remote-net` и перезагрузка pi2 (D), в L3 — завершение и повторный запуск процесса агента. Состояние лабораторных сценариев не сохраняется: после любого перезапуска эмулятор стартует с исправным вставленным модулем, поэтому состояние после перезагрузки предсказуемо (раздел 12.4).

**Запрет вне лабораторного режима.** «Лабораторный режим» = активный бэкенд с `supports_lab = True`. В S6 это единственный возможный бэкенд (схема допускает только `backend: lab-emulator`), но запрет реализован структурно, а не настройкой: `ModuleManager.command("lab", …)` при `not backend.supports_lab` возвращает `forbidden` и ничего не передаёт; у `HardwareBackendStub` `supports_lab = False`, и будущий аппаратный бэкенд обязан сохранить это (тест `test_lab_forbidden_without_lab_backend`). Дополнительно: команда приходит только из аутентифицированного сеанса (mTLS, URI пульта) или с loopback в тестах; GUI показывает панель сценариев только при `backend.lab = true` и требует подтверждения; локальный канал пульта — unix-сокет 0600 с проверкой uid.

## 7. Процессы эмулятора и агент

### 7.1. Выбор: дочерний процесс агента

Эмулятор работает **дочерним процессом** агента `sheregesh-remote-net`: `python3 -s -m sheregesh_emulators.jrmodule --profile radiomaster-nomad`, канал — stdin/stdout (JSON-строки), stderr наследуется (journald службы, префикс `[jrmodule]`).

Почему так:
- **проверяемые отказы.** «Служба эмулятора аварийно завершается» — настоящий выход процесса (`crash`, внешний `kill -KILL`), а не флаг в памяти; «эмулятор недоступен» — настоящий отказ запуска. Эмулятор внутри агента в потоке так не проверить: его авария — авария агента;
- **без нового образа.** Новый systemd-юнит или файл в `/usr` требует образа (ADR-0010), а дочерний процесс запускается из того же выпуска DEV deploy: `PYTHONPATH` дочернего процесса собирается из каталогов уже загруженных пакетов (`dirname(dirname(sheregesh.__file__))`, `dirname(dirname(sheregesh_emulators.__file__))`), то есть из выпуска, проверенного launcher, или из заводского `/usr/lib/sheregesh`. Флаг `-s`, как у launcher; `-I` нельзя — он отключает `PYTHONPATH`;
- **ограничения службы соблюдены**: тот же пользователь, `NoNewPrivileges`, без сети (pipe), без записи на диск, сокетов не открывает.

Запуск — через существующий `sheregesh.video.spawn(args, stdin=PIPE, stdout=PIPE)`: `setpriv --pdeathsig KILL`, своя группа процессов, `trap "" HUP`; дочерний процесс умирает вместе с агентом при любом его завершении, в том числе SIGKILL. При остановке и перезапуске службы systemd убивает всю cgroup. Сирот не остаётся (проверка — раздел 11, JR-08).

### 7.2. Канал агент ↔ эмулятор

JSON-строка ≤ 4096 байт на сообщение. Родитель → эмулятор: `{"op": …, "id": n, …}`; эмулятор → родитель: `{"ev": …, "id": n?, …}`.

| Родитель → | Ответ эмулятора |
|---|---|
| (запуск) | первой строкой `{"ev":"hello","emulator":"jrmodule-lab","version":1,"profile":ID,"pid":N}` не позже `hello_timeout_s` |
| `{"op":"keepalive"}` | `{"ev":"alive"}` всегда, пока процесс работает (уровень канала) |
| `{"op":"probe"}` | `{"ev":"presence","present":bool}` — отвечает при любом сценарии, включая `hang` и `iface-error` (обнаружение — физический признак, а не ответ модуля); также без запроса при `attach`/`detach` |
| `{"op":"init","id":n,"profile":ID}` | через `init_s` модельного времени: `{"ev":"init","id":n,"ok":true,"identity":{…}}` или `{"ok":false,"code":"MOD-INIT-FAILED"\|"MOD-IFACE-ERROR","text":…}`; при `hang`/`detach` — нет ответа |
| `{"op":"ping","id":n}` | `{"ev":"pong","id":n}`; при `iface-error` — `{"ev":"fault","code":"MOD-IFACE-ERROR","text":…}`; при `hang`/`detach` — нет ответа |
| `{"op":"lab","scenario":S}` | `{"ev":"lab","scenario":S,"ok":bool,"error":…}`, затем действие |

Эмулятор завершается с кодом 0 по EOF stdin, 70 по `crash`, 69 при `--refuse-start` (до `hello`). Строка длиннее 4096 байт или не JSON — нарушение протокола: процесс убивается, `channel-down {"reason":"protocol"}`.

Неблокируемость: чтение stdout — поток-демон `jrmodule-reader` в очередь `queue.SimpleQueue`; `poll()` только забирает очередь и проверяет `proc.poll()`. Запись — в неблокирующий stdin (`os.set_blocking(fd, False)`); `BlockingIOError` → процесс убивается, `channel-down {"reason":"no-keepalive"}`. `BrokenPipeError` означает, что процесс уже завершается: неотправленное отбрасывается, итог — по коду выхода в `poll()` (`exited`, например 70 после `crash`), иначе гонка «`ping` сразу после `crash`» давала бы `EMU-NO-KEEPALIVE` вместо `EMU-EXITED`. После выхода процесса `poll()` дочитывает очередь до EOF (не дольше 1 с), поэтому ответ на `lab crash` приходит раньше `channel-down`. Код выхода: отрицательный — номер сигнала (сторож `pdeath.py` вне Linux сообщает сигнал как 128 + N — приводится к −N). `pid` в телеметрии — PID процесса эмулятора из `hello` (вне Linux `Popen.pid` — сторож). Ожидания завершения процесса в такте нет: после SIGTERM состояние `stopping`, SIGKILL через `stop_timeout_s` на следующих тактах. Единственная блокирующая операция — `close()` при выходе агента, не дольше `stop_timeout_s`.

Детерминизм: ядро `JrModuleEmulator` — чистая модель на инъецированных часах; дочерний процесс лишь продвигает свой `SimClock` реальным прошедшим временем (как `Agent.ticker` продвигает `self.clock`). Unit- и сценарные тесты используют `InlineEmulatorBackend` + `SimClock` и не зависят от планировщика ОС.

### 7.3. Ограничение перезапусков

- Процесс эмулятора перезапускает только менеджер, только на `FAULT → RECOVERING`, только если `RestartBudget.try_spend()` (≤ 3 за 60 с) и после задержки backoff.
- Бюджет исчерпан → `EMU-UNAVAILABLE`, `backend.state = "unavailable"`, журнал `module: emulator restart limit reached: 3 in 60 s — automatic restarts stopped`. Дальше — только команда `reset` (или перезапуск службы).
- Агент из-за модуля не завершается: исключения в `ModuleService.tick()/command()` перехватываются, пишутся в журнал (`module: internal error …`), состояние → `FAULT` с кодом `EMU-EXITED`/`MOD-INIT-FAILED` по месту. Сторож systemd (`WatchdogSec=15`) не затрагивается: такт модуля — миллисекунды.
- Перезапуски службы целиком ограничены уже существующими `StartLimitIntervalSec=300`/`StartLimitBurst=5` юнита.

### 7.4. Подключение к агенту (`agent.py`, B — единственный владелец)

| Место | Изменение |
|---|---|
| `main()` аргументы | `--module` (включить в loopback), `--no-module` (выключить), `--remote-config-dir DIR` (по умолчанию `dirname(--link-config)/remote-config`, без `--link-config` — нет хранилища, настройки в памяти), `--module-emulator-command` (скрыт, `argparse.SUPPRESS`: подмена эмулятора в тестах). Подсистема включена по умолчанию **только в сетевом профиле** (`--link-config`, т. е. pi2); в loopback — по `--module` (существующие системные тесты не получают лишний дочерний процесс) |
| `Agent.__init__` | `self.module = ModuleService(cfg_dir, log=lambda s: log(f"module: {s}"), emulator_command=…) if enabled else None` |
| `Agent.run()` | `self.module.start()` до `sdnotify.ready(...)`; `start()` не ждёт эмулятора |
| `Agent.ticker()` | `self.module.tick()` каждый такт; внеочередной `status`, если `module.seq` изменился и с прошлого `status` прошло ≥ 0,5 с |
| `Agent.status_body()` | `if self.module and "module-cmd" in TYPES.get(self.session or "", ()): body["module"] = self.module.status()` |
| `Agent.on_message()` | ветка `elif mtype == "module-cmd" and mtype in TYPES.get(self.session or "", ()): self.module.command(body)`, затем `status` сразу (итог или `pending` не ждёт heartbeat). Если `self.module is None` (`--no-module`), `status.module` не передаётся и итог показать негде: команда учитывается как `ignored` с записью в журнал `module-cmd ignored: module subsystem disabled`; клиент пульта распознаёт это сам (сессия 1.3 без `status.module` → `unsupported`, раздел 9.2) |
| `Agent.handle()` | при новом сеансе — `self.module.new_session()` (рядом с `self.video_select = None`) |
| `Agent.write_status()` | ключ `"module": self.module.health()`; запись также при смене `module.seq` (не только раз в 2 с) |
| `main().stop` и `finally` | `agent.module.close()` |

## 8. Настройки модуля

Хранятся существующим механизмом ADR-0004 — `sheregesh.config.ConfigStore` с новой схемой `remote-1` (`Schema.load("remote-1")`, устройство `remote`). Отдельный JSON по образцу `video-input.json` отклонён: нужны атомарные ревизии, подтверждение и восстановление при повреждении, которые уже есть в `ConfigStore`.

- **Путь:** `/home/sheregesh-ui/.config/sheregesh/remote-config/` (`revisions/`, `confirmed/`, `quarantine/`, `state.json`, `state.prev.json`, `.lock`). Каталог `~/.config/sheregesh` уже на запись у `sheregesh-remote-net` (`ReadWritePaths`), юнит менять не нужно. Ключи и `link.json` не затрагиваются.
- **Схема `remote-1.json`:** `{"schema_version": 1, "device": "remote", "files": {"module.yaml": {"x-services": ["module"], "schema": {…}}}}`; `module.yaml`: `additionalProperties: false`, обязательные `enabled` (bool), `profile` (string, `^[a-z0-9][a-z0-9-]{0,31}$`), `backend` (`enum: ["lab-emulator"]`), `auto_init` (bool), `auto_recover` (bool). Новые ключи в будущих версиях — необязательные.
- **Заводские значения** (`remote_defaults()`): `enabled: true`, `profile: radiomaster-nomad`, `backend: lab-emulator`, `auto_init: true`, `auto_recover: true`.
- **Сохранение (`configure`):** `SettingsStore.save()` → `ConfigStore.apply(files, health_check, reason="console module-cmd")`; `health_check` проверяет профиль по `REGISTRY` и `make_backend` по виду бэкенда (без запуска процесса); успех — ревизия подтверждена, затем `manager.apply_settings()`. Вызов идёт в `ThreadPoolExecutor(max_workers=1)` сервиса (fsync на microSD не блокирует цикл агента); `tick()` забирает готовый `Future`. Одновременно — один `configure`, второй получает `busy`.
- **Поведение при старте** (`SettingsStore.load()`, ошибки наружу не выходят):

| Ситуация | Действие | `settings.source` / журнал |
|---|---|---|
| Каталога нет | `ConfigStore.initialize(remote_defaults())` | `factory` |
| `recover()` вернул действия (повреждён `state.json`, взят `state.prev.json`, восстановлен по маркерам, отброшен pending) | работа с восстановленной ревизией | `recovered`, `notice` = коды `CFG-*` через запятую |
| `NoValidConfiguration` (нет ни одной целостной ревизии) | каталог переименовывается в `remote-config.corrupt-<UTC YYYYmmddTHHMMSS>`, создаётся заново с заводскими значениями | `reset`, `notice = "CFG-RESET"` |
| `OSError`/`PermissionError`/иное (нет `--remote-config-dir`, ФС только для чтения) | заводские значения в памяти | `memory`, `notice = "CFG-MEMORY"`; `configure` → `not-saved` |
| Сохранённый профиль отсутствует в реестре (откат ПО) | заводские профиль и бэкенд в памяти, хранилище не трогается | `memory`, к `notice` добавляется `CFG-MEMORY` |

Ни один из случаев не мешает запуску модуля и не включает ничего сверх заводских значений. Лабораторные сценарии в настройки не входят.

## 9. Пульт: клиент, локальный канал, CLI

### 9.1. Формат в клиенте (B)

`PeerClient.snapshot()["telemetry"]["module"]` — результат `wire.sanitize_module()` или ключ отсутствует. `PeerClient.snapshot()["session"]` — версия сессии.

### 9.2. `PeerClient.module_command` (B)

```python
def module_command(self, op: str, args: dict | None = None, wait_s: float = 5.0) -> dict:
    """→ {"ok": bool, "result": str, "error": str, "req": int}. Вызывать не из GUI-потока.
    result: код блока (6.3) | not-connected (нет соединения; или потеряно до отправки или до итога — итог неизвестен)
            | unsupported (сессия < 1.3; или сессия 1.3, но блок не сообщает status.module — подсистема выключена)
            | timeout (нет итога с этим req за wait_s; для configure — итог ещё pending)
            | bad-request (op не из OPS или args не проходят wire.check_command — проверяется до отправки)."""
```

Механика — как `select_video_source`: `req` из счётчика, сообщение в `_outbox`, ожидание `telemetry.module.cmd.req == req` с итогом не `pending`; `snapshot()["module_cmd"] = {"req","op","state": queued|sent|offline|unsupported}`.

### 9.3. Локальный канал пульта (`control.py`, C)

Добавляется команда (остальные не меняются; `self._busy` общий):

```text
{"cmd": "module", "op": "status"}                                   → {"result": "ok", "detail": "", "module": M}
{"cmd": "module", "op": "reinit"|"reset", "wait": 15}               → {"result": код, "detail": текст, "module": M}
{"cmd": "module", "op": "lab", "args": {"scenario": "detach"}, "wait": 15}
{"cmd": "module", "op": "configure", "args": {"enabled": false}, "wait": 30}
```

Выполняется через слоты `ModuleView` с `confirmed=True` (как `Backend.selectVideoSource(id, True)`): `reinit` → `requestReinit`, `reset` → `requestReset`, `lab` → `runLabScenario`, `configure` → `configure(args)`; ответ — после выхода команды (`command.id` этого запроса) из состояния `sending` или по `wait` (по умолчанию 15 с, `configure` — 30 с, не более 60 с). `status` — только снимок пульта, блоку ничего не отправляется. Проверка запроса на пульте (до вызова слота, `bad-request`, состояние не меняется): `op` из `status|reinit|reset|lab|configure`; `args` — объект; `reinit`/`reset` — без `args`; `lab` — ровно `{"scenario": str}` (имя вне `labScenarios` → `bad-request`, нелабораторный бэкенд → `forbidden`); `configure` — непустое подмножество `enabled`/`auto_init`/`auto_recover` (bool), `profile` (str); `wait` — число. Коды `result`: из раздела 6.3 плюс `no-link`, `unsupported`, `timeout`, `busy` (идёт другая команда пульта или другой запрос канала), `bad-request`, `failed`. `M = ModuleView.snapshot()`:

```json
{"support": "ok", "stale": false, "session": "1.3", "telemetry": { …status.module после sanitize… },
 "display": {"stateText": "Готов", "errorText": "Ошибка интерфейса", "changedAt": "14:02:31"},
 "command": {"state": "done", "op": "lab", "result": "ok", "detail": "", "id": 4}}
```

`display.stateText` — текст плашки `moduleStateText` (для `FAULT` — «Ошибка»), `display.errorText` — описание кода последней ошибки («Ошибка интерфейса», `""` — ошибок не было); тексты — те же исходные строки, что `qsTr` в `ModulePage.qml`, через переводчик приложения (контекст `ModulePage`), поэтому совпадают с экраном. `telemetry` — `null`, если данных нет.

### 9.4. `python3 -m sheregesh.link.modulectl` (B)

```text
modulectl status | reinit | reset | lab SCENARIO | configure KEY=VALUE…
          [--via auto|console|direct] [--wait S] [--expect STATE [--expect-timeout S]] [--link-config FILE]
```

- `--via console` — через сокет пульта (`videoselect.socket_path()`, `ask_console()` переиспользуются; `--socket PATH` — явный путь); `direct` — собственный `PeerClient` (вытесняет сеанс пульта на 1–3 с, как `videoselect`); `auto` — пульт, если запущен. `--port N` — прямой сеанс без TLS к `127.0.0.1:N` (испытания на loopback, подразумевает `direct`). `--wait` по умолчанию 15 с, для `configure` — 30 с.
- Вывод — JSON `{"result", "detail", "via", "module": M}`; в режиме `direct` `M` строится тем же способом (`support`, `stale`, `session`, `telemetry`), без `display`.
- `--expect READY` — после команды опрашивать `status` раз в 0,5 с до `telemetry.state == STATE` или таймаута (итог `timeout`).
- Код выхода: 0 — `ok` (и состояние достигнуто при `--expect`), 1 — отказ или таймаут, 2 — нет связи/пульта (`not-connected`, `no-link`), `bad-request` или неверные аргументы (ошибка argparse, без JSON).

## 10. GUI пульта (C)

### 10.1. `backend.py`

- `Backend.__init__`: `self._module = ModuleView(self, command=lambda: getattr(self._module_peer(), "module_command", None), log=lambda level, text: self.log(level, "module", text))`; `_module_peer()` возвращает `self.client` (в тестах подменяется — как `_video_peer()`). Вызов клиента: `module_command(op, args, wait_s)` позиционно.
- `Backend._refresh()` (после `_link_status`) → `Backend._update_module(s)`: `self._module.update(telemetry=(s.peer.telemetry or {}).get("module"), session=s.peer.session, link=self._status["peerLink"], mode=self._status["peerMode"], received=bool(s.peer.telemetry))`; затем MODULE-FAULT (раздел 12.2) и журналы. `received=False` (сеанс есть, первого `status` ещё не было) — `no-link`, а не `absent`. MODULE-FAULT поднимается через `self.bench.monitor.log.raise_(clock, "MODULE-FAULT", code=…)`: у события свой параметр `code`, а `EventGenerator.inject(code, **params)` его не принимает; при смене кода событие снимается и поднимается заново (новый текст в журнале событий). Журналы: смена `(support, seq)`; `support = local` и первое состояние после запуска до первой телеметрии не пишутся (не переход).
- Свойство `@Property(QObject, constant=True) def module(self)` → `self._module`. QML обращается `backend.module.<поле>`.

### 10.2. `ModuleView(QObject)` — свойства и слоты

Один сигнал `changed`; все свойства `notify=changed`.

| Property | Тип | Значение |
|---|---|---|
| `support` | str | `ok` — сессия ≥ 1.3 и есть `status.module` с состоянием из `STATES`; `no-link` — канала нет (в том числе `AUTH_FAILED`/`INCOMPATIBLE`, сеанс без версии и сеанс до первого `status`); `old-remote` — сессия < 1.3; `absent` — сессия ≥ 1.3, но блок модуль не сообщает (подсистема выключена); `local` — пульт без удалённого блока (`peerMode = emulated`) |
| `stale` | bool | показаны данные до потери связи (только при `no-link` после `ok`; при `old-remote`/`absent`/`local` прежние данные сбрасываются) |
| `staleAt` | str | время приёма последних данных по часам пульта `HH:MM:SS` (для «Данные на %1») |
| `state` | str | код из `STATES`; `""` при `support != ok` |
| `lastState` | str | последнее известное состояние (при `stale` — для «последнее известное состояние: …») |
| `seq` | int | `seq` блока, `-1` — данных нет |
| `reason` | str | причина последнего перехода |
| `profileId`, `profileName`, `profileFamily`, `profileInterface` | str | из `profile` |
| `profiles` | list | `profiles` |
| `backendKind`, `isLab` | str, bool | `backend.kind`, `backend.lab` |
| `emulatorState` | str | `backend.state` |
| `emulatorPid`, `emulatorRestarts`, `emulatorRestartLimit` | int | из `backend` |
| `emulatorLastExit` | int \| null | `backend.last_exit` |
| `lastErrorCode`, `lastErrorText`, `lastErrorAt`, `lastErrorState` | str | `error.code`, текст блока (без управляющих символов, ≤ 200), время по часам пульта `HH:MM:SS` (`now − error.since_s`; пересчитывается только для новой ошибки — другой `(code, state)` или расхождение > 2 с, чтобы задержка доставки не «сдвигала» время), `error.state` |
| `errorActive` | bool | `error.active` |
| `changedAt` | str | время последнего перехода по часам пульта: при новом `seq` — `now − since_s`, `HH:MM:SS` |
| `changedAgoS` | int | секунд с последнего перехода по часам пульта (для «(%2 с назад)») |
| `sinceS` | float | `since_s` |
| `identity`, `settings`, `diagnostics`, `recovery` | dict | соответствующие объекты |
| `history` | list | `history`; у каждого перехода добавлено `at` — время по часам пульта `HH:MM:SS` (вычисляется один раз на `seq`) |
| `output` | str | `output` (`"none"`) |
| `protocolNote` | str | версия сессии для текста «старый блок» |
| `command` | dict | `{"state": idle\|sending\|done\|error, "op", "result", "detail", "id"}`; `id` — номер команды пульта (по нему `control.py` ждёт свой итог) |
| `labScenarios` | list | `wire.LAB_SCENARIOS`, если `isLab`, иначе `[]` |

| Slot | Возврат | Поведение |
|---|---|---|
| `requestReinit(confirmed: bool)` | bool | без `confirmed` — `False`; иначе `module_command("reinit")` в фоновом потоке, итог через сигнал в поток GUI (как `_selectDone`) |
| `requestReset(confirmed: bool)` | bool | то же, `reset` |
| `setEnabled(enabled: bool, confirmed: bool)` | bool | `configure {"enabled": …}`, `wait_s=30` |
| `setAutoInit(value: bool, confirmed: bool)` | bool | `configure {"auto_init": …}` |
| `configure(args: dict, confirmed: bool)` | bool | общий `configure` (для `control.py`): непустое подмножество `enabled`/`auto_init`/`auto_recover` (bool), `profile` (str), иначе `False` и `result = "bad-request"`; `wait_s=30` |
| `runLabScenario(name: str, confirmed: bool)` | bool | только при `isLab` и имени из `labScenarios`; иначе `False` и `result = "forbidden"` (не лабораторный бэкенд) или `"bad-request"` (имя) |
| `snapshot()` | dict | формат `M` (раздел 9.3) — для `control.py` и `dump_ui` |

Одновременно одна команда (во время `sending` слоты возвращают `False`, `command` не меняется); при `support != ok` все слоты возвращают `False` и пишут `command.state = "error"`, `result = "no-link"|"unsupported"`; клиент без `module_command` — `unsupported`. Вызов клиента не вернулся за `wait_s + 5` с — `timeout`; связь потеряна во время `sending` — сразу `error`/`no-link`, поздний ответ прежнего запроса отбрасывается. Ответ клиента: `{"ok": true}` — `done`; иначе `error` с `result` клиента (`failed`, если его нет) и `detail = error`; исключение — `error` с текстом исключения. `changed` испускается только при изменении видимого (не на каждом такте бэкенда).

**Нет связи с блоком** (`support = no-link`): плашка состояния показывает «Нет связи с удалённым блоком», а не последнее состояние (нельзя показать «Готов» по старым данным); последние известные значения видны приглушённо с подписью «Данные на %1 · последнее известное состояние: …» (`stale = true`); кнопки видны, но неактивны. **Старый блок** (`old-remote`): плашка «Не поддерживается блоком», рядом «Удалённый блок не поддерживает внешний модуль (протокол %1)», кнопок нет (так же при `absent` и `local`). **Перезапуск GUI**: состояния на пульте нет — всё восстанавливается из первой телеметрии; до неё `no-link`.

### 10.3. `ModulePage.qml`

Разметка в стиле `DiagnosticsPage.qml`/`VideoPage.qml` (Tile, палитра `win.p`, масштаб `win.s`). Обязательные `objectName` (на них опираются тесты C и `dump_ui`):

| objectName | Что |
|---|---|
| `moduleLabBanner`, `moduleLabBannerText` | плашка «Лабораторный эмулятор, не физический JR-модуль» — видна всегда, когда `isLab` **или** `support != "ok"` (в S6 — всегда) |
| `moduleSupportText` | пояснение при `support != "ok"`: `no-link` — «Команды недоступны, пока связь не восстановится.», `old-remote`/`absent`/`local` — фразы из таблицы переводов ниже |
| `moduleStateBadge`, `moduleStateText` | цвет и текст состояния. При `support = ok` — текст состояния 4.1, для `FAULT` всегда «Ошибка» (JR-06: `stateText = «Ошибка»`); при `support != ok` — «Нет связи с удалённым блоком» / «Не поддерживается блоком» / «Нет данных модуля» / «Нет удалённого блока» |
| `moduleStateDetail` | рядом с плашкой при `FAULT`: описание кода ошибки 4.4 («Ошибка интерфейса», «Эмулятор недоступен» …) — так «Ошибка интерфейса» раздела 4.1 видна крупно, не ломая JR-06 |
| `moduleProfileText` | «RadioMaster Nomad (radiomaster-nomad)» |
| `moduleEmulatorText` | «Эмулятор: работает · перезапусков 0 из 3 · PID 1234» |
| `moduleErrorText` | «Последняя ошибка: Ошибка интерфейса в 14:02:31», «(активна)» при `error.active`, «Последняя ошибка: нет», если не было |
| `moduleStaleText` | «Данные на 14:02:40 · последнее известное состояние: Готов» (только при `stale`) |
| `moduleCommandText` | ход и итог последней команды: «Команда «переинициализация» отправляется на удалённый блок…» / «…: выполнена» / «… не выполнена: <причина> (<detail>)» |
| `moduleChangedText` | «Изменено: 14:02:31 (12 с назад)» |
| `moduleOutputText` | «Выход управления: нет (S6)» |
| `moduleDiagList`, `moduleHistoryList` | диагностика (идентичность, `init_ms`, `pong_age_s`, счётчики, ревизия и источник настроек, `notice`, сессия протокола) и история переходов |
| `moduleReinitButton`, `moduleResetButton`, `moduleEnableButton`, `moduleConfirmDialog` | команды только через диалог подтверждения (`moduleConfirmQuestion` — текст: что произойдёт; для сценария — «Действует только на эмулятор: физического модуля и выхода управления нет.»); «ОК» по Enter, отмена по Esc; ряд кнопок `moduleCommandRow` скрыт при `old-remote`/`absent`/`local`, неактивен при `no-link` и во время команды; `moduleReinitButton`/`moduleResetButton` неактивны при `DISABLED` |
| `moduleLabPanel`, `moduleLabButton_<scenario>` | лабораторные сценарии (`moduleLabButton_detach` …), только при `isLab`; кнопки — делегаты `Repeater` (искать обходом `childItems`, не `findChild`) |
| `moduleTile`, `moduleDiagTile`, `moduleInfoTabs` (`moduleDiagTab`, `moduleHistoryTab`), `moduleNote` | плитка модуля; панель «Диагностика / История переходов» вкладками (на 1280×800 обе в столбик не помещаются); пояснение «пульт только показывает…» — второстепенная строка плитки (`Tile.optional`) |

Компоновка на 1280×800: плашка эмулятора (с «Выход управления: нет (S6)» справа) → плашка состояния, `moduleStateDetail`/`moduleSupportText`, «Изменено» → плитка модуля (3/5 ширины) и панель диагностики (2/5) → панель сценариев на всю ширину (сетка до 5 колонок, кнопки 48 px). Кнопки команд 52 px.

Цвета: `READY` — `ok`; `DISCONNECTED` — `warning`; `RECOVERING`, `DETECTED`, `INITIALIZING` — `info`; `FAULT` — `fault`; `DISABLED` — `muted`; `no-link` — `warning`; `old-remote`, `absent`, `local` — `muted`.

Исходные строки QML — английские, перевод в `sheregesh_ru.ts` (обязательные):

| Источник (qsTr) | Перевод |
|---|---|
| Module | Модуль |
| External module | Внешний модуль |
| Laboratory emulator, not a physical JR module | Лабораторный эмулятор, не физический JR-модуль |
| Module absent / Module detected / Initializing / Ready / Fault / Interface error / Recovering / Disabled | Модуль отсутствует / Модуль обнаружен / Инициализация / Готов / Ошибка / Ошибка интерфейса / Восстановление / Отключён |
| No link to the remote unit | Нет связи с удалённым блоком |
| The remote unit does not support the external module (protocol %1) | Удалённый блок не поддерживает внешний модуль (протокол %1) |
| The remote unit does not report the module state | Удалённый блок не сообщает состояние модуля |
| The external module is available only with a remote unit | Внешний модуль доступен только с удалённым блоком |
| Emulator: %1 · restarts %2 of %3 · PID %4 | Эмулятор: %1 · перезапусков %2 из %3 · PID %4 |
| running / starting / restarting / stopped / unavailable | работает / запуск / перезапуск / остановлен / недоступен |
| Last error: %1 at %2 / none | Последняя ошибка: %1 в %2 / нет |
| Changed: %1 (%2 s ago) | Изменено: %1 (%2 с назад) (между числом и единицей — неразрывный пробел, `test_number_and_unit_never_split_across_lines`); от 120 с — «%2 мин назад» |
| Not supported by the remote unit / No module data / No remote unit | Не поддерживается блоком / Нет данных модуля / Нет удалённого блока |
| Control output: none (S6) | Выход управления: нет (S6) |
| Data as of %1 | Данные на %1 |
| Reinitialize / Reset error / Enable module / Disable module | Переинициализировать / Сбросить ошибку / Включить модуль / Отключить модуль |
| Laboratory scenarios | Лабораторные сценарии |
| тексты кодов ошибок раздела 4.4 | как в разделе 4.4, кроме `MOD-INIT-TIMEOUT`: «Инициализация не завершилась вовремя» — в `status.module` нет `profile.init_timeout_s`, число «%1 с» взять неоткуда |

### 10.4. `dump_ui` (`app.py`)

В файл `SHEREGESH_UI_STATE` (`/run/sheregesh-ui/ui-state.json` на pi1) добавляется:

```json
"module": {"support": "ok", "state": "READY", "seq": 17, "stale": false,
           "stateText": "<text moduleStateText>", "stateDetail": "<text moduleStateDetail>",
           "supportText": "<text moduleSupportText>", "profileText": "<text moduleProfileText>",
           "errorText": "<text moduleErrorText>", "emulatorText": "<text moduleEmulatorText>",
           "changedText": "<text moduleChangedText>", "outputText": "<text moduleOutputText>",
           "labBanner": true, "faultEvent": false, "command": { …ModuleView.command… }}
```

Строит `app.module_ui_state(ui)`. Тексты и `labBanner` не зависят от открытой вкладки: у элементов с условной видимостью (`moduleLabBanner`, `moduleStateDetail`, `moduleSupportText`, `moduleChangedText`) условие вынесено в свойство `shown`, а не читается `visible` (у элементов неоткрытой страницы `StackLayout` `visible = false`); скрытый по условию текст — `""`. `faultEvent` — активно ли событие `MODULE-FAULT` в мониторе. Запись — по прежнему таймеру и при смене `(support, state, seq)`.

## 11. Приёмка JR-01…JR-10 (D)

Предусловия: обе платы на одном коммите S6 (`shdeploy deploy pi1`, `shdeploy deploy pi2`; сессия 1.3), связь CONNECTED. Скрипт `tools/probe/s6_jr_accept.py` по образцу `s5_accept.py`/`s5_pi2_accept.py`: `--plan`, `--out DIR`, `--only JR-..`, `--reboot` (разрешает перезагрузку pi2), вердикты PASS | FAIL | ERROR | BLOCKED | SKIP (`S5.verdict_of`). Средства:
- **команды** — на pi1 через работающий пульт: `{REL}; PYTHONPATH=$C/software python3 -m sheregesh.link.modulectl … --via console` (как `videoselect()` в `s5_pi2_accept.py`);
- **GUI** — выборки `ui-state.json` pi1 (ключ `module`), `S4.Stand`/семплер;
- **блок** — выборки `/run/sheregesh-remote/link-status.json` pi2 (`module` = `ModuleService.health()`: `state`, `seq`, `since_s`, `profile`, `backend_state`, `pid`, `restarts`, `error_code` — код последней ошибки, как `status.module.error.code`, хранится и после восстановления, `error_active`, `settings_revision`, `settings_source`) через `S4.RemoteSampler`; процессы — `pgrep -u sheregesh-ui -f sheregesh_emulators.jrmodule`, `systemctl show -p MainPID,NRestarts sheregesh-remote-net`, `ps -o ppid= -p PID`;
- **журнал** pi2 — `journalctl -u sheregesh-remote-net --since …` (строки `[remote] module: …`), если доступен пользователю; иначе история `status.module.history`.

Пределы (`LIMITS`): `ready_s` 15, `detect_s` 5, `fault_s` 6, `recover_s` 20, `crash_recover_s` 15, `unavailable_s` 30, `hold_s` 30, `service_restart_s` 30, `reboot_s` 300.

| JR | Проверка | PASS, если |
|---|---|---|
| JR-01 Эмулятор доступен на pi2 | link-status pi2; `pgrep`; `MainPID` службы | `backend_state = running`; ровно один процесс `jrmodule`, его родитель — `MainPID` `sheregesh-remote-net`; служба `active` |
| JR-02 Пульт показывает профиль Nomad | `modulectl status`; ui-state | `telemetry.profile.id = radiomaster-nomad`, `name = RadioMaster Nomad`; `profileText` содержит «RadioMaster Nomad»; `labBanner = true` |
| JR-03 Инициализация завершается | `modulectl reinit --expect READY --expect-timeout 15` | `result ok`; в `history` есть `INITIALIZING → READY` с `init-ok`; `diag.init_ms ≤ 5000`; от команды до `READY` ≤ `ready_s` |
| JR-04 GUI показывает READY | ui-state | `module.state = READY`, `stateText = «Готов»` не позже 2 с после `READY` в link-status pi2; при `--gui` — снимок `gui_capture` страницы `module` |
| JR-05 Отключение обнаруживается | `lab detach`, затем `lab attach` | `stateText = «Модуль отсутствует»` ≤ `detect_s`; затем `READY` ≤ `ready_s` |
| JR-06 Ошибка отображается | `lab iface-error`; затем (после JR-07) `lab hang` | `stateText = «Ошибка»`, `errorText` содержит «Ошибка интерфейса», `faultEvent = true` ≤ `fault_s`; для `hang` — первый `FAULT` в `history` с кодом `MOD-NO-RESPONSE` ≤ `fault_s` (дальше последняя ошибка может смениться на `MOD-INIT-TIMEOUT` — это ожидаемо) |
| JR-07 Восстановление работает | после `iface-error` — `lab heal`; после `hang` — `lab heal` | в истории есть `RECOVERING`; `READY` ≤ `recover_s` без команд `reset`/`reinit`; `errorText` по-прежнему показывает последнюю ошибку, `faultEvent = false` |
| JR-08 Перезапуск службы обрабатывается | (a) `lab crash`; (b) `pkill -KILL -u sheregesh-ui -f sheregesh_emulators.jrmodule` на pi2; (c) `lab emu-unavailable`, удержание `hold_s`, `lab emu-available`, `reset`; (d) `systemctl restart sheregesh-remote-net` | (a), (b): `FAULT EMU-EXITED`, затем `READY` ≤ `crash_recover_s`, новый PID, `restarts` +1, процесс один; (c): `FAULT EMU-UNAVAILABLE` ≤ `unavailable_s`, за `hold_s` нет ни одного процесса `jrmodule`, `restarts` не растёт, нет новых строк `emulator started`; после `reset` — `READY` ≤ `ready_s`; (d): ui-state `support no-link`, затем `READY` ≤ `service_restart_s`; ровно один `jrmodule`, родитель — новый `MainPID`; прежний PID отсутствует; `NRestarts` службы не вырос; у агента пульта `NRestarts` не вырос |
| JR-09 Настройки сохраняются | `configure enabled=false` → `systemctl restart sheregesh-remote-net` → проверка → `configure enabled=true`; при `--reboot` — то же через перезагрузку pi2 | после перезапуска: `DISABLED`, процессов `jrmodule` нет, `settings.revision` та же, что после `configure`, `settings.source = store`; после `enabled=true` — `READY` ≤ `ready_s`. В `finally` исходные настройки возвращаются. Перезагрузка pi2 — **только** если агент OTA pi2 сообщает `COMMITTED` (правило `s5_pi2_accept.reboot_allowed`), иначе подшаг BLOCKED: обычная перезагрузка пробной загрузки вернёт прежний слот |
| JR-10 OTA-state не изменился | `sheregesh-ota-client status` на pi1 и pi2 до первого шага и после последнего (`S5_pi2.ota_summary`); `readlink /var/lib/sheregesh-deploy/current`; `boot_id` | состояние, слот, версия, раздел загрузки совпадают; выпуск DEV deploy прежний; `boot_id` прежний (кроме pi2 при `--reboot`); `systemctl --failed` пуст на обеих платах |

Порядок шагов: JR-10 «до» → 01 → 02 → 03 → 04 → 05 → 06 (iface) → 07 (heal) → 06 (hang) → 07 (heal) → 08a–d → 09 → JR-10 «после». В `finally`: `lab heal`, `lab attach`, `lab emu-available`, `reset`, исходные настройки, ожидание `READY`.

## 12. Журнал, события, безопасность вывода

### 12.1. Журнал блока

Через существующий `log()` агента (stderr → journald `sheregesh-remote-net`), префикс `[remote] module: `. Пишутся: каждый переход (`DISCONNECTED -> DETECTED (present) profile=radiomaster-nomad`, для `FAULT` — с `code=…`), запуск и выход эмулятора (`emulator started pid=N`, `emulator exited code=70 (restart 1/3 in 60 s)`), исчерпание бюджета, лабораторные команды (`lab detach (req 5)`), команды `reinit`/`reset`/`configure` с итогом, сохранение настроек (`settings revision r000002 saved`) и действия восстановления хранилища (`settings recovered: CFG-STATE-PREV`), внутренние ошибки. Повторяющиеся неудачные попытки восстановления — строка `recovery attempt N in D s` на попытку плюс строки её переходов (обычно 3–4); попытки не чаще backoff (в установившемся режиме — раз в 30 с). `link-status.json` получает `module = ModuleService.health()`.

### 12.2. Пульт

- Журнал приложения (экран «Журнал»): `Backend.log(level, "module", …)` на каждую смену `seq`, `support` и итог команды; уровень: `READY` — info, `FAULT` — error, `DISCONNECTED`/`RECOVERING` — warning, прочие — info.
- Системный журнал: `self.journal.emit(prio, "module: <STATE> (from=… reason=… code=… profile=… backend=…)")` только на смену `seq` или `support` (как `_journal_transitions`): `READY` — `INFO`, `FAULT` — `ERR`, `DISCONNECTED`/`RECOVERING` — `WARNING`, прочее — `NOTICE`.
- Диагностическое событие монитора: новый код `MODULE-FAULT` в `faults.CATALOG` (`WARNING`, `latched=True`; RU «Внешний модуль: ошибка {code}», EN «External module fault: {code}»; действие RU «Состояние и восстановление — на экране «Модуль».»). `Backend` делает `self.bench.events.inject("MODULE-FAULT", code=…)` при `support = ok` и `state = FAULT`, иначе `self.bench.events.clear("MODULE-FAULT")`.

### 12.3. Никогда нет самопроизвольного перехода к активному аппаратному выводу

Гарантируется тем, что выхода нет вовсе, и это проверяется:
1. **Кодом.** В `sheregesh.module` и `jrmodule.py` нет API вывода; поле `output` — константа `"none"` в `ModuleManager.snapshot()`, не зависящая от состояния. `HardwareBackendStub` не конструируется (`ModuleError`), `make_backend` для любого вида, кроме `lab-emulator`, — `ModuleError`; схема `remote-1` допускает только `backend: lab-emulator`. `module-cmd` не принимает никаких аргументов, кроме перечисленных в 6.3 (`bad-request`), — каналов, значений органов управления и команд «взвести» в протоколе нет.
2. **Тестом `tests/unit/test_module_no_output.py`** (B): (а) разбор AST всех файлов `software/sheregesh/module/**` и `software/emulators/sheregesh_emulators/jrmodule.py` — запрещены импорты `serial`, `termios`, `tty`, `fcntl`, `gpiod`, `RPi`, `spidev`, `smbus`, `smbus2`, `periphery`, `usb`, `pyudev`, `socket`, `ctypes`, `mmap` и строковые литералы, начинающиеся с `/dev/` или `/sys/class/`; (б) прогон `SCRIPT_FULL` и всех сценариев раздела 6.4 на `InlineEmulatorBackend`+`SimClock` — на каждом такте `snapshot()["output"] == "none"`; (в) `HardwareBackendStub()` и `make_backend(backend="hardware")` → `ModuleError`; схема отвергает `backend: hardware`; (г) `command("lab", …)` при `supports_lab=False` → `forbidden`; (д) `wire.check_command` отвергает любые ключи `args`, кроме разрешённых.

### 12.4. Отказы

| Отказ | Поведение |
|---|---|
| pi1 стартует раньше pi2 | `support = no-link`, «Нет связи с удалённым блоком»; после подключения — фактическое состояние блока |
| pi2 стартует раньше pi1 | модуль доходит до `READY` автономно (≤ 15 с от старта агента); пульт после подключения показывает `READY` и историю |
| Эмулятор недоступен | `FAULT EMU-START-FAILED` → не более 3 запусков за 60 с → `FAULT EMU-UNAVAILABLE`, процессов нет, ожидание команды `reset`; агент работает, видео и канал не затронуты |
| Эмулятор аварийно завершается | `FAULT EMU-EXITED` → перезапуск по бюджету → `READY`; четвёртая авария за 60 с → `EMU-UNAVAILABLE` |
| Пропал и вернулся Ethernet | блок продолжает работу; пульт: `no-link`, кнопки неактивны, незавершённая команда — `no-link` (итог неизвестен, видно по `settings`/`state` после возврата); после возврата `cmd` прежнего сеанса не показывается, история догоняет |
| Перезапуск GUI | состояние только из телеметрии; до первого `status` — `no-link` |
| Перезапуск службы / блока | заново из настроек: `DISABLED` или `DISCONNECTED → … → READY`; лабораторные флаги сброшены; бюджет перезапусков новый |
| Настройки повреждены / отсутствуют | раздел 8: `recovered` / `reset` (каталог в `remote-config.corrupt-*`) / `factory` / `memory`; модуль запускается, причина в журнале и в `settings.notice` на экране |
| Внутренняя ошибка подсистемы | исключение перехвачено в `ModuleService`, журнал, `FAULT`; агент, heartbeat и сторож systemd не затронуты |

## 13. Тесты

| Уровень | Файл (владелец) | Что проверяет |
|---|---|---|
| L2 | `tests/unit/test_module_states.py` (B) | `ALLOWED` совпадает с таблицей 4.2; недопустимый переход в `strict` → `AssertionError` |
| L2 | `tests/unit/test_module_manager.py` (B) | `InlineEmulatorBackend`+`SimClock`: каждая строка 4.2 и 6.4; таймауты 4.3 по модельному времени; backoff; бюджет 3/60 с и `EMU-UNAVAILABLE`; `reset`/`reinit`; `auto_init=false`; `DISABLED` останавливает бэкенд; `SCRIPT_FULL` даёт ожидаемые состояния; одинаковый прогон → одинаковая `history` |
| L2 | `tests/unit/test_module_profiles.py` (B) | Nomad: `matches`, `describe`; `register_profile` нового профиля (поддельный «crossfire-test») работает **без изменений менеджера** |
| L2 | `tests/unit/test_module_settings.py` (B) | нет каталога → `factory`; битый `state.json` → `recovered`; все ревизии битые → `reset` и каталог `*.corrupt-*`; каталог только для чтения → `memory`, `configure` → `not-saved`; `invalid` для неизвестного профиля; повторная загрузка возвращает сохранённое |
| L2 | `tests/unit/test_module_wire.py` (B) | `sanitize_module`: усечение, типы, `v != 1`, неизвестный `state`, `output ≠ none` → `unknown`; `check_command`: лишние ключи, неверный `req`, неизвестные `op`/сценарий |
| L2 | `tests/unit/test_module_no_output.py` (B) | раздел 12.3, п. 2 |
| L2 | `tests/unit/test_jrmodule_emulator.py` (B) | ядро эмулятора: ответы на `probe/init/ping/keepalive`, сценарии, детерминизм |
| L2/L3 | `tests/unit/test_module_lab_backend.py` (B) | реальный дочерний процесс: `hello`, `crash` → `channel-down code=70`, внешний SIGKILL, `--refuse-start`, отсутствие `hello` (подмена команды `sleep`), строка > 4096 байт, неблокирующая запись при зависшем процессе, `close()` ≤ 1 с, нет сирот |
| L3 | `tests/system/test_module_agent.py` (B) | процесс агента `--module --remote-config-dir tmp` + `PeerClient` на loopback: `status.module` → `READY`; `module_command` всех `op`; `crash` и SIGKILL дочернего процесса → восстановление; бюджет; SIGTERM агента → нет процесса `jrmodule`; перезапуск агента с тем же каталогом — настройки сохранены |
| L3 | `tests/system/test_module_compat.py` (B) | агент `--protocol 1.2` + клиент 1.3 → `unsupported`, `status.module` нет; сырой сеанс 1.2 шлёт `module-cmd` → не исполняется, учитывается; клиент 1.2 (`_raw_session` как в `test_video_select.py`) с агентом 1.3 — прежнее поведение |
| L2 | `tests/unit/test_console_module_view.py` (C) | `ModuleView.update`: все `support`, `stale`, `changedAt` по `seq`, коды ошибок → тексты, `labScenarios` пуст без `isLab`, слоты без `confirmed` → `False` |
| L3 GUI | `tests/gui/test_gui_module.py` (C) | offscreen: вкладка `tabModule` (F8), все `objectName` 10.3, тексты RU для каждого состояния и `support`, плашка видна всегда, кнопки неактивны при `no-link`, подставной клиент с `module_command`; индексы вкладок 0…6 не изменились |
| L3 GUI | `tests/gui/test_console_module_control.py` (C) | `{"cmd":"module",…}` через сокет: `status`, `lab`, `configure`, `busy`, `bad-request`, ответ при отсутствии связи |
| L3 | `tests/gui/test_s6_jr_loopback.py` (D) | процесс агента (`--module`) + настоящий `Backend`/`Ui` offscreen + `modulectl --via console`: JR-01…JR-09 в loopback с ui-state (`SHEREGESH_UI_STATE`) |
| L2 | `tests/unit/test_s6_jr_accept.py` (D) | чистые функции судейства `s6_jr_accept.py` (как `test_s5_pi2_accept.py`) |
| L4 | `tools/probe/s6_jr_accept.py` (D) | раздел 11 на pi1+pi2 |

Существующие наборы `tests/unit`, `tests/gui`, `tests/system` проходят без изменения ожиданий, кроме версии сессии `1.2 → PROTOCOL_VERSION` (раздел 3.2).

## 14. Чего нет в S6

- Радиоуправляющих сигналов, каналов, значений органов управления, телеметрии радиолинии, взведения и любого «выхода управления»; `output` всегда `"none"`.
- Аппаратного backend: ни открытия UART/USB/`/dev/*`, ни GPIO, ни питания модуля; `HardwareBackendStub` — только интерфейс с отказом в конструкторе.
- Профиля TBS Crossfire (только доказанная тестом расширяемость реестра профилей).
- Изменений OTA, A/B, установщика, launcher, образа, systemd-юнитов, polkit, видеоподсистемы и видеоканала.
- Изменения требований и реестра TEST-ID; ADR канала управления наземными платформами (D6) — отдельный, следующий.
- Выбора профиля из GUI (показывается; меняется командой `configure profile=…`, в S6 профиль один).
- Синхронизации часов плат; абсолютные времена блока не передаются.

## 15. Отклонённые варианты

- **Эмулятор в процессе агента (поток/корутина)** — авария эмулятора не отделима от аварии агента, «аварийное завершение службы эмулятора» не проверяется по-настоящему. Остаётся как `InlineEmulatorBackend` для детерминированных тестов.
- **Отдельный systemd-юнит эмулятора** — новый юнит и правило polkit требуют образа (ADR-0010).
- **Эмулятор как сетевой или unix-сервер** — лишняя поверхность (порт или сокет на запись в `ProtectSystem=strict`), обнаружение и аутентификация; pipe к дочернему процессу проще и не виден снаружи.
- **Отдельный тип SHRG-LAB на каждую команду** (`module-reinit`, `module-lab`, …) — больше типов и правил совместимости при той же семантике; один `module-cmd` с `op` расширяется без новой MINOR для новых сценариев.
- **Состояние модуля через `event`** — пульт принимает только коды из `CATALOG`, а состояние — не событие; `status.module` даёт полное состояние при каждом подключении.
- **Отдельный `module.json` рядом с `video-input.json`** — нет ревизий, подтверждения и восстановления ADR-0004.
- **Разрешение лабораторных команд флагом в настройках** — флаг можно включить той же командой `configure`; запрет по виду бэкенда структурный.
