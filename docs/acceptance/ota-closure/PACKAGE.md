# OTA CLOSURE — пакет кандидата и файловое испытание L3 (процедура)

Кандидат — `abd9d68e4697c2d951898e6011d256e98bf38417` (`git merge-base --is-ancestor` для 6353c70 и d1258f3 → 0; `software/` и `tools/deploy/` в `v2` после него не менялись). Образ `sheregesh-lab-dev-pi4` собирается на devbox (`SOURCE_DATE_EPOCH=1792880268` → версия OTA `0.1.1792880268`). Всё ниже выполняется на Mac, без плат. Закрытый ключ `~/.ssh/sheregesh-deploy` используется только локально (`ssh-keygen -Y sign` внутри `shdeploy`/`run.py`), никуда не копируется и не выводится. Любой код ≠ 0 или несовпадение — **STOP**, без исправлений кода.

Журналы и результаты — `docs/acceptance/ota-closure/build/` (коммит в ветке `pkg/ota-closure`). Образы и пакеты в Git не попадают (`/build/` в `.gitignore`).

## 0. Переменные и предусловия

```sh
REPO=/Users/peachbite/Developer/sheregesh/.claude/worktrees/agent-a2e57e664205fe855   # чистое дерево ветки от v2
C=abd9d68e4697c2d951898e6011d256e98bf38417
SRC=devbox-lan:/srv/build/sheregesh-image/src/build/image/sheregesh-lab-dev-pi4/      # OUT=/out/$CFG, /out=$ROOT/build/image
ART=/Users/peachbite/Developer/sheregesh/build/image/devbox-abd9d68-dev
BASE=/Users/peachbite/Developer/sheregesh/build/image/ci-57e8fe4-dev/sheregesh-lab-dev-pi4.img.zst
LOG=$REPO/docs/acceptance/ota-closure/build
P1=$ART/ota-1/sheregesh-ota_sheregesh-lab-dev-pi4_0.1.1792880268.shota
P2=$ART/ota-2/sheregesh-ota_sheregesh-lab-dev-pi4_0.1.1792880268.shota
mkdir -p "$ART" "$LOG"

# дерево чистое, код агента и инструментов = кандидату (режим «агент кандидата» копирует $REPO/software)
test -z "$(git -C "$REPO" status --porcelain -- software tools)" && git -C "$REPO" diff --quiet "$C" -- software tools/deploy
# база 57e8fe4 (образ на слоте A обеих плат)
(cd "$(dirname "$BASE")" && shasum -a 256 -c SHA256SUMS)
# ключ подписи образа = ключ плат = ключ Mac (только открытые ключи)
ssh-keygen -lf ~/.ssh/sheregesh-deploy.pub                                  # SHA256:5wJfw7QwUm7iVV+NCXlvgxqxgvXx5qLPjpDwoRlOZUw
ssh devbox-lan ssh-keygen -lf /srv/build/sheregesh-image/in/deploy.pub     # тот же отпечаток
```

Сборка готова, когда на devbox есть `SHA256SUMS` и `build-report.json` (пишутся последними) и нет процесса `os/build.sh`:
```sh
ssh devbox-lan 'ls -la /srv/build/sheregesh-image/src/build/image/sheregesh-lab-dev-pi4/; pgrep -f "[o]s/build.sh" || echo done'
```

Место на Mac: артефакты ≈ 0,85 ГБ, два пакета ≈ 2 × 0,33 ГБ, временно ≈ 7 ГБ в `$TMPDIR` на каждую сборку пакета (распакованный образ). На devbox L3 занимает `/srv/build` (≈ 8 ГБ на прогон, удаляется после).

## 1. Копирование образа с проверкой SHA256SUMS

```sh
rsync -a -P "$SRC" "$ART/"            # -P = --partial --progress: прерванная передача возобновляется повтором той же команды
(cd "$ART" && shasum -a 256 -c SHA256SUMS) | tee "$LOG/sha256sums-check.txt"   # все строки OK, иначе повторить rsync
ssh devbox-lan "cd ${SRC#devbox-lan:} && sha256sum -c SHA256SUMS" >> "$LOG/sha256sums-check.txt"
cp "$ART/SHA256SUMS" "$LOG/SHA256SUMS"
```

