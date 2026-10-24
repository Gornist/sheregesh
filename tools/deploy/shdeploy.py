#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""shdeploy — обновления лабораторных плат «Шерегеша» с Mac без перестановки microSD (S3.1-DEPLOY).

  python3 tools/deploy/shdeploy.py status [pi1|pi2]          состояние плат (только чтение)
  python3 tools/deploy/shdeploy.py deploy pi1|pi2            выпуск приложения из текущего коммита Git
  python3 tools/deploy/shdeploy.py rollback pi1|pi2          предыдущий выпуск
  python3 tools/deploy/shdeploy.py factory pi1|pi2           заводской код образа (/usr/lib/sheregesh)
  python3 tools/deploy/shdeploy.py build --out DIR           только собрать (и подписать с --sign) пакет
  python3 tools/deploy/shdeploy.py ota-build --image IMG --build-report JSON --out DIR
  python3 tools/deploy/shdeploy.py ota-status|ota-install|ota-enable|ota-disable|ota-tryboot|ota-confirm|ota-abort pi2

Плата за раз — одна (вторая продолжает работать). Адрес: --host, переменная SHEREGESH_PI1_HOST/SHEREGESH_PI2_HOST,
кэш ~/.cache/sheregesh/boards.json или поиск по MAC в локальной сети; перед любым действием сверяется серийный номер
платы — DHCP не подменит pi1 на pi2. Вход — sheregesh-ui по ключу SSH (как раньше); ключ подписи выпусков —
--key, SHEREGESH_DEPLOY_KEY или ~/.ssh/sheregesh-deploy, отдельный от ключа входа (закрытый ключ не покидает Mac:
подпись делается локально ssh-keygen -Y sign, на плату уходят только пакет и подпись).
Пакет собирается из закоммиченного состояния HEAD; незакоммиченные изменения в него НЕ входят (предупреждение).
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "software"), str(ROOT / "tools/image"), str(ROOT / "tools/probe")]
import build_deb  # noqa: E402
from sheregesh.deploy import bundle, sig  # noqa: E402
from sheregesh.update import ota  # noqa: E402

BOARDS = {"pi1": {"serial": "10000000568152de", "mac": "d8:3a:dd:69:0e:90", "role": "пульт"},
          "pi2": {"serial": "100000009d497e91", "mac": "d8:3a:dd:69:0f:6e", "role": "удалённый блок"}}
USER = "sheregesh-ui"
INCOMING = ".cache/sheregesh-deploy/incoming"
STATE = "/var/lib/sheregesh-deploy/state.json"
CACHE = Path.home() / ".cache/sheregesh/boards.json"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new"]


class CliError(Exception):
    pass


def git(*args, binary=False):
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=True)
    return r.stdout if binary else r.stdout.decode().strip()


# ---------------------------------------------------------------- пакет приложения


def collect_files(rev="HEAD"):
    """Файлы выпуска из коммита rev (не из рабочего каталога): те же каталоги, что .deb, плюс .qm из .ts."""
    out = {}
    listing = git("ls-tree", "-r", "-z", "--full-tree", rev, "--", *build_deb.SOURCES, binary=True)
    for entry in filter(None, listing.split(b"\0")):
        meta, path = entry.decode().split("\t", 1)
        mode, kind, obj = meta.split()
        if kind != "blob":
            continue
        out[path] = (git("cat-file", "blob", obj, binary=True), 0o755 if mode == "100755" else 0o644)
    tool = build_deb.lrelease()
    if not tool:
        raise CliError("нет lrelease (pyside6-lrelease): переводы .qm обязательны")
    with tempfile.TemporaryDirectory() as tmp:
        for path in [p for p in out if p.endswith(".ts")]:
            src = Path(tmp) / Path(path).name
            src.write_bytes(out[path][0])
            subprocess.run([tool, "-silent", str(src), "-qm", str(src.with_suffix(".qm"))], check=True,
                           capture_output=True)
            out[path[:-3] + ".qm"] = (src.with_suffix(".qm").read_bytes(), 0o644)
    return out


def build_release(rev="HEAD"):
    commit = git("rev-parse", rev)
    short = commit[:12]
    base = build_deb.default_version().split("+")[0]
    version = f"{base}+g{short}"
    dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    data = bundle.build_bundle(collect_files(rev), version, commit, int(git("log", "-1", "--format=%ct", rev)),
                               dirty=False, extra={"built_by": "tools/deploy/shdeploy.py"})
    return data, version, commit, dirty


