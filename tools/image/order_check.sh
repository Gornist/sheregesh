#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Циклы упорядочения systemd в корне системного раздела образа (system_a): systemd-analyze verify default.target
# и sysinit.target, несколько прогонов (систему разрывает цикл, удаляя задание; выбор зависит от порядка обхода и от
# прогона к прогону разный). Код 0 — циклов нет; 1 — найдены (строки «ordering cycle»/«deleted» в выводе).
# Только чтение: раздел монтируется ro,noload под overlay в tmpfs одноразового привилегированного контейнера
# (Linux-хост с Docker, devbox). Генераторы не запускаются (persistent.mount и т. п. не проверяются).
#   tools/image/order_check.sh ОБРАЗ.img.zst [ПРОГОНОВ]
# Найдено в OTA CLOSURE (цикл 2): sheregesh-var-seed Before=systemd-sysusers при After=local-fs.target — цикл; на
# плате systemd удалил задание (в цикле 1 — sysusers, безвредно; в цикле 2 — по-видимому, local-fs.target), слот B
# загрузился без /persistent и служб. verify_image и L3 (файловое испытание) этого не видели.
set -euo pipefail
IMG="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"; RUNS="${2:-8}"
W=$(mktemp -d "$(dirname "$IMG")/.order.XXXXXX"); trap 'rm -rf "$W"' EXIT   # распаковка — рядом с образом
docker run --rm --privileged -e DEBIAN_FRONTEND=noninteractive -e RUNS="$RUNS" -v "$IMG":/img.zst:ro -v "$W":/w \
  debian:trixie bash -euo pipefail -c '
  apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends zstd fdisk systemd >/dev/null
  zstd -qdc /img.zst > /w/img
  read -r start size < <(sfdisk -d /w/img | awk -F"[=,]" "/name=\"system_a\"/{gsub(/ /,\"\"); print \$2, \$4}")
  [ -n "$start" ] && [ -n "$size" ]
  mkdir -p /lo /ov /r && mount -t tmpfs tmpfs /ov && mkdir -p /ov/up /ov/wk
  mount -o ro,noload,loop,offset=$((start*512)),sizelimit=$((size*512)) /w/img /lo
  mount -t overlay overlay -o lowerdir=/lo,upperdir=/ov/up,workdir=/ov/wk /r   # verify пишет служебное в корень
  echo "$(systemd-analyze --version | head -1); прогонов: $RUNS"
  found=0
  for i in $(seq 1 "$RUNS"); do
    out=$(systemd-analyze --root=/r verify --man=no default.target sysinit.target 2>&1 | grep -E "ordering cycle|deleted to break" || true)
    if [ -n "$out" ]; then found=1; printf "%s\n" "$out" | sed "s/^/прогон $i: /"; fi
  done
  umount /r /lo
  if [ "$found" = 0 ]; then echo "PASS: циклов упорядочения нет"; else echo "FAIL: найдены циклы упорядочения"; exit 1; fi
'