Нужны: `sheregesh-lab-dev-pi4.img.zst`, `SHA256SUMS`, `build-report.json`, `verify.json`, `sheregesh_0.1.0~dev0+gabd9d68_all.deb` (`ota-build` берёт юниты из .deb и проверку из `verify.json` рядом с `build-report.json`).

## 2. Проверка build-report и verify

```sh
python3 -I - "$ART" "$C" <<'EOF' | tee "$LOG/build-report-check.txt"
import json, sys, pathlib
art, c = pathlib.Path(sys.argv[1]), sys.argv[2]
r = json.loads((art / "build-report.json").read_text()); v = json.loads((art / "verify.json").read_text())
fails = [x["id"] for x in v["checks"] if x["status"] != "PASS"]
build = next(x for x in v["checks"] if x["id"] == "deploy.build")["detail"]
ok = {"commit": r["commit"] == c, "dirty=false": r["dirty"] is False, "post_only=false": r["post_only"] is False,
      "config dev": r["config"] == "sheregesh-lab-dev-pi4" and v["variant"] == "sheregesh-lab-dev-pi4",
      "ota_version > 0.1.1792865618": (0, 1, int(r["source_date_epoch"])) > (0, 1, 1792865618),
      "verify_exit=0": r["verify_exit"] == 0, "verify без FAIL": not fails and v["verdict"] == "PASS",
      "verify в отчёте = verify.json": r["verify"] == v, "build.json образа = коммит": c in build,
      "deb кандидата": len(list(art.glob("sheregesh_*+gabd9d68_all.deb"))) == 1}
print(json.dumps({"source_date_epoch": r["source_date_epoch"], "image_sha256": r["image_sha256"],
                  "verify": v["counts"], "failed": fails, **ok}, ensure_ascii=False, indent=1))
sys.exit(0 if all(ok.values()) else 1)
EOF
```

`compatible` dev (`sheregesh-lab-pi4-dev-1`) задаётся вариантом и проверяется в манифесте (§5).

## 3. Пакет дважды (воспроизводимость)

```sh
cd "$REPO"
python3 tools/deploy/shdeploy.py ota-build --image "$ART/sheregesh-lab-dev-pi4.img.zst" \
  --build-report "$ART/build-report.json" --out "$ART/ota-1" | tee "$LOG/ota-build-1.log"
python3 tools/deploy/shdeploy.py ota-build --image "$ART/sheregesh-lab-dev-pi4.img.zst" \
  --build-report "$ART/build-report.json" --out "$ART/ota-2" | tee "$LOG/ota-build-2.log"
shasum -a 256 "$P1" "$P2" | tee "$LOG/payload-sha256.txt"
cmp "$P1" "$P2" && cmp "$P1.manifest.json" "$P2.manifest.json" && echo "REPRODUCIBLE" | tee -a "$LOG/payload-sha256.txt"
cp "$P1.manifest.json" "$LOG/ota-payload-manifest.json"
rm -rf "$ART/ota-2"                    # идентичен ota-1
```

`created` и версия — из `source_date_epoch`, xz — фиксированные потоки и блоки, подпись ed25519 детерминирована ⇒ одинаковый SHA-256. SHA-256 не равен `f6e192346705c21c19a394da09f2e7d221d02c176adc02a3e727f0f1e8046edb` (a7f1b74).

## 4. Подпись по allowed_signers плат

```sh
python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1] + "/software"); from sheregesh.deploy import sig
sys.stdout.write(sig.allowed_signers_line(open(sys.argv[2]).read()))' "$REPO" ~/.ssh/sheregesh-deploy.pub > "$ART/allowed_signers"
shasum -a 256 "$ART/allowed_signers"   # = 74c9561082ab15a30146f39465f76ae8e29508d64a4752c891dccf7ae33600c0 (файл на pi1/pi2), иначе STOP
python3 "$REPO/docs/acceptance/s3.2-ota/pi1-preflight/tools/verify_payload.py" "$REPO" "$P1" "$ART/allowed_signers" \
  sheregesh-lab-pi4-dev-1 0.1.1792843040 | tee "$LOG/payload-check.json"
```

Ожидается: `signer` = `Good "sheregesh-ota" signature for sheregesh-deploy with ED25519 key SHA256:5wJfw7Qw…lOZUw`, `compressed_sha256_ok` true для boot и system, `trailing_bytes` 0, `version_gt_active` true (активная на платах `0.1.1792843040`), `verdict` PASS. Проверка — кодом агента репозитория (`ota.Payload`: подпись до чтения образов).