def signing_key(arg):
    for k in (arg, os.environ.get("SHEREGESH_DEPLOY_KEY"), "~/.ssh/sheregesh-deploy"):   # ключ входа не подставляется
        if k and Path(k).expanduser().exists():
            return Path(k).expanduser()
    raise CliError("нет ключа подписи: --key, SHEREGESH_DEPLOY_KEY или ~/.ssh/sheregesh-deploy")


# ---------------------------------------------------------------- платы


class Remote:
    def __init__(self, host, user=USER):
        self.host, self.user = host, user

    def run(self, cmd, data=None, timeout=900, check=False):
        r = subprocess.run(["ssh", *SSH_OPTS, f"{self.user}@{self.host}", cmd], input=data, capture_output=True,
                           timeout=timeout)
        if check and r.returncode:
            raise CliError(f"{self.host}: {cmd.split()[0]}: {r.stderr.decode(errors='replace').strip()[-300:]}")
        return r.returncode, r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace")

    def stream(self, cmd, src, timeout=7200):
        """Длинное действие с выводом построчно (OTA): stdin — файл."""
        with open(src, "rb") if src else open(os.devnull, "rb") as f:
            p = subprocess.Popen(["ssh", *SSH_OPTS, f"{self.user}@{self.host}", cmd], stdin=f, stdout=subprocess.PIPE)
            last = {}
            for line in p.stdout:
                text = line.decode(errors="replace").rstrip()
                try:
                    obj = json.loads(text)
                    last = obj
                    print(obj.get("msg") or json.dumps(obj, ensure_ascii=False, indent=1))
                except ValueError:
                    print(text)
            p.wait(timeout)
        return p.returncode, last


def _cache():
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def resolve(board, host=None, remote_cls=None, finder=None):
    """Адрес платы + проверка серийного номера. Возвращает Remote."""
    if board not in BOARDS:
        raise CliError(f"неизвестная плата {board!r}: pi1 или pi2")
    want = BOARDS[board]["serial"]
    candidates = [h for h in (host, os.environ.get(f"SHEREGESH_{board.upper()}_HOST"), _cache().get(board)) if h]
    if not candidates:
        if finder is None:
            import collect_boot
            finder = collect_boot.find
        net = os.environ.get("SHEREGESH_LAN", "192.168.1.0/24")
        print(f"{board}: поиск по MAC {BOARDS[board]['mac']} в {net}…", file=sys.stderr)
        found = finder(BOARDS[board]["mac"], net)
        candidates = [found] if found else []
    for h in candidates:
        r = (remote_cls or Remote)(h)
        rc, out, _ = r.run("tr -d '\\0' < /proc/device-tree/serial-number", timeout=30)
        if rc == 0 and out.strip() == want:
            c = _cache()
            c[board] = h
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(c, indent=1))
            return r
        if rc == 0:
            print(f"{board}: {h} — другая плата (серийный {out.strip()}), пропуск", file=sys.stderr)
    raise CliError(f"{board} не найдена (серийный {want}); укажите --host")


def show_state(text):
    try:
        st = json.loads(text)
    except ValueError:
        return text.strip() or "нет данных"
    last = st.get("last") or {}
    lines = [f"  выпуск: {st.get('current') or 'заводской код'}; предыдущий: {st.get('previous') or '—'}",
             f"  последнее: {last.get('action', '—')} → {last.get('result', '—')}: {last.get('reason', '')}"]
    for unit, d in (last.get("units") or {}).items():
        lines.append(f"    {unit}: {d}")
    for unit, d in (last.get("rollback_units") or {}).items():
        lines.append(f"    возврат {unit}: {d}")
    return "\n".join(lines)


