#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# Видеоконвейеры SRTP (selftest impair --via=direct) под настоящим tc netem — запуск НА devbox, не на Mac.
#   bash run.sh SRC_DIR OUT_DIR [ПРОФИЛЬ...]   (JB=мс — задержка джиттер-буфера rx, по умолчанию 20)
# SRC_DIR — дерево репозитория (git archive), OUT_DIR — JSON по профилю. netem — на lo своего сетевого пространства
# контейнера (--cap-add NET_ADMIN): на хост и другие контейнеры не влияет. Профили — PROFILES ниже.
set -euo pipefail
SRC=$(cd "$1" && pwd); OUT=$(mkdir -p "$2" && cd "$2" && pwd)
IMG=sheregesh-netem-video
docker build -q -t "$IMG" "$(dirname "$0")" >/dev/null
PROFILES=(
  "clean|"
  "loss1-reorder5|delay 5ms 2ms loss 1% reorder 5% 50%"
  "loss3-reorder10-dup2|delay 5ms 2ms loss 3% reorder 10% 50% duplicate 2%"
  "burst-loss|delay 5ms loss gemodel 1% 20% 70% 0.1%"
  "jitter20|delay 20ms 10ms distribution normal"
  "loss5|delay 5ms loss 5%"
)
JB=${JB:-20}
shift 2
for p in "${PROFILES[@]}"; do
  name=${p%%|*}; nem=${p#*|}
  if [ $# -gt 0 ] && [[ " $* " != *" $name "* ]]; then continue; fi
  docker run --rm --cap-add NET_ADMIN -v "$SRC:/src:ro" -e PYTHONPATH=/src/software "$IMG" bash -c "
    set -e; [ -n '$nem' ] && tc qdisc add dev lo root netem $nem
    tc qdisc show dev lo | head -1 >&2
    timeout 60 python3 -W ignore -m sheregesh.videosvc.pipelines selftest impair --via=direct --seconds=20 --jb=$JB" \
    2>"$OUT/$name-jb$JB.err" | tail -1 > "$OUT/$name-jb$JB.json"
  echo "$name jb=$JB: $(head -c 120 "$OUT/$name-jb$JB.json")"
done
