#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Готовый контейнер-сборщик образа (ADR-0018, ускорение): debian:trixie arm64 с пакетами os/docker-build.sh и
# зависимостями rpi-image-gen указанной ревизии. Делается один раз на ревизию rpi-image-gen (docker commit), дальше
# сборка не ставит пакеты под эмуляцией. Клон rpi-image-gen и кэш apt — из постоянного тома sheregesh-image-work.
#   make_builder.sh РЕВИЗИЯ_RPI_IMAGE_GEN   → печатает тег образа (shbuild-builder:<12 знаков ревизии>)
set -euo pipefail
REF=${1:?ревизия rpi-image-gen}; [[ "$REF" =~ ^[0-9a-f]{40}$ ]] || { echo "ревизия — 40 hex" >&2; exit 2; }
TAG=shbuild-builder:${REF:0:12}
if docker image inspect "$TAG" >/dev/null 2>&1; then echo "$TAG"; exit 0; fi
NAME=shbuild-mkbuilder-$$
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT
docker volume create sheregesh-image-work >/dev/null
docker run --name "$NAME" --platform linux/arm64 -e REF="$REF" -v sheregesh-image-work:/work debian:trixie bash -euo pipefail -c '
  export DEBIAN_FRONTEND=noninteractive
  mkdir -p /work/cache/apt/partial
  rm -f /etc/apt/apt.conf.d/docker-clean
  echo "Dir::Cache::archives \"/work/cache/apt\";" > /etc/apt/apt.conf.d/90-sheregesh-cache
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends sudo git ca-certificates curl python3 python3-yaml qt6-l10n-tools zstd rsync >/dev/null
  git config --global --add safe.directory "*"
  if [ ! -x /work/rpi-image-gen/rpi-image-gen ]; then git clone -q https://github.com/raspberrypi/rpi-image-gen.git /work/rpi-image-gen; fi
  git -C /work/rpi-image-gen cat-file -e "$REF^{commit}" 2>/dev/null || git -C /work/rpi-image-gen fetch -q --depth 1 origin "$REF"
  git -C /work/rpi-image-gen checkout -q --force "$REF"
  /work/rpi-image-gen/install_deps.sh >/dev/null
  echo "$REF" > /etc/sheregesh-builder' >&2
docker commit "$NAME" "$TAG" >/dev/null
echo "$TAG"
