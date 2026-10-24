#!/bin/bash
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# Запуск испытания app-stop на сборочном хосте с docker (не на платах): контейнер debian:trixie с systemd как PID 1.
#   tests/systemd/app_stop/host.sh [ssh-хост]   (по умолчанию devbox-lan)
# Рабочий каталог — /srv/build/appstop-<метка>, контейнер и образ с той же меткой; всё удаляется по выходу.
set -euo pipefail
HOST=${1:-devbox-lan}
ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
TAG=appstop-$(date +%Y%m%d%H%M%S)-$$
W=/srv/build/$TAG
echo "# хост $HOST, каталог $W, коммит $(git -C "$ROOT" rev-parse --short HEAD)"
S=$(mktemp -d); trap 'rm -rf "$S"' EXIT
mkdir -p "$S/repo/software/sheregesh" "$S/repo/tests/unit" "$S/repo/tools/image/units" "$S/harness"
cp "$ROOT"/software/sheregesh/{__init__,sdnotify,notify_relay}.py "$S/repo/software/sheregesh/"
cp "$ROOT"/tests/unit/test_notify_relay{,_stop}.py "$S/repo/tests/unit/"
cp "$ROOT/tools/image/units/sheregesh-console.service" "$S/repo/tools/image/units/"
cp "$ROOT"/tests/systemd/app_stop/{Dockerfile,fake_cage.sh,fake_app.py,make_units.py,run.sh} "$S/harness/"
X=(); tar --version | grep -q bsdtar && X=(--no-xattrs --no-mac-metadata)
COPYFILE_DISABLE=1 tar ${X[@]+"${X[@]}"} -C "$S" -cf - . | ssh "$HOST" "mkdir -p $W/stage && tar -C $W/stage -xf - && chmod -R a+rX $W/stage"
# shellcheck disable=SC2029
ssh "$HOST" "set -e; trap 'docker rm -f $TAG >/dev/null 2>&1; docker rmi $TAG >/dev/null 2>&1; rm -rf $W' EXIT
  docker build -q -t $TAG $W/stage/harness >/dev/null
  docker run -d --name $TAG --privileged --cgroupns=private --tmpfs /run --tmpfs /run/lock \
    -v $W/stage:/stage:ro $TAG >/dev/null
  docker exec $TAG bash /stage/harness/run.sh"