def deploy_action(remote, action, data=None, signature=None):
    """Загрузка (для install) и запуск экземпляра установщика; итог — из state.json платы."""
    if action == "install":
        remote.run(f"mkdir -p ~/{INCOMING} && rm -f ~/{INCOMING}/bundle.*", check=True)
        for name, blob in (("bundle.tar.xz.sig", signature), ("bundle.tar.xz", data)):
            remote.run(f"cat > ~/{INCOMING}/{name}.part && mv ~/{INCOMING}/{name}.part ~/{INCOMING}/{name}",
                       data=blob, check=True)
    _, before, _ = remote.run(f"cat {STATE} 2>/dev/null")
    rc, out, err = remote.run(f"systemctl start sheregesh-deploy@{action}.service; rc=$?; cat {STATE}; exit $rc")
    # Неуспех установки — не «упавший юнит» (SuccessExitStatus=1 2, чтобы не портить sheregesh-health):
    # итог берётся из state.json, а запуск подтверждается новой записью в истории.
    try:
        prev = json.loads(before) if before.strip() else {}
        st = json.loads(out)
        ran = (len(st.get("history", [])) != len(prev.get("history", []))) or st.get("last") != prev.get("last")
        ok = ran and st.get("last", {}).get("result") == "OK"
        if not ran:
            err = (err + "\nустановщик не выполнил действие (юнит не запускался?)").strip()
    except (ValueError, AttributeError):
        ok = False
    return (0 if rc == 0 and ok else 1), out, err


def cmd_build(a):
    data, version, commit, dirty = build_release(a.rev)
    a.out.mkdir(parents=True, exist_ok=True)
    f = a.out / f"sheregesh-app_{bundle.release_id(version)}.tar.xz"
    f.write_bytes(data)
    if a.sign:
        f.with_name(f.name + ".sig").write_bytes(sig.sign(data, signing_key(a.key), sig.NS_APP))
    print(f"{f}\n  версия {version}\n  коммит {commit}\n  sha256 {hashlib.sha256(data).hexdigest()}")
    if dirty:
        print("  ВНИМАНИЕ: есть незакоммиченные изменения — в пакет они не вошли", file=sys.stderr)
    return 0


def cmd_deploy(a):
    remote = resolve(a.board, a.host)
    if a.action == "install":
        data, version, commit, dirty = build_release(a.rev)
        if dirty:
            print("ВНИМАНИЕ: незакоммиченные изменения в пакет не входят (только HEAD)", file=sys.stderr)
        signature = sig.sign(data, signing_key(a.key), sig.NS_APP)
        print(f"{a.board} ({remote.host}): выпуск {version}, sha256 {hashlib.sha256(data).hexdigest()[:16]}…, "
              f"{len(data) // 1024} КиБ")
        rc, out, err = deploy_action(remote, "install", data, signature)
    else:
        rc, out, err = deploy_action(remote, a.action)
    print(show_state(out))
    if rc and not out.strip():
        print(err.strip()[-400:], file=sys.stderr)
    return 0 if rc == 0 else 1


STATUS_CMD = ("echo '== система'; tr -d '\\0' < /proc/device-tree/serial-number; echo; uptime; "
              "readlink -f /dev/disk/by-slot/active/system; "
              "echo '== службы'; systemctl show -p Id,ActiveState,SubState,NRestarts sheregesh-console "
              "sheregesh-remote-emu sheregesh-remote-net sheregesh-ota.socket 2>/dev/null | paste -sd' ' | "
              "sed 's/Id=/\\nId=/g'; echo; echo '== deploy'; cat " + STATE + " 2>/dev/null || echo '{}'; "
              "echo '== ota'; sheregesh-ota-client status 2>&1 | tail -1")


def cmd_status(a):
    rc_all = 0
    for board in ([a.board] if a.board else list(BOARDS)):
        try:
            remote = resolve(board, a.host if a.board else None)
        except CliError as e:
            print(f"## {board}: {e}")
            rc_all = 1
            continue
        rc, out, _ = remote.run(STATUS_CMD, timeout=60)
        print(f"## {board} ({BOARDS[board]['role']}) — {remote.host}")
        part = out.split("== deploy")
        print(part[0].rstrip())
        if len(part) > 1:
            dep, _, rest = part[1].partition("== ota")
            print("== deploy\n" + show_state(dep))
            print("== ota\n  " + rest.strip()[:600])
    return rc_all


# ---------------------------------------------------------------- OTA


def _decompressed(image, tmp):
    if image.suffix in (".zst", ".xz"):
        tool = "zstd" if image.suffix == ".zst" else "xz"
        if not shutil.which(tool):
            raise CliError(f"нет {tool} для распаковки {image.name}")
        out = Path(tmp) / image.stem
        with open(out, "wb") as f:
            subprocess.run([tool, "-dc", str(image)], stdout=f, check=True)
        return out
    return image


