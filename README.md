# Шерегеш 1.0

Открытая воспроизводимая DIY-платформа для роботов, роверов и лабораторных стендов: **пульт** и **удалённый блок** на Raspberry Pi 4B, связанные проводным Ethernet. Для разработки и испытаний без оборудования есть программные эмуляторы и синтетические данные.

**Статус:** этап S6 — пульт и удалённый блок на стенде: видео, OTA A/B, внешний модуль ELRS. Ни одно требование не имеет статуса PASS; гейт релиза — NO-GO.

## Где что лежит

| Путь | Содержание |
|---|---|
| `docs/srs/SHEREGESH-SRS-1.0.md` | Системные требования, Rev B (раздел 4 генерируется) |
| `docs/requirements/requirements.json` | **Единственный источник** требований, испытаний и изменений |
| `docs/requirements/TEST-MATRIX.csv`, `CHANGELOG-REQ.md` | Генерируются из реестра |
| `docs/tests/TEST-SPEC.md` | Спецификация испытаний (генерируется) |
| `docs/hardware/` | Аппаратная спецификация Rev B, `PRE-CAD-CHECKLIST.md` |
| `docs/adr/` | Архитектурные решения ADR-0001…0008 |
| `docs/decisions/DECISIONS.md` | Журнал решений владельца (D1–D7) и Tech Lead |
| `docs/audit/` | Первичный аудит и реестр проблем AUD-NNN с текущими статусами |
| `docs/security/` | Секреты |
| `docs/baseline/rev-a/` | Неизменяемый комплект Rev A (SHA256SUMS) |
| `hardware/bom/` | BOM v0.4 |
| `tools/reqs/reqgen.py` | Проверка реестра, генерация, гейт релиза |
| `tools/security/scan.py` | Базовые проверки секретов и границ объёма |

## Запуск на эмуляторах (x86/Linux, без Raspberry Pi)

```sh
tools/dev/sheregesh-emu --demo                 # два процесса: удалённый блок (+ синтетическое видео) и пульт, сценарий
tools/dev/sheregesh-emu                        # два процесса без сценария
tools/dev/sheregesh-emu --single --demo        # один процесс: блок эмулируется внутри пульта, локальное видео
tools/dev/sheregesh-emu --demo --speed 10      # ускорить модельное время
tools/dev/sheregesh-emu --screenshots shots/   # снимки сценария без дисплея
tools/dev/sheregesh-emu --gallery gallery/     # полная матрица снимков 1920×1200 (≈ 150 файлов)
tools/dev/sheregesh-emu --soak 900             # длительный прогон с неисправностями, отчёт reports/soak/
tools/dev/sheregesh-emu --test                 # все автотесты (offscreen)
```

При первом запуске создаётся `.venv` из `tools/ci/requirements-dev.txt` (PySide6 6.12, PyYAML, jsonschema, pytest).
Системные пакеты (Ubuntu/Debian): `libegl1 libgl1 libxkbcommon0 libfontconfig1 gstreamer1.0-tools gstreamer1.0-plugins-base
gstreamer1.0-plugins-good gstreamer1.0-libav util-linux`. Без GStreamer всё работает, кроме видео (экран «Видео» показывает «Видео недоступно»).

Процессы связаны только через `127.0.0.1`: TCP 7700 (`SHEREGESH_PORT`) — диагностические данные, UDP 5600 (`SHEREGESH_VIDEO_PORT`) —
синтетическое видео. Удалённый блок отправляет только синтетическое состояние; команд ОС, управления устройствами и радиоинтерфейсов нет.

Сценарий `--demo` (200 с модельного времени): штатное состояние → нагрев и переход на батарею → предупреждение TEMP-HIGH →
пропадание видео (60–75 с) → молчание удалённого блока и потеря связи (95–115 с) → восстановление.
Проверить отказ можно и вручную: завершить процесс `sheregesh_remote` — пульт покажет «Процесс удалённого блока не запущен»;
повторный запуск `PYTHONPATH=software:software/emulators:software/remote python3 -m sheregesh_remote --video` восстановит связь.

Управление без сенсора: `Ctrl+1…7` или `F1…F7` — экраны; `Esc` — к вкладкам; стрелки — между вкладками и плитками;
`Tab` — по полям; `Пробел` или `Enter` — кнопки; в диалоге `Enter` — подтвердить, `Esc` — отменить.

## Программные модули

| Путь | Содержание |
|---|---|
| `software/sheregesh/config/` | Схема и хранилище ревизий конфигурации (ADR-0004) |
| `software/sheregesh/hal.py`, `monitor.py`, `faults.py`, `protocol.py` | Интерфейсы оборудования, монитор состояния, каталог событий RU/EN, совместимость протокола |
| `software/emulators/sheregesh_emulators/` | Эмуляторы и детерминированные сценарии, демонстрационный сценарий |
| `software/sheregesh/link/` | Канал SHRG-LAB: кодек сообщений, клиент пульта |
| `software/sheregesh/video.py`, `protocol.py`, `compat-matrix.yaml` | Конвейеры синтетического видео; правило совместимости (CHG-012) и матрица HW/SW |
| `software/remote/sheregesh_remote/` | Отдельный процесс-эмулятор удалённого блока |
| `software/console/sheregesh_console/` | GUI пульта: бэкенд, приём видео, 7 страниц QML, переводы |
| `tools/dev/` | Лаунчер, длительный прогон (`soak.py`), матрица снимков (`gallery.py`) |
| `tests/unit`, `tests/system`, `tests/gui`, `tests/tools` | Автотесты L2/L3; отчёт `reports/evidence.json` по TEST-ID |

## Проверки

```sh
python3 tools/reqs/reqgen.py validate          # согласованность реестра
python3 tools/reqs/reqgen.py generate          # обновить производные файлы
python3 tools/reqs/reqgen.py gate              # GO/NO-GO (REQ-REL-001)
python3 tools/reqs/reqgen.py evidence          # автотесты против реестра: уровень CI против требуемого
python3 -m unittest discover -s tests/tools -p 'test_*.py'
python3 tools/security/scan.py secrets
```

Инструменты реестра и сканеры используют только стандартную библиотеку Python 3.11+.
Обычный CI показывает NO-GO как информацию; релизный конвейер (`.github/workflows/release.yml`) блокируется гейтом.
