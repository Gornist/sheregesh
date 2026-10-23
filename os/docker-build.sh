#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Сборка образа на машине разработчика (macOS/Linux, Docker с arm64): привилегированный контейнер debian:trixie,
# rpi-image-gen запускается от непривилегированного пользователя. Рабочий каталог — том Docker (не файловая
# система хоста), результаты — build/image/<конфигурация>.
#   os/docker-build.sh [КОНФИГУРАЦИЯ]       SSH_PUBKEY=~/.ssh/id_ed25519.pub os/docker-build.sh sheregesh-lab-dev-pi4
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CFG="${1:-sheregesh-lab-pi4}"
KEYARGS=()
if [[ -n "${SSH_PUBKEY:-}" ]]; then
  KEYARGS=(-v "$(cd "$(dirname "$SSH_PUBKEY")" && pwd)/$(basename "$SSH_PUBKEY"):/keys/user1.pub:ro" -e SSH_PUBKEY=/keys/user1.pub)
fi
docker volume create sheregesh-image-work >/dev/null
exec docker run --rm --privileged --platform linux/arm64 \
  -v "$ROOT":/src:ro -v "$ROOT/build/image":/out -v sheregesh-image-work:/work "${KEYARGS[@]}" \
  -e CFG="$CFG" -e SOURCE_DATE_EPOCH="$(git -C "$ROOT" log -1 --format=%ct)" \
  debian:trixie bash -euo pipefail -c '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq --no-install-recommends sudo git ca-certificates python3 python3-yaml \
      qt6-l10n-tools zstd rsync >/dev/null
    id builder >/dev/null 2>&1 || useradd -m -s /bin/bash builder
    echo "builder ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/builder
    chown builder /work
    # Копия рабочего дерева внутри контейнера: rpi-image-gen пишет рядом с исходниками (pkgs/), /src — только чтение.
    rsync -a --delete --exclude build --exclude .venv --exclude reports /src/ /work/src/
    chown -R builder /work/src
    sudo -u builder git config --global --add safe.directory "*"
    mkdir -p /work/rpi-image-gen && chown builder /work/rpi-image-gen
    if [ ! -x /work/rpi-image-gen/rpi-image-gen ]; then
      sudo -u builder git clone -q https://github.com/raspberrypi/rpi-image-gen.git /work/rpi-image-gen
    fi
    sudo -u builder git -C /work/rpi-image-gen checkout -q "$(cat /work/src/os/rpi-image-gen.ref)"
    /work/rpi-image-gen/install_deps.sh >/dev/null
    mkdir -p /out/$CFG && chown builder /out/$CFG
    sudo -u builder env SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" SSH_PUBKEY="${SSH_PUBKEY:-}" \
      SHEREGESH_WORK=/work OUT=/out/$CFG /work/src/os/build.sh "$CFG"
  '
