# Лабораторный образ S5 — `sheregesh-lab-dev-pi4` из v2 `bd74c8c` (devbox, 2026-10-25)

Решение владельца: «готовь лабораторный образ с root-помощником для vivid/USB/IP на pi2 и исправлением S5-D1. Сборку на
devbox разрешаю, OTA и запись карт — только после моего отдельного подтверждения». На платы **не ставился**.

**Состав сверх выпуска на платах (g980c45):** S5-D1 (`sheregesh-remote-net`: `ReadWritePaths` каталога конфигурации,
`ReadOnlyPaths` роли и `link.json`), п. 7 — лабораторные источники видео pi2 (`tools/lab-video/pi2`, только dev-слой),
код `software/` по `bd74c8c` (UVC-CVBS 534d:0021 и всё S5).

| Проверка | Итог | Файл |
|---|---|---|
| verify_image | **76 PASS / 0 FAIL / 0 SKIP**, в т. ч. `net.video_config` (S5-D1), `labvideo.files` (10), `labvideo.polkit`, `labvideo.units` | `verify.json` |
| build-report | commit `bd74c8c412028f8f4fb0a4d6b89b738e676db959`, `dirty: false`, `verify_exit: 0` | `build-report.json` |
| image_sha256 (несжатый, 7 021 285 376 Б) | `0ecf3aa93426d60b523329dbce04d20472f4174b371ced647dfa34c64f9d0eb2` | `build-report.json` |
| `img.zst` | `96f29cebe5c849ae957e0ac73892a6434f36c42b894c5f3b4994868c4cbfcbac`; `sha256sum -c` и `zstd -t` — OK | `SHA256SUMS` |
| order_check (systemd 257, 8 прогонов) | PASS: циклов упорядочения нет | — |
| owners_check против CI 57e8fe4 | совпадает (10 строк, diff пуст) | `owners.txt` |
| CI-сборка того же коммита (нативный arm64, ≈ 8 мин) | оба варианта verify_exit 0; `labvideo.absent` PASS в строгом | run 40216454930 (артефакт не выгружен: квота GitHub) |

**Отклонение:** devbox перезагрузился сам в 21:16:21 MSK на шаге отчёта (журнал без штатного завершения). Причина —
глобальная нехватка памяти: параллельно работала Ghidra другого проекта (≈ 13,7 ГБ ОЗУ и 3 ГБ swap), не сжатие
образа (разбор — [`../../s5-ota/DEVBOX-REBOOT.md`](../../s5-ota/DEVBOX-REBOOT.md)). Образ, `verify.json`, `SHA256SUMS` к этому моменту записаны и проверены; `build-report.json`
дописан тем же кодом `os/build.sh` в нативном контейнере (поле `report_completed_after_host_reboot`, `started_utc` —
приблизительно).

Артефакты: `devbox-lan:~/shimg/src/build/image/sheregesh-lab-dev-pi4/`.

**На платах после установки проверить** (только после решения владельца, по одной плате): S5-D1 — выбор источника
переживает `systemctl restart sheregesh-remote-net`; п. 7 — пять пунктов `tools/lab-video/pi2/README.md`.
