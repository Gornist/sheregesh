# E2E-13 — кандидат для OTA pi1: гейты до платы (2026-10-25)

**Кандидат:** коммит v2 `09fca4e6ca073b33cf2b82c7171957ed55d59f5b`, сборка devbox (build1, `SHEREGESH_BUILD_AS_ROOT=1`,
`SOURCE_DATE_EPOCH` 1792917547), образ `image_sha256` `1aef13e506e6f555a197a0af3b3d8ef8eb5d31d987ac8c75f9acb938d0e412e8`.
**Пакет:** `sheregesh-ota_sheregesh-lab-dev-pi4_0.1.1792917547.shota`, SHA-256
**`d38cc0f09c514b06e0ea3640fd2f708bed301716ec7b86b8bf1093dfed554910`**, V = 0.1.1792917547 (артефакты на маке:
`build/image/devbox-09fca4e-run1/`). SHA пакета утверждает владелец.

| Гейт | Результат | Файл |
|---|---|---|
| SHA256SUMS артефактов (мак) | OK ×5 | `sha256sums-check.txt` |
| verify_image | 72/0/0, в т. ч. `boot.order` PASS | `verify.json` |
| order_check (systemd-analyze, 8 прогонов) | PASS: циклов упорядочения нет | ниже |
| owners_check против CI 57e8fe4 | список совпадает (10 строк, `diff` пуст) | `owners-run1.txt`, `owners-ci.txt` |
| Содержимое: `notify_relay.py` | = `09fca4e` (61381d34…) — ретранслятор с кодом 0 на SIGTERM | — |
| Содержимое: `sheregesh-console.service`, `sheregesh-var-seed.service` | = репозиторий (0787042c…, 8fce3de6…) | `manifest-check.txt` |
| .deb образа = дерево 09fca4e | 83 файла; отличается только `build.json` локальной пересборки (коммит/время) | `deb-vs-repo.diff` |
| Пакет воспроизводим | две сборки, `cmp` пакета и манифеста — совпадают | `payload-sha256.txt` |
| Подпись по `allowed_signers` плат (74c95610…00c0) | `Good "sheregesh-ota"`, ключ SHA256:5wJfw7Qw…, trailing 0, version_gt_active | `payload-check.json` |
| Манифест | commit, V, compatible dev, layout, image_sha256, units — PASS | `manifest-check.txt` |
| L3 агентом 57e8fe4 (установит на pi1) | 39/39, обязательные PASS | `l3-agent-57e8fe4.*` |
| L3 агентом кандидата (09fca4e) | 39/39, обязательные PASS | `l3-agent-candidate.*` |
| CI | на 09fca4e прогон отменён (вытеснен); зелёный на потомке 0b9b036 — `software/`, `os/`, `tools/image`, `tools/deploy` идентичны | — |

order_check: `systemd 257 (257.13-1~deb13u1); прогонов: 8` → `PASS: циклов упорядочения нет`
(для сравнения: образ цикла 2 — FAIL в каждом прогоне, см. `../hardware/cycle2/analysis/cycle-c2.txt`).

## Отклонение: build-report `dirty: true`
- Причина — сценарий сборки, не исходники: `rsync --exclude build` в `os/docker-build.sh` не скопировал в дерево
  сборки отслеживаемый каталог `docs/acceptance/ota-closure/build/` (18 файлов: журналы и JSON прошлой приёмки);
  `git status` в дереве сборки показал ровно эти 18 удалений и ничего больше.
- Ни один из них не входит в .deb (`build_deb.SOURCES`: `software/…`, `tools/probe/s2probe.py`, `tools/dev/soak.py`),
  в `os/` или `tools/image/`. Содержимое .deb пофайлово совпадает с деревом (строка выше), `build.json` образа —
  коммит 09fca4e, V 0.1.1792917547.
- Сценарий исправлен для следующих сборок: исключения только в корне (5509311), повторная сборка после сборки от root
  (d25770d). Пересборка build2 остановлена решением главного агента (скорость; отклонение оформлено доказательствами).