## 5. Сверка манифеста

```sh
python3 -I - "$P1.manifest.json" "$ART/build-report.json" "$C" <<'EOF' | tee "$LOG/manifest-check.txt"
import json, sys
m, r, c = json.load(open(sys.argv[1])), json.load(open(sys.argv[2])), sys.argv[3]
ok = {"commit": m["commit"] == c, "version": m["version"] == f"0.1.{r['source_date_epoch']}" == "0.1.1792880268",
      "version > a7f1b74 > активной": tuple(map(int, m["version"].split("."))) > (0, 1, 1792865618) > (0, 1, 1792843040),
      "compatible": m["compatible"] == "sheregesh-lab-pi4-dev-1", "layout": m["layout"] == "image-rota-gpt-ab-1",
      "kind": m["kind"] == "sheregesh-system-update", "variant": m["variant"] == "sheregesh-lab-dev-pi4",
      "image_sha256 = build-report": m["image_sha256"] == r["image_sha256"],
      "image_verify.failed = []": m["image_verify"]["failed"] == [] and m["image_verify"]["checks"] == len(r["verify"]["checks"]),
      "images boot+system": [i["part"] for i in m["images"]] == ["boot", "system"],
      "package кандидата": m["package"].endswith("+gabd9d68_all.deb"),
      "units": {"sheregesh-ota-boot.service", "sheregesh-ota.socket", "sheregesh-var-seed.service"} <= set(m["units"])}
print(json.dumps(ok, ensure_ascii=False, indent=1)); sys.exit(0 if all(ok.values()) else 1)
EOF
```

## 6. L3 на файловом образе — два режима (последовательно)

База — образ 57e8fe4 (§0). Хост `devbox-lan`, контейнер `nice -n 19`; запускать **после** окончания сборки образа. Разные `--remote`, чтобы прогоны не делили каталог.

```sh
cd "$REPO"
# (а) агент 57e8fe4 — он выполнит установку на pi2 и pi1
python3 tools/deploy/ota-check/run.py --base "$BASE" --payload "$P1" --agent-rev 57e8fe4 --docker-host devbox-lan \
  --remote /srv/build/ota-closure-l3-57e8fe4 --log "$LOG/l3-agent-57e8fe4.log" --result "$LOG/l3-agent-57e8fe4.json"; echo "rc=$?"
# (б) агент кандидата (код $REPO/software = abd9d68)
python3 tools/deploy/ota-check/run.py --base "$BASE" --payload "$P1" --docker-host devbox-lan \
  --remote /srv/build/ota-closure-l3-candidate --log "$LOG/l3-agent-candidate.log" --result "$LOG/l3-agent-candidate.json"; echo "rc=$?"
for f in "$LOG"/l3-agent-*.json; do python3 -I - "$f" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1])); res = {x["name"]: x["status"] for x in d["results"]}
need = ["новый слот: confirm — health с профилем пользователя стенда, не /root", "новый слот: build.json = коммит и версия манифеста",
        "чужая подпись: REJECTED", "чужая подпись: слот B не тронут", "обрезанный поток: A не тронут",
        "повторная установка в TRYBOOT: REJECTED", "abort: OK, основной слот A", "в конце общие данные не изменились"]
bad = [n for n in need if res.get(n) != "PASS"]
print(sys.argv[1], d["agent"], f'{d["pass"]}/{d["pass"] + d["fail"]}', "обязательные:", "PASS" if not bad else bad)
sys.exit(0 if d["fail"] == 0 and d["pass"] == 39 and not bad else 1)
EOF
done | tee "$LOG/l3-summary.txt"
```

Ожидание: **39/39 PASS** в обоих режимах (a7f1b74: 38/39, единственный FAIL — health с профилем стенда, исправлено в 6353c70). `payload_sha256` в обоих `result.json` = SHA из §3.

## 7. Итог

`git add docs/acceptance/ota-closure/{PACKAGE.md,build}` и коммит в `pkg/ota-closure`. В отчёт: путь и SHA-256 образа (`img.zst` и `image_sha256`), путь и SHA-256 `.shota`, итоги §2–§6. SHA-256 пакета утверждает владелец (GATE §5 B) до `ota-install --sha256`.
