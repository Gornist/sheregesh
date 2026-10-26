# Видео пульта мимо Python (ADR-0015): образ, OTA pi1 — состояние на 2026-10-26

**Итог: код и образ готовы, на плате не проверено.** Установка OTA на pi1 выполнена до INSTALLED (слот A);
разрешение переключения (`ota-enable`) и пробная загрузка (`ota-tryboot`) не выполнялись — заблокированы
автоматическим режимом как продуктивное развёртывание, нужно решение владельца. pi1 работает на слоте B как прежде.

## Образ `db36670` (devbox, `os/docker-build.sh`, SHEREGESH_BUILD_AS_ROOT=1)

| Проверка | Итог |
|---|---|
| build-report | commit `db3667031e71…`, `dirty: false`, `verify_exit: 0`; image_sha256 `9920503f…1f85` |
| Пакеты GL | `gstreamer1.0-qt6`, `gstreamer1.0-gl`, `python3-gi`, GIR — в `packages.tsv` (1.26.2 = gst платы) |
| owners_check | = эталон (`owners-bd74c8c`), diff пуст |
| order_check (8 прогонов) | PASS, циклов нет |
| OTA-пакет | `sheregesh-ota_sheregesh-lab-dev-pi4_0.1.1793024692.shota`, sha256 `ee3ec8a0d02849f37990e4becf811365f704a326e8895a697927c61770a10988`, две сборки — REPRODUCIBLE; подпись — Good (allowed_signers pi1) |

## OTA pi1 (`ota-pi1/`)

| Шаг | Итог |
|---|---|
| 0 | verify_payload PASS; ota-status COMMITTED B, consistent; ota-disable; снимки pi1/pi2; check_link и check_video_link PASS |
| 1 | `ota-install` OK → INSTALLED, target A, V 0.1.1793024692 (3 мин 16 с) |
| 1a | `slot_guard compare --mode install` 44/0/0 (после исправления направления B → A в slot_guard); pi2 unchanged 44/0/0 |
| 2–3 | **не выполнено** — `ota-enable` и `ota-tryboot` заблокированы, нужно решение владельца |

Код агента обновлений, health и deploy с образа pi1 (`b1d95a2`, прошлый успешный OTA) не менялся.

## Дальше (после решения владельца)

```bash
D=docs/acceptance/video-gl/ota-pi1; S=tools/deploy/shdeploy.py; G=tools/deploy/ota-check/slot_guard.py
python3 $G snap pi1 192.168.1.92 $D/pi1-pre-enable.json
python3 $G compare $D/pi1-before.json $D/pi1-pre-enable.json --mode install --version 0.1.1793024692
python3 $S ota-enable pi1 --host 192.168.1.92 | tee $D/ota-enable.txt
python3 $S ota-tryboot pi1 --host 192.168.1.92 | tee $D/tryboot.txt       # SSH на .92 через ~25–60 с
# ≥ 180 с: ota-status HEALTH_PENDING, snap trial, compare --mode trial, check_link/check_video_link, затем
python3 $S ota-confirm pi1 --host 192.168.1.92 | tee $D/confirm.json
```
Если pi1 не поднимается после tryboot — выключить и включить питание: прошивка вернёт слот B (tryboot однократен).
После confirm: DEV deploy текущего v2 на pi1 и `ui_bench.py --gl --rtsp rtsp://192.168.1.193:8554/cam` для
640×360 / 1280×720 (матрица — в ADR-0015, §4).
