#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Сборка лабораторного образа Raspberry Pi 4B (S2-1, ADR-0002/0006) через rpi-image-gen.
#   os/build.sh [КОНФИГУРАЦИЯ]       sheregesh-lab-pi4 (строгий, по умолчанию) или sheregesh-lab-dev-pi4
# Хост: Debian trixie / Ubuntu 24.04 arm64 (нативно), обычный пользователь с sudo только для install_deps.
# Переменные: SSH_PUBKEY — открытый ключ для dev-варианта (обязателен для него); DEPLOY_PUBKEY — открытый ключ
# подписи выпусков DEV deploy/OTA (S3.1-DEPLOY; обязателен для dev, отличен от SSH_PUBKEY); OUT — каталог результатов
# (build/image/<конфигурация>); SHEREGESH_WORK — рабочий каталог rpi-image-gen; SOURCE_DATE_EPOCH — время сборки
# (по умолчанию — время последнего коммита); SHEREGESH_POST_ONLY=1 — только проверка и артефакты уже собранного
# образа (отладка постобработки). Продуктивных ключей сборка не использует и не создаёт.
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

# Ключи dev-варианта — до любых загрузок и сборки: отказ сразу.
EXTRA=()
if [[ "$CFG" == *-dev-* ]]; then
  [[ -s "${SSH_PUBKEY:-}" ]] || { echo "dev-вариант: задайте SSH_PUBKEY=путь/к/ключу.pub" >&2; exit 2; }
  grep -qE '^(ssh-ed25519|ecdsa-sha2-nistp[0-9]+|ssh-rsa) [A-Za-z0-9+/=]+' "$SSH_PUBKEY" \
    || { echo "SSH_PUBKEY: не похоже на открытый ключ" >&2; exit 2; }
  grep -q "PRIVATE KEY" "$SSH_PUBKEY" && { echo "SSH_PUBKEY: это закрытый ключ" >&2; exit 2; }
  EXTRA=(-- "IGconf_ssh_pubkey_user1=$(realpath "$SSH_PUBKEY")")
  # S3.1-DEPLOY: открытый ключ подписи выпусков (DEV deploy, OTA) — отдельный от ключа входа: кража ключа входа
  # не даёт установить код. Подстановки ключа входа нет: без DEPLOY_PUBKEY или при совпадении — отказ сборки.
  SIGNER="${DEPLOY_PUBKEY:-}"
  [[ -n "$SIGNER" && -s "$SIGNER" ]] || { echo "DEPLOY_PUBKEY: для dev-варианта обязателен (файл открытого ключа подписи)" >&2; exit 2; }
  grep -q "PRIVATE KEY" "$SIGNER" && { echo "DEPLOY_PUBKEY: это закрытый ключ" >&2; exit 2; }
  read -r ktype kbody _ < "$SIGNER"
  [[ "$ktype" =~ ^(ssh-ed25519|ecdsa-sha2-nistp[0-9]+|sk-ssh-ed25519@openssh.com|ssh-rsa)$ && -n "$kbody" ]] \
    || { echo "DEPLOY_PUBKEY: не похоже на открытый ключ" >&2; exit 2; }
  read -r _ login_body _ < "$SSH_PUBKEY"
  [[ "$kbody" != "$login_body" ]] || { echo "DEPLOY_PUBKEY совпадает с ключом входа SSH_PUBKEY — нужен отдельный ключ подписи" >&2; exit 2; }
  SIGNERS="$ROOT/os/layer/sheregesh-dev-access.d/deploy/allowed_signers"
  mkdir -p "$(dirname "$SIGNERS")"
  printf 'sheregesh-deploy namespaces="sheregesh-deploy,sheregesh-ota,sheregesh-ota-enable" %s %s\n' \
    "$ktype" "$kbody" > "$SIGNERS"
  # S5 п. 7: лабораторные источники видео pi2 (vivid, USB/IP эмулятора shcam) — только dev-слой. Состав и права —
  # tools/lab-video/pi2/manifest.tsv (его же проверяет verify_image), файлы копируются рядом с манифестом.
  LABV="$ROOT/os/layer/sheregesh-dev-access.d/lab-video"
  rm -rf "$LABV" && mkdir -p "$LABV"
  grep -v '^#' "$ROOT/tools/lab-video/pi2/manifest.tsv" | while IFS=$'\t' read -r src _dest _mode; do
    if [[ -n "$src" ]]; then install -D -m 0644 "$ROOT/tools/lab-video/$src" "$LABV/$src"; fi
  done
  cp "$ROOT/tools/lab-video/pi2/manifest.tsv" "$LABV/manifest.tsv"
fi

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


echo "== сборка образа $CFG (SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH)"
START=$(date -u +%FT%TZ)
if [[ -z "${SHEREGESH_POST_ONLY:-}" ]]; then
  HOST_BUILD_DIR="$WORK/build" "$RIG/rpi-image-gen" build -S "$ROOT/os" -c "$CFG.yaml" ${EXTRA[@]+"${EXTRA[@]}"} \
    2>&1 | tee "$OUT/build.log"
fi
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
python3 -I - "$OUT" "$CFG" "$NAME" "$RIG_REF" "$START" "$VERIFY" "$IMG" "$ROOT" <<'EOF'
import json, os, platform, subprocess, sys, hashlib
out, cfg, name, ref, start, verify, img, root = sys.argv[1:]


def sha256(path):                          # потоково: образ 7 ГБ не помещается в память контейнера
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

git = lambda *a: subprocess.run(["git", "-C", root, *a], capture_output=True, text=True).stdout.strip()
report = {"config": cfg, "image": name, "rpi_image_gen": ref, "commit": git("rev-parse", "HEAD"),
          "dirty": bool(git("status", "--porcelain", "--untracked-files=no")), "post_only": bool(os.environ.get("SHEREGESH_POST_ONLY")),
          "source_date_epoch": int(os.environ["SOURCE_DATE_EPOCH"]), "started_utc": start,
          "host": f"{platform.system()} {platform.release()} {platform.machine()}",
          "image_bytes": os.path.getsize(img),
          "image_sha256": sha256(img),
          "verify_exit": int(verify), "verify": json.load(open(os.path.join(out, "verify.json"))),
          "note": "Сборка образа не равна загрузке Pi: tryboot, A/B-откат, дисплей и производительность — NOT TESTED."}
json.dump(report, open(os.path.join(out, "build-report.json"), "w"), ensure_ascii=False, indent=1)
print(json.dumps({k: v for k, v in report.items() if k != "verify"}, ensure_ascii=False, indent=1))
EOF
exit "$VERIFY"
