#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Сборка лабораторного образа Raspberry Pi 4B (S2-1, ADR-0002/0006) через rpi-image-gen.
#   os/build.sh [КОНФИГУРАЦИЯ]       sheregesh-lab-pi4 (строгий, по умолчанию) или sheregesh-lab-dev-pi4
# Хост: Debian trixie / Ubuntu 24.04 arm64 (нативно), обычный пользователь с sudo только для install_deps.
# Переменные: SSH_PUBKEY — открытый ключ для dev-варианта (обязателен для него); OUT — каталог результатов
# (build/image/<конфигурация>); SHEREGESH_WORK — рабочий каталог rpi-image-gen; SOURCE_DATE_EPOCH — время сборки
# (по умолчанию — время последнего коммита). Продуктивных ключей сборка не использует и не создаёт.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CFG="${1:-sheregesh-lab-pi4}"
[[ -f "$ROOT/os/config/$CFG.yaml" ]] || { echo "нет конфигурации os/config/$CFG.yaml" >&2; exit 2; }
OUT="${OUT:-$ROOT/build/image/$CFG}"
WORK="${SHEREGESH_WORK:-$ROOT/build/sheregesh-image-work}"
RIG="$WORK/rpi-image-gen"
RIG_REF="$(tr -d '[:space:]' < "$ROOT/os/rpi-image-gen.ref")"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct)}"
mkdir -p "$OUT" "$WORK"

echo "== rpi-image-gen $RIG_REF"
if [[ ! -d "$RIG/.git" ]]; then
  git init -q "$RIG"
  git -C "$RIG" remote add origin https://github.com/raspberrypi/rpi-image-gen.git
fi
git -C "$RIG" fetch -q --depth 1 origin "$RIG_REF"
git -C "$RIG" checkout -q --force FETCH_HEAD
[[ "$(git -C "$RIG" rev-parse HEAD)" == "$RIG_REF" ]] || { echo "rpi-image-gen: не тот коммит" >&2; exit 1; }

echo "== пакет sheregesh"
PKGS="$ROOT/os/layer/sheregesh-lab.d/pkgs"
rm -rf "$PKGS"
python3 -I "$ROOT/tools/image/build_deb.py" --out "$PKGS" | tee "$OUT/deb.txt"

EXTRA=()
if [[ "$CFG" == *-dev-* ]]; then
  [[ -s "${SSH_PUBKEY:-}" ]] || { echo "dev-вариант: задайте SSH_PUBKEY=путь/к/ключу.pub" >&2; exit 2; }
  grep -qE '^(ssh-ed25519|ecdsa-sha2-nistp[0-9]+|ssh-rsa) [A-Za-z0-9+/=]+' "$SSH_PUBKEY" \
    || { echo "SSH_PUBKEY: не похоже на открытый ключ" >&2; exit 2; }
  grep -q "PRIVATE KEY" "$SSH_PUBKEY" && { echo "SSH_PUBKEY: это закрытый ключ" >&2; exit 2; }
  EXTRA=(-- "IGconf_ssh_pubkey_user1=$(realpath "$SSH_PUBKEY")")
fi

echo "== сборка образа $CFG (SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH)"
START=$(date -u +%FT%TZ)
HOST_BUILD_DIR="$WORK/build" "$RIG/rpi-image-gen" build -S "$ROOT/os" -c "$CFG.yaml" "${EXTRA[@]}" \
  2>&1 | tee "$OUT/build.log"
NAME="$(python3 -I -c 'import sys,yaml; d=yaml.safe_load(open(sys.argv[1])); print(d["image"]["name"])' \
        "$ROOT/os/config/$CFG.yaml")"
IMG="$WORK/build/image-$NAME/$NAME.img"
[[ -f "$IMG" ]] || { echo "образ не найден: $IMG" >&2; exit 1; }
DEPLOY="$(ls -d "$WORK"/build/deploy-* | tail -n1)"

echo "== проверка структуры $IMG"
set +e
python3 -I "$ROOT/tools/image/verify_image.py" "$IMG" --variant "$CFG" --out "$OUT"
VERIFY=$?
set -e

echo "== артефакты"
zstd -q -T0 -10 -f "$IMG" -o "$OUT/$NAME.img.zst"
cp "$PKGS"/sheregesh_*_all.deb "$OUT/"
# SBOM и манифест rpi-image-gen — дополнительно к собственному манифесту пакетов.
cp "$DEPLOY"/filesystem-*.sbom.zst "$OUT/$NAME.sbom.zst" 2>/dev/null || true
(cd "$OUT" && sha256sum "$NAME.img.zst" sheregesh_*_all.deb "$NAME.packages.tsv" verify.json $(ls "$NAME.sbom.zst" 2>/dev/null) > SHA256SUMS)
python3 -I - "$OUT" "$CFG" "$NAME" "$RIG_REF" "$START" "$VERIFY" "$IMG" <<'EOF'
import json, os, platform, subprocess, sys, hashlib
out, cfg, name, ref, start, verify, img = sys.argv[1:]
git = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True).stdout.strip()
report = {"config": cfg, "image": name, "rpi_image_gen": ref, "commit": git("rev-parse", "HEAD"),
          "dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
          "source_date_epoch": int(os.environ["SOURCE_DATE_EPOCH"]), "started_utc": start,
          "host": f"{platform.system()} {platform.release()} {platform.machine()}",
          "image_bytes": os.path.getsize(img),
          "image_sha256": hashlib.sha256(open(img, "rb").read()).hexdigest(),
          "verify_exit": int(verify), "verify": json.load(open(os.path.join(out, "verify.json"))),
          "note": "Сборка образа не равна загрузке Pi: tryboot, A/B-откат, дисплей и производительность — NOT TESTED."}
json.dump(report, open(os.path.join(out, "build-report.json"), "w"), ensure_ascii=False, indent=1)
print(json.dumps({k: v for k, v in report.items() if k != "verify"}, ensure_ascii=False, indent=1))
EOF
exit "$VERIFY"
