#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Сборка образа на машине разработчика (macOS/Linux, Docker с arm64): привилегированный контейнер debian:trixie,
# rpi-image-gen запускается от непривилегированного пользователя. Рабочий каталог — том Docker (не файловая
# система хоста), результаты — build/image/<конфигурация>.
#   os/docker-build.sh [КОНФИГУРАЦИЯ]
#   SSH_PUBKEY=dev.pub DEPLOY_PUBKEY=deploy.pub os/docker-build.sh sheregesh-lab-dev-pi4   (оба — открытые ключи)
# Из git worktree (.git — файл, ссылка на чужой каталог): метаданные git передаются в контейнер пакетом
# (git bundle HEAD), рабочие файлы — как есть (rsync). build_deb.py (git ls-files, версия) и build-report.json
# (commit, dirty) видят тот же коммит, что и в обычном клоне.
# SHEREGESH_SRC=каталог — собрать другое дерево (например, чистый checkout нужного коммита) этим же сценарием.
# Сборочный хост без клона (devbox): SHEREGESH_SRC=распакованный git archive КОММИТ, SHEREGESH_BUNDLE=пакет git
# с этим коммитом, SOURCE_DATE_EPOCH=время коммита (git log -1 --format=%ct КОММИТ). На хосте x86_64 нужен
# зарегистрированный qemu-aarch64 (binfmt, флаг F) — контейнер arm64 исполняется в эмуляции. qemu-user не умеет
# clone(CLONE_NEWUSER) (podman unshare → «cannot clone: Invalid argument»), поэтому там SHEREGESH_BUILD_AS_ROOT=1:
# rpi-image-gen работает от root одноразового привилегированного контейнера, bin/ns исполняет команды без unshare.
set -euo pipefail
ROOT="$(cd "${SHEREGESH_SRC:-$(dirname "${BASH_SOURCE[0]}")/..}" && pwd)"
CFG="${1:-sheregesh-lab-pi4}"
KEYARGS=()
if [[ -n "${SSH_PUBKEY:-}" ]]; then
  KEYARGS=(-v "$(cd "$(dirname "$SSH_PUBKEY")" && pwd)/$(basename "$SSH_PUBKEY"):/keys/user1.pub:ro" -e SSH_PUBKEY=/keys/user1.pub)
fi
if [[ -n "${DEPLOY_PUBKEY:-}" ]]; then
  KEYARGS+=(-v "$(cd "$(dirname "$DEPLOY_PUBKEY")" && pwd)/$(basename "$DEPLOY_PUBKEY"):/keys/deploy.pub:ro" -e DEPLOY_PUBKEY=/keys/deploy.pub)
fi
mkdir -p "$ROOT/build/image"
rm -f "$ROOT/build/image/.src.bundle"
if [[ -n "${SHEREGESH_BUNDLE:-}" ]]; then        # дерево без .git (git archive) + пакет git того же коммита
  cp "$SHEREGESH_BUNDLE" "$ROOT/build/image/.src.bundle"
  : "${SOURCE_DATE_EPOCH:?с SHEREGESH_BUNDLE задайте SOURCE_DATE_EPOCH (время коммита)}"
elif [[ -f "$ROOT/.git" ]]; then
  git -C "$ROOT" bundle create -q "$ROOT/build/image/.src.bundle" HEAD
fi
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct)}"
docker volume create sheregesh-image-work >/dev/null
exec docker run --rm --privileged --platform linux/arm64 \
  -v "$ROOT":/src:ro -v "$ROOT/build/image":/out -v sheregesh-image-work:/work ${KEYARGS[@]+"${KEYARGS[@]}"} \
  -e CFG="$CFG" -e SHEREGESH_POST_ONLY="${SHEREGESH_POST_ONLY:-}" -e SHEREGESH_BUILD_AS_ROOT="${SHEREGESH_BUILD_AS_ROOT:-}" -e SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
  debian:trixie bash -euo pipefail -c '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq --no-install-recommends sudo git ca-certificates python3 python3-yaml \
      qt6-l10n-tools zstd rsync >/dev/null
    id builder >/dev/null 2>&1 || useradd -m -s /bin/bash builder
    echo "builder ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/builder
    chown builder /work
    # Копия рабочего дерева внутри контейнера: rpi-image-gen пишет рядом с исходниками (pkgs/), /src — только чтение.
    rsync -a --delete --exclude build --exclude .venv --exclude reports /src/ /work/src/
    git config --global --add safe.directory "*"   # /work/src — владелец с хоста (rsync -a), git от root иначе отказывается
    if [ -f /out/.src.bundle ]; then
      rm -rf /work/src/.git
      git -C /work/src init -q
      set -- $(git bundle list-heads /out/.src.bundle | head -n1)
      git -C /work/src fetch -q /out/.src.bundle "$2"
      git -C /work/src update-ref --no-deref HEAD "$1"
      git -C /work/src reset -q
    fi
    chown -R builder /work/src
    sudo -u builder git config --global --add safe.directory "*"
    mkdir -p /work/rpi-image-gen && chown builder /work/rpi-image-gen
    if [ ! -x /work/rpi-image-gen/rpi-image-gen ]; then
      sudo -u builder git clone -q https://github.com/raspberrypi/rpi-image-gen.git /work/rpi-image-gen
    fi
    RIG_REF="$(tr -d "[:space:]" < /work/src/os/rpi-image-gen.ref)"
    sudo -u builder git -C /work/rpi-image-gen cat-file -e "$RIG_REF^{commit}" 2>/dev/null \
      || sudo -u builder git -C /work/rpi-image-gen fetch -q --depth 1 origin "$RIG_REF"
    sudo -u builder git -C /work/rpi-image-gen checkout -q --force "$RIG_REF"
    /work/rpi-image-gen/install_deps.sh >/dev/null
    mkdir -p /out/$CFG && chown builder /out/$CFG
    AS="sudo -u builder"; [ -n "$SHEREGESH_BUILD_AS_ROOT" ] && AS=""
    $AS env SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" SSH_PUBKEY="${SSH_PUBKEY:-}" DEPLOY_PUBKEY="${DEPLOY_PUBKEY:-}" SHEREGESH_POST_ONLY="$SHEREGESH_POST_ONLY" \
      SHEREGESH_WORK=/work OUT=/out/$CFG /work/src/os/build.sh "$CFG"
  '
