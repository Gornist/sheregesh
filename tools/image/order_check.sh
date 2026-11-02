#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Циклы упорядочения systemd в корне системного раздела образа (system_a): systemd-analyze verify default.target
# и sysinit.target, несколько прогонов (систему разрывает цикл, удаляя задание; выбор зависит от порядка обхода и от
# прогона к прогону разный). Код 0 — циклов нет; 1 — найдены (строки «ordering cycle»/«deleted» в выводе); 3 — проверка
# не отработала (нет default.target, юниты не загружены или контрольный цикл не найден — проверка слепа).
# Только чтение: раздел монтируется ro,noload под overlay в tmpfs одноразового привилегированного контейнера
# (Linux-хост с Docker, devbox). Генераторы не запускаются (persistent.mount и т. п. не проверяются).
#   tools/image/order_check.sh ОБРАЗ.img.zst [ПРОГОНОВ]
# Найдено в OTA CLOSURE (цикл 2): sheregesh-var-seed Before=systemd-sysusers при After=local-fs.target — цикл; на
# плате systemd удалил задание (в цикле 1 — sysusers, безвредно; в цикле 2 — по-видимому, local-fs.target), слот B
# загрузился без /persistent и служб. verify_image и L3 (файловое испытание) этого не видели.
set -euo pipefail
IMG="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"; RUNS="${2:-8}"
W=$(mktemp -d "$(dirname "$IMG")/.order.XXXXXX"); trap 'rm -rf "$W"' EXIT   # распаковка — рядом с образом
# SHBOOT_RAW — образ задания, уже распакованный сервисом (разреженно, один раз на все этапы); образ инструментов
# shbuild-qemu (zstd, fdisk, systemd) собран заранее. Нет того или другого — как раньше (debian:trixie + apt + распаковка).
RAWARGS=(); [ -n "${SHBOOT_RAW:-}" ] && [ -s "$SHBOOT_RAW" ] && RAWARGS=(-v "$SHBOOT_RAW":/img.raw:ro)
TOOLS=debian:trixie; docker image inspect shbuild-qemu >/dev/null 2>&1 && TOOLS=shbuild-qemu
docker run --rm --privileged -e DEBIAN_FRONTEND=noninteractive -e RUNS="$RUNS" -v "$IMG":/img.zst:ro -v "$W":/w ${RAWARGS[@]+"${RAWARGS[@]}"} \
  "$TOOLS" bash -euo pipefail -c '
  command -v sfdisk >/dev/null && command -v zstd >/dev/null && command -v systemd-analyze >/dev/null \
    || { apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends zstd fdisk systemd >/dev/null; }
  if [ -e /img.raw ]; then F=/img.raw; else zstd -qdf --sparse /img.zst -o /w/img; F=/w/img; fi
  read -r start size < <(sfdisk -d $F | awk -F"[=,]" "/name=\"system_a\"/{gsub(/ /,\"\"); print \$2, \$4}")
  [ -n "$start" ] && [ -n "$size" ]
  mkdir -p /lo /ov /r && mount -t tmpfs tmpfs /ov && mkdir -p /ov/up /ov/wk
  mount -o ro,noload,loop,offset=$((start*512)),sizelimit=$((size*512)) $F /lo
  mount -t overlay overlay -o lowerdir=/lo,upperdir=/ov/up,workdir=/ov/wk /r   # verify пишет служебное в корень
  echo "$(systemd-analyze --version | head -1); прогонов: $RUNS"
  # Корень настоящий? Без default.target проверка ничего не прочитала бы и «не нашла бы» циклов.
  [ -e /r/usr/lib/systemd/system/default.target ] || [ -e /r/etc/systemd/system/default.target ] \
    || { echo "ERROR: в образе нет default.target — проверка не читает юниты"; exit 3; }
  # Канарейка: заведомый цикл в оверлее. Если проверка его не видит, она слепа (другой формат вывода, не те ключи,
  # не загрузились юниты) и «циклов нет» ничего не значит.
  mkdir -p /r/etc/systemd/system
  printf "[Unit]\nDescription=canary a\nWants=zz-canary-b.service\nAfter=zz-canary-b.service\n[Service]\nExecStart=/bin/true\n" > /r/etc/systemd/system/zz-canary-a.service
  printf "[Unit]\nDescription=canary b\nWants=zz-canary-a.service\nAfter=zz-canary-a.service\n[Service]\nExecStart=/bin/true\n" > /r/etc/systemd/system/zz-canary-b.service
  canary=$(systemd-analyze --root=/r verify --man=no zz-canary-a.service 2>&1 || true)
  printf "%s\n" "$canary" | grep -qE "ordering cycle|deleted to break" \
    || { echo "ERROR: контрольный цикл не обнаружен — проверка слепа; вывод:"; printf "%s\n" "$canary" | head -5; exit 3; }
  rm -f /r/etc/systemd/system/zz-canary-a.service /r/etc/systemd/system/zz-canary-b.service
  echo "канарейка: контрольный цикл найден"
  found=0
  for i in $(seq 1 "$RUNS"); do
    full=$(systemd-analyze --root=/r verify --man=no default.target sysinit.target 2>&1 || true)
    # проверка не отработала (юниты не загружены), а не «циклов нет»
    if printf "%s\n" "$full" | grep -qE "Failed to (load|create|determine|initialize)|Cannot (access|open)|Unit (default|sysinit)\.target not found"; then
      echo "ERROR: прогон $i не прочитал юниты:"; printf "%s\n" "$full" | head -5; exit 3
    fi
    out=$(printf "%s\n" "$full" | grep -E "ordering cycle|deleted to break" || true)
    if [ -n "$out" ]; then found=1; printf "%s\n" "$out" | sed "s/^/прогон $i: /"; fi
  done
  umount /r /lo
  if [ "$found" = 0 ]; then echo "PASS: циклов упорядочения нет"; else echo "FAIL: найдены циклы упорядочения"; exit 1; fi
'
