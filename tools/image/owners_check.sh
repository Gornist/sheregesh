#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Владельцы файлов системного раздела образа image-rota (system_a): все пути вне /home с uid или gid ≠ 0, в виде
# «uid:gid путь», отсортированно. Сравнение с эталоном (образ CI) — diff двух списков. Только чтение: раздел
# монтируется ro,noload в одноразовом привилегированном контейнере на Linux-хосте с Docker (devbox).
#   tools/image/owners_check.sh ОБРАЗ.img.zst > owners.txt
# Найдено в OTA CLOSURE (цикл 1): сборка от root без userns дала uid 1000 на /etc, /usr и юнитах; verify_image это
# не проверял.
set -euo pipefail
IMG="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
W=$(mktemp -d "$(dirname "$IMG")/.owners.XXXXXX"); trap 'rm -rf "$W"' EXIT   # распаковка — рядом с образом, не в слое контейнера
docker run --rm --privileged -e DEBIAN_FRONTEND=noninteractive -v "$IMG":/img.zst:ro -v "$W":/w debian:trixie bash -euo pipefail -c '
  apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends zstd fdisk >/dev/null
  zstd -qdc /img.zst > /w/img
  read -r start size < <(sfdisk -d /w/img | awk -F"[=,]" "/name=\"system_a\"/{gsub(/ /,\"\"); print \$2, \$4}")
  [ -n "$start" ] && [ -n "$size" ]
  mkdir /mnt/s && mount -o ro,noload,loop,offset=$((start*512)),sizelimit=$((size*512)) /w/img /mnt/s
  cd /mnt/s && find . -xdev \( -path ./home -prune \) -o \( ! -uid 0 -o ! -gid 0 \) -printf "%U:%G %p\n" | LC_ALL=C sort
  cd / && umount /mnt/s
'