def _compress_range(img, offset, size, dst):
    h_raw, h_comp = hashlib.sha256(), hashlib.sha256()
    xz = shutil.which("xz")
    with open(img, "rb") as src, open(dst, "wb") as out:
        src.seek(offset)
        if xz:
            # Фиксированные потоки и размер блока: результат не зависит от числа ядер машины (воспроизводимость).
            p = subprocess.Popen([xz, "--threads=4", "--block-size=16MiB", "-6", "-c", "--check=crc64"],
                                 stdin=subprocess.PIPE, stdout=out)
        else:
            import lzma
            comp = lzma.LZMACompressor(format=lzma.FORMAT_XZ, preset=6)
        left = size
        while left:
            chunk = src.read(min(8 << 20, left))
            if not chunk:
                raise CliError("образ короче раздела")
            left -= len(chunk)
            h_raw.update(chunk)
            if xz:
                p.stdin.write(chunk)
            else:
                out.write(comp.compress(chunk))
        if xz:
            p.stdin.close()
            if p.wait():
                raise CliError("xz завершился с ошибкой")
        else:
            out.write(comp.flush())
    with open(dst, "rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h_comp.update(chunk)
    return {"size": size, "sha256": h_raw.hexdigest(), "compressed_size": dst.stat().st_size,
            "compressed_sha256": h_comp.hexdigest(), "compression": "xz"}


def deb_units(deb):
    """Юниты systemd пакета sheregesh из .deb (ar + tar) с SHA-256 — проверенный набор в манифесте обновления."""
    import tarfile
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["ar", "x", str(Path(deb).resolve())], cwd=tmp, check=True, capture_output=True)
        data = next(Path(tmp).glob("data.tar.*"))
        with tarfile.open(data) as tar:
            for m in tar.getmembers():
                if m.isfile() and m.name.lstrip("./").startswith("usr/lib/systemd/system/sheregesh"):
                    out[Path(m.name).name] = hashlib.sha256(tar.extractfile(m).read()).hexdigest()
    return dict(sorted(out.items()))


def build_ota(image, variant, version, commit, out_dir, key, source="", created=None, extra=None):
    import verify_image as vi
    dev = "-dev-" in variant
    with tempfile.TemporaryDirectory() as tmp:
        img = _decompressed(Path(image), tmp)
        with open(img, "rb") as f:
            gpt = vi.read_gpt(f)
        by = {p["name"]: p for p in gpt["partitions"]}
        if [p["name"] for p in gpt["partitions"]] != [n for n, *_ in vi.LAYOUT]:
            raise CliError(f"разметка образа не image-rota: {[p['name'] for p in gpt['partitions']]}")
        if vi.sha256_range(img, by["boot_a"]["offset"], by["boot_a"]["size"]) != \
                vi.sha256_range(img, by["boot_b"]["offset"], by["boot_b"]["size"]):
            raise CliError("boot_a и boot_b образа различаются — образ не из сборки image-rota")
        imgs = []
        for part, name in (("boot", "boot_a"), ("system", "system_a")):
            dst = Path(tmp) / f"{part}.xz"
            imgs.append({"part": part, **_compress_range(img, by[name]["offset"], by[name]["size"], dst)})
        manifest = {"format": ota.FORMAT, "kind": ota.KIND, "layout": ota.LAYOUT,
                    "compatible": ota.device_compatible(dev), "version": version, "commit": commit, "variant": variant,
                    "source": source, "images": imgs, **(extra or {}),
                    "created": created or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        raw = json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True).encode()
        signature = sig.sign(raw, key, sig.NS_OTA)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"sheregesh-ota_{variant}_{version}.shota"
        with open(out, "wb") as f:
            ota.write_payload(f, raw, signature, [Path(tmp) / "boot.xz", Path(tmp) / "system.xz"])
        (out_dir / (out.name + ".manifest.json")).write_bytes(raw)
    return out, manifest


