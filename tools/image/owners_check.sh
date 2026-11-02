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
# SHBOOT_RAW — образ задания, уже распакованный сервисом (разреженно, один раз на все этапы); образ инструментов
# shbuild-qemu (zstd, fdisk, systemd, ...) собран заранее — без apt-get на каждый запуск. Нет того или другого —
# как раньше: чистый debian:trixie + распаковка.
RAWARGS=(); [ -n "${SHBOOT_RAW:-}" ] && [ -s "$SHBOOT_RAW" ] && RAWARGS=(-v "$SHBOOT_RAW":/img.raw:ro)
TOOLS=debian:trixie; docker image inspect shbuild-qemu >/dev/null 2>&1 && TOOLS=shbuild-qemu
docker run --rm --privileged -e DEBIAN_FRONTEND=noninteractive -v "$IMG":/img.zst:ro -v "$W":/w ${RAWARGS[@]+"${RAWARGS[@]}"} "$TOOLS" bash -euo pipefail -c '
  command -v sfdisk >/dev/null && command -v zstd >/dev/null || { apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends zstd fdisk >/dev/null; }
  if [ -e /img.raw ]; then F=/img.raw; else zstd -qdf --sparse /img.zst -o /w/img; F=/w/img; fi
  read -r start size < <(sfdisk -d $F | awk -F"[=,]" "/name=\"system_a\"/{gsub(/ /,\"\"); print \$2, \$4}")
  [ -n "$start" ] && [ -n "$size" ]
  mkdir /mnt/s && mount -o ro,noload,loop,offset=$((start*512)),sizelimit=$((size*512)) $F /mnt/s
  cd /mnt/s && find . -xdev \( -path ./home -prune \) -o \( ! -uid 0 -o ! -gid 0 \) -printf "%U:%G %p\n" | LC_ALL=C sort
  cd / && umount /mnt/s
'