def cmd_ota_build(a):
    if a.build_report:
        rep = json.loads(Path(a.build_report).read_text())
        commit, epoch, variant = rep["commit"], int(rep["source_date_epoch"]), rep["config"]
        major, minor = build_deb.default_version().split("~")[0].split(".")[:2]
        version = f"{int(major)}.{int(minor)}.{epoch}"
        created = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")   # воспроизводимо
        art = Path(a.build_report).parent
        extra = {"image_sha256": rep.get("image_sha256")}
        debs = sorted(art.glob("sheregesh_*.deb"))
        if debs:
            extra["package"] = debs[0].name
            extra["units"] = deb_units(debs[0])
        if (art / "verify.json").exists():
            v = json.loads((art / "verify.json").read_text())
            checks = v.get("checks", [])
            extra["image_verify"] = {"sha256": hashlib.sha256((art / "verify.json").read_bytes()).hexdigest(),
                                     "checks": len(checks), "failed": [c["id"] for c in checks if c["status"] != "PASS"]}
            if extra["image_verify"]["failed"]:
                raise CliError(f"проверка структуры образа не пройдена: {extra['image_verify']['failed']}")
    else:
        created, extra = None, None
        if not (a.version and a.commit and a.variant):
            raise CliError("нужен --build-report или --version, --commit и --variant")
        version, commit, variant = a.version, a.commit, a.variant
    out, m = build_ota(a.image, variant, version, commit, a.out, signing_key(a.key), source=Path(a.image).name,
                       created=created, extra=extra)
    h = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"{out}\n  версия {version} ({m['compatible']}), коммит {commit}\n  sha256 {h}")
    for i in m["images"]:
        print(f"  {i['part']}: {i['size']} байт → {i['compressed_size']} (xz), sha256 {i['sha256'][:16]}…")
    return 0


def cmd_ota(a):
    remote = resolve(a.board, a.host)
    action = a.command[len("ota-"):]
    if action == "install":
        if not a.payload:
            raise CliError("нужен --payload ФАЙЛ.shota (ota-build)")
        print(f"{a.board} ({remote.host}): поток {a.payload} → неактивный слот (основной не меняется)")
        rc, last = remote.stream("sheregesh-ota-client install", a.payload)
        return 0 if last.get("result") == "OK" else 1
    if action == "enable":
        token = json.dumps({"kind": "sheregesh-ota-enable", "serial": BOARDS[a.board]["serial"], "switching": True,
                            "board": a.board, "issued": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        req = json.dumps({"token": token, "sig": sig.sign(token.encode(), signing_key(a.key),
                                                          sig.NS_OTA_ENABLE).decode()})
        rc, out, err = remote.run("sheregesh-ota-client enable", data=req.encode())
    else:
        rc, out, err = remote.run(f"sheregesh-ota-client {action}")
    print(out.strip() or err.strip())
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(prog="shdeploy", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--rev", default="HEAD")
    b.add_argument("--sign", action="store_true")
    b.add_argument("--key")
    s = sub.add_parser("status")
    s.add_argument("board", nargs="?", choices=list(BOARDS))
    s.add_argument("--host")
    for name, action in (("deploy", "install"), ("rollback", "rollback"), ("factory", "factory")):
        p = sub.add_parser(name)
        p.add_argument("board", choices=list(BOARDS))
        p.add_argument("--host")
        p.add_argument("--key")
        p.add_argument("--rev", default="HEAD")
        p.set_defaults(action=action)
    o = sub.add_parser("ota-build")
    o.add_argument("--image", type=Path, required=True)
    o.add_argument("--build-report")
    o.add_argument("--variant")
    o.add_argument("--version")
    o.add_argument("--commit")
    o.add_argument("--out", type=Path, required=True)
    o.add_argument("--key")
    for name in ("ota-status", "ota-install", "ota-enable", "ota-disable", "ota-tryboot", "ota-confirm", "ota-abort"):
        p = sub.add_parser(name)
        p.add_argument("board", choices=list(BOARDS))
        p.add_argument("--host")
        p.add_argument("--key")
        p.add_argument("--payload")
    a = ap.parse_args(argv)
    try:
        if a.command == "build":
            return cmd_build(a)
        if a.command == "status":
            return cmd_status(a)
        if a.command in ("deploy", "rollback", "factory"):
            return cmd_deploy(a)
        if a.command == "ota-build":
            return cmd_ota_build(a)
        return cmd_ota(a)
    except (CliError, sig.SignatureError, bundle.BundleError, subprocess.CalledProcessError) as e:
        print(f"shdeploy: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
