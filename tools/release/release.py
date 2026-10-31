#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Подготовка выпуска «Шерегеша» (REQ-REL-010, REQ-REL-011, REQ-DOC-006). Тега, подписи и публикации здесь нет:
тег ставит и выпуск публикует владелец (ADR-0005); этот инструмент собирает и проверяет комплект.

  release.py sbom-package --deb FILE --out FILE.spdx.json   SPDX 2.3 пакета sheregesh: пакет и его зависимости Depends
  release.py sbom-check FILE...                            структура SPDX 2.3 (.json или .json.zst/.sbom.zst)
  release.py versions [--tag vX.Y.Z] [--deb FILE] [--passport JSON]... [--gui JSON]
                                                           SemVer одинаков в теге, пакете, паспорте и GUI; HW — отдельно
  release.py prepare --artifacts DIR --out DIR [--tag vX.Y.Z] [--branch REF]... [--passport JSON]... [--gui JSON]
                     [--reuse CMD]                         комплект выпуска и отчёт проверки (release-check.md);
                                                           история — все ветки origin, если --branch не задан

prepare без --tag — сухой прогон: тег берётся из версии исходников, гейт реестра (REQ-REL-001) записывается, но не
останавливает сборку комплекта; с --tag любая проверка не PASS — код 1. Артефакты (--artifacts) — каталог задания
shbuild (`shbuild.py fetch`): образ, пакет .deb, SBOM образа, build-report.json, shbuild-report.json.
"""
import argparse
import hashlib
import io
import json
import lzma
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "software"))
sys.path.insert(0, str(ROOT / "tools/build"))
from sheregesh import __version__, semver  # noqa: E402
from sheregesh.info import HW_REVISION  # noqa: E402

REPO_URL = "https://github.com/Gornist/sheregesh"
LICENSE = "GPL-3.0-or-later"
SEMVER = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")


# --- SBOM ---------------------------------------------------------------------------------------------------------

def deb_control(deb):
    """Поля control пакета .deb (ar → control.tar.xz → ./control)."""
    data = Path(deb).read_bytes()
    if not data.startswith(b"!<arch>\n"):
        raise ValueError(f"{deb}: не архив ar")
    pos = 8
    while pos + 60 <= len(data):
        head = data[pos:pos + 60]
        name, size = head[:16].decode().strip().rstrip("/"), int(head[48:58])
        body = data[pos + 60:pos + 60 + size]
        if name.startswith("control.tar"):
            raw = lzma.decompress(body) if name.endswith(".xz") else body
            with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
                member = next(m for m in tar.getmembers() if m.name.lstrip("./") == "control")
                text = tar.extractfile(member).read().decode()
            fields, key = {}, None
            for line in text.splitlines():
                if line[:1] in (" ", "\t") and key:
                    fields[key] += "\n" + line.strip()
                elif ":" in line:
                    key, val = line.split(":", 1)
                    fields[key] = val.strip()
            return fields
        pos += 60 + size + (size % 2)
    raise ValueError(f"{deb}: нет control.tar")


def _spdx_id(name):
    return re.sub(r"[^A-Za-z0-9.-]", "-", name)


def package_sbom(deb, epoch=None):
    """SPDX 2.3 (JSON) пакета sheregesh: сам пакет (SHA-256, лицензия, purl) и зависимости Depends (DEPENDS_ON).
    Детерминирован: время — SOURCE_DATE_EPOCH (или время файла), пространство имён — по SHA-256 пакета."""
    deb = Path(deb)
    ctl = deb_control(deb)
    sha = hashlib.sha256(deb.read_bytes()).hexdigest()
    name, ver = ctl["Package"], ctl["Version"]
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch if epoch is not None else deb.stat().st_mtime))
    main_id = f"SPDXRef-Package-{_spdx_id(name)}"
    pkgs = [{"name": name, "SPDXID": main_id, "versionInfo": ver, "supplier": "Organization: Sheregesh contributors",
             "downloadLocation": REPO_URL, "filesAnalyzed": False, "licenseConcluded": LICENSE,
             "licenseDeclared": LICENSE, "copyrightText": "2026 Sheregesh contributors",
             "checksums": [{"algorithm": "SHA256", "checksumValue": sha}],
             "comment": f"SemVer {deb_semver(ver)}; файл {deb.name}",
             "externalRefs": [{"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl",
                               "referenceLocator": f"pkg:deb/{name}@{ver}?arch={ctl.get('Architecture', 'all')}"}]}]
    rels = [{"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES", "relatedSpdxElement": main_id}]
    for dep in (d.strip() for d in ctl.get("Depends", "").split(",") if d.strip()):
        dname = dep.split()[0].split(":")[0]
        did = f"SPDXRef-Package-dep-{_spdx_id(dname)}"
        pkgs.append({"name": dname, "SPDXID": did, "versionInfo": "NOASSERTION", "downloadLocation": "NOASSERTION",
                     "filesAnalyzed": False, "licenseConcluded": "NOASSERTION", "licenseDeclared": "NOASSERTION",
                     "copyrightText": "NOASSERTION", "comment": f"Depends: {dep}",
                     "externalRefs": [{"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl",
                                       "referenceLocator": f"pkg:deb/debian/{dname}"}]})
        rels.append({"spdxElementId": main_id, "relationshipType": "DEPENDS_ON", "relatedSpdxElement": did})
    return {"spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0", "SPDXID": "SPDXRef-DOCUMENT",
            "name": f"{name}_{ver}", "documentNamespace": f"{REPO_URL}/spdx/{deb.name}-{sha[:16]}",
            "creationInfo": {"created": created, "creators": ["Tool: sheregesh-release"]},
            "packages": pkgs, "relationships": rels}


def load_spdx(path):
    path = Path(path)
    raw = path.read_bytes()
    if path.suffix == ".zst":
        raw = subprocess.run(["zstd", "-dc", str(path)], capture_output=True, check=True).stdout
    return json.loads(raw)


def check_spdx(doc):
    """Ошибки структуры SPDX 2.3 (пусто — годен): обязательные поля документа и пакетов, связь DESCRIBES."""
    err = []
    if not str(doc.get("spdxVersion", "")).startswith("SPDX-2."):
        err.append("spdxVersion не SPDX-2.x")
    for k, v in (("dataLicense", "CC0-1.0"), ("SPDXID", "SPDXRef-DOCUMENT")):
        if doc.get(k) != v:
            err.append(f"{k} ≠ {v}")
    for k in ("name", "documentNamespace"):
        if not doc.get(k):
            err.append(f"нет {k}")
    ci = doc.get("creationInfo") or {}
    if not ci.get("created") or not ci.get("creators"):
        err.append("нет creationInfo.created/creators")
    pkgs = doc.get("packages") or []
    if not pkgs:
        err.append("нет packages")
    ids = set()
    for i, p in enumerate(pkgs):
        miss = [k for k in ("name", "SPDXID", "downloadLocation") if not p.get(k)]
        if miss:
            err.append(f"packages[{i}]: нет {', '.join(miss)}")
        ids.add(p.get("SPDXID"))
    described = doc.get("documentDescribes") or [r.get("relatedSpdxElement") for r in doc.get("relationships") or []
                                                 if r.get("spdxElementId") == "SPDXRef-DOCUMENT"
                                                 and r.get("relationshipType") == "DESCRIBES"]
    if not described:
        err.append("нет связи DESCRIBES")
    return err


# --- версии -------------------------------------------------------------------------------------------------------

def deb_semver(ver):
    """0.1.0~dev0+g44f5c21 → 0.1.0-dev.0+g44f5c21 (версия Debian пакета → SemVer)."""
    return semver(str(ver).replace("~", "."))


def core(v):
    """SemVer без метаданных сборки: их правило SemVer при сравнении версий не учитывает."""
    return str(v or "").split("+", 1)[0]


def versions(tag=None, deb=None, passports=(), gui=None, notes=None):
    """REQ-REL-010: версия ПО в SemVer одинакова в теге, пакете (имени архива), паспорте и GUI; HW-версия указана
    отдельно в паспорте и в выпуске. → {"semver", "checks": [{what, value, ok}], "ok"}."""
    src = semver(__version__)
    checks = [{"what": "исходники (sheregesh.__version__)", "value": src, "ok": bool(SEMVER.fullmatch(src))}]

    def add(what, value, ok=None):
        checks.append({"what": what, "value": value, "ok": core(value) == core(src) if ok is None else ok})
    if tag is not None:
        add("тег", tag, tag == f"v{core(src)}" or tag == f"v{src}")
    if deb is not None:
        m = re.fullmatch(r"[a-z0-9.+-]+_([^_]+)_[a-z0-9]+\.deb", Path(deb).name)
        add("пакет .deb", deb_semver(m.group(1)) if m else Path(deb).name, None if m else False)
    for p in passports:
        info = json.loads(Path(p).read_text())
        role = info.get("role") or "?"
        add(f"паспорт {role} (sheregesh-info)", info.get("software"))
        hw = info.get("hardware") or ""
        add(f"паспорт {role}: HW-версия отдельно", hw, hw.startswith(HW_REVISION) and core(src) not in hw)
    if gui is not None:
        about = (json.loads(Path(gui).read_text()).get("about") or {})
        add("GUI «О системе»", about.get("aboutSoftware"))
        add("GUI «О системе»: HW-версия", about.get("aboutHardware") or "",
            str(about.get("aboutHardware") or "").startswith(HW_REVISION))
    if notes is not None:
        add("примечания к выпуску: HW-версия", HW_REVISION, HW_REVISION in Path(notes).read_text())
    return {"semver": src, "hw": HW_REVISION, "checks": checks, "ok": all(c["ok"] for c in checks)}


# --- комплект выпуска ---------------------------------------------------------------------------------------------

def _run(argv, cwd=ROOT):
    r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def reuse_lint(reuse, commit):
    """reuse lint ровно по дереву коммита (git archive): неотслеживаемые файлы рабочей копии в выпуск не входят."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="release-reuse-") as tmp:
        arch = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", commit], capture_output=True)
        if arch.returncode != 0:
            return arch.returncode, f"git archive {commit}: {arch.stderr.decode(errors='replace').strip()}"
        with tarfile.open(fileobj=io.BytesIO(arch.stdout)) as tar:
            tar.extractall(tmp, filter="data")
        rc, txt = _run([*reuse.split(), "--root", tmp, "lint"], cwd=tmp)
    lines = [ln for ln in txt.splitlines() if ln.startswith(("* Files with", "Congratulations", "Unfortunately"))
             or ln.startswith("* ") and rc != 0 and "/" in ln]
    return rc, f"коммит {commit[:12]}: " + "; ".join(lines[-6:])


def published_branches():
    """Публикуемые ветки — все ветки удалённого origin (REQ-REL-011: «вся история публикуемых веток»); без origin —
    v2."""
    rc, out = _run(["git", "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"])
    refs = [r for r in out.splitlines() if rc == 0 and r and r != "origin" and not r.endswith("/HEAD")]
    return tuple(refs) or ("v2",)


def release_notes(sv, art, build, image_name, sbom_names):
    reqs = json.loads((ROOT / "docs/requirements/requirements.json").read_text())
    chg = [c for c in reqs.get("changes", [])]
    lines = [f"# «Шерегеш» {sv}", "",
             f"- **ПО (SemVer):** {sv}",
             f"- **Аппаратная спецификация:** {HW_REVISION} (отдельная версия, REQ-REL-010)",
             f"- **Коммит:** `{build.get('commit')}`",
             f"- **Образ:** `{image_name}` (SHA-256 архива — `SHA256SUMS`; несжатого образа "
             f"`{build.get('image_sha256')}`)",
             f"- **SBOM (SPDX 2.3):** {', '.join(f'`{n}`' for n in sbom_names)}",
             f"- **Лицензии:** ПО {LICENSE}, аппаратура CERN-OHL-S-2.0, документация CC-BY-SA-4.0",
             "", "## Изменения требований", ""]
    lines += [f"- {c['id']} ({c.get('date', '')}): {c.get('title', '')}" for c in chg[-20:]] or ["- нет"]
    lines += ["", "Проверки выпуска — `release-check.md`; контрольные суммы — `SHA256SUMS`.", ""]
    return "\n".join(lines)


def prepare(art, out, tag=None, branches=None, passports=(), gui=None, reuse=None):
    art, out = Path(art), Path(out)
    branches = tuple(branches) if branches else published_branches()
    dry = tag is None
    sv = semver(__version__)
    tag = tag or f"v{core(sv)}"
    out.mkdir(parents=True, exist_ok=True)
    steps = []

    def step(sid, what, verdict, detail="", blocking=True):
        steps.append({"id": sid, "what": what, "verdict": verdict, "detail": detail[-2000:], "blocking": blocking})

    import gate
    try:
        rep = gate.require_passed(art)
        step("shbuild", "задание shbuild PASSED, все этапы PASS (ADR-0018)", "PASS", rep.get("id", ""))
    except gate.GateError as e:
        step("shbuild", "задание shbuild PASSED, все этапы PASS (ADR-0018)", "FAIL", str(e))
    build = json.loads((art / "build-report.json").read_text()) if (art / "build-report.json").exists() else {}

    rc, txt = _run([sys.executable, "-I", "tools/reqs/reqgen.py", "validate"])
    step("reqs", "реестр требований валиден", "PASS" if rc == 0 else "FAIL", txt)
    rc, txt = _run([sys.executable, "-I", "tools/reqs/reqgen.py", "gate"])
    step("gate", "гейт REQ-REL-001: все Must PASS на требуемом уровне", "PASS" if rc == 0 else "FAIL",
         next((ln for ln in txt.splitlines() if ln.startswith(("GO", "NO-GO"))), txt.splitlines()[-1] if txt else ""),
         blocking=not dry)

    rc, txt = _run([sys.executable, "-I", "tools/security/scan.py", "secrets"])
    step("secrets", "секреты в отслеживаемых файлах (REQ-REL-011)", "PASS" if rc == 0 else "FAIL", txt)
    for ref in branches:
        rc, txt = _run([sys.executable, "-I", "tools/security/scan.py", "history", ref])
        step(f"history:{ref}", f"секреты во всей истории {ref}; решения — docs/security/history-findings.md",
             "PASS" if rc == 0 else "FAIL", txt)
    if reuse:
        rc, txt = reuse_lint(reuse, build.get("commit") or "HEAD")
        step("reuse", "reuse lint дерева коммита выпуска: у каждого файла подтверждённая лицензия (REQ-REL-011, "
             "REQ-DOC-006)", "PASS" if rc == 0 else "FAIL", txt)
    else:
        step("reuse", "reuse lint (REQ-REL-011, REQ-DOC-006)", "SKIP" if dry else "FAIL",
             "не запускался: укажите --reuse 'reuse' (в CI — job ci «Лицензии REUSE»)")
    lic = ROOT / "docs/security/dependency-licenses.md"
    step("provenance", "происхождение и лицензии сторонних зависимостей описаны", "PASS" if lic.exists() else "FAIL",
         str(lic.relative_to(ROOT)))

    # SBOM: образа (rpi-image-gen/syft) и пакета (этот инструмент)
    sboms = []
    img_sbom = next(iter(sorted(art.glob("*.sbom.zst"))), None)
    if img_sbom is None:
        step("sbom:image", "SBOM образа SPDX", "FAIL", "нет *.sbom.zst в артефактах")
    else:
        e = check_spdx(load_spdx(img_sbom))
        step("sbom:image", "SBOM образа SPDX", "FAIL" if e else "PASS", "; ".join(e) or img_sbom.name)
    debs = sorted(art.glob("*.deb"))
    deb = debs[0] if len(debs) == 1 else None
    pkg_sbom_name = None
    if deb is None:
        step("sbom:package", "SBOM пакета SPDX", "FAIL", f"пакетов .deb: {len(debs)}")
    else:
        pkg_sbom = art / (deb.name + ".spdx.json")
        if not pkg_sbom.exists():
            pkg_sbom = out / (deb.name + ".spdx.json")
            pkg_sbom.write_text(json.dumps(package_sbom(deb, build.get("source_date_epoch")), ensure_ascii=False,
                                           indent=1))
        e = check_spdx(load_spdx(pkg_sbom))
        step("sbom:package", "SBOM пакета SPDX", "FAIL" if e else "PASS", "; ".join(e) or pkg_sbom.name)
        pkg_sbom_name = pkg_sbom.name

    # комплект: имя архива с SemVer
    image = next(iter(sorted(art.glob("*.img.zst"))), None)
    files = []
    if image is not None:
        dst = out / f"sheregesh-{core(sv)}-{image.name.removeprefix('sheregesh-')}"
        if not dst.exists():
            try:
                dst.hardlink_to(image)
            except OSError:
                shutil.copy2(image, dst)
        files.append(dst)
    if deb is not None:
        shutil.copy2(deb, out / deb.name)
        files.append(out / deb.name)
    if img_sbom is not None:
        dst = out / f"sheregesh-{core(sv)}-{img_sbom.name.removeprefix('sheregesh-')}"
        shutil.copy2(img_sbom, dst)
        files.append(dst)
    if pkg_sbom_name:
        if not (out / pkg_sbom_name).exists():
            shutil.copy2(art / pkg_sbom_name, out / pkg_sbom_name)
        files.append(out / pkg_sbom_name)
    notes = out / "RELEASE-NOTES.md"
    notes.write_text(release_notes(sv, art, build, files[0].name if image is not None else "—",
                                   [f.name for f in files if "sbom" in f.name or f.name.endswith(".spdx.json")]))
    files.append(notes)

    ver = versions(tag, deb, passports, gui, notes)
    step("versions", "REQ-REL-010: SemVer одинаков в теге, имени архива, паспорте и GUI; HW-версия отдельно",
         "PASS" if ver["ok"] and image is not None and core(sv) in files[0].name else "FAIL",
         "; ".join(f"{c['what']}={c['value']}{'' if c['ok'] else ' ✗'}" for c in ver["checks"]))
    if not passports or gui is None:
        steps[-1]["detail"] += "; паспорт/GUI не переданы (--passport, --gui)"
        if not dry:
            steps[-1]["verdict"] = "FAIL"

    ok = all(s["verdict"] == "PASS" for s in steps if s["blocking"] and not (dry and s["verdict"] == "SKIP"))
    result = {"tag": tag, "dry_run": dry, "semver": sv, "hw": HW_REVISION, "commit": build.get("commit"),
              "verdict": "PASS" if ok else "FAIL", "steps": steps,
              "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (out / "release-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    md = [f"# Проверка выпуска {tag}{' (сухой прогон)' if dry else ''}", "",
          f"Итог: **{result['verdict']}** · ПО {sv} · {HW_REVISION} · коммит `{build.get('commit')}`", "",
          "| Проверка | Итог | Подробно |", "|---|---|---|"]
    md += [f"| {s['what']}{'' if s['blocking'] else ' (не блокирует сухой прогон)'} | {s['verdict']} | "
           f"{s['detail'].splitlines()[-1] if s['detail'] else ''} |".replace("\n", " ") for s in steps]
    (out / "release-check.md").write_text("\n".join(md) + "\n")
    files += [out / "release-check.md", out / "release-check.json"]
    (out / "SHA256SUMS").write_text("".join(f"{_sha256(f)}  {f.name}\n" for f in files))
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sbom-package")
    p.add_argument("--deb", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--epoch", type=int, default=None)
    p = sub.add_parser("sbom-check")
    p.add_argument("files", nargs="+", type=Path)
    p = sub.add_parser("versions")
    p.add_argument("--tag")
    p.add_argument("--deb")
    p.add_argument("--passport", action="append", default=[])
    p.add_argument("--gui")
    p = sub.add_parser("prepare")
    p.add_argument("--artifacts", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--tag")
    p.add_argument("--branch", action="append", default=None)
    p.add_argument("--passport", action="append", default=[])
    p.add_argument("--gui")
    p.add_argument("--reuse", default=None, help="команда reuse, например 'reuse' или '.venv/bin/reuse'")
    a = ap.parse_args(argv)
    if a.cmd == "sbom-package":
        a.out.write_text(json.dumps(package_sbom(a.deb, a.epoch), ensure_ascii=False, indent=1))
        print(a.out)
        return 0
    if a.cmd == "sbom-check":
        bad = 0
        for f in a.files:
            e = check_spdx(load_spdx(f))
            print(f"{'FAIL' if e else 'PASS'} {f}" + (": " + "; ".join(e) if e else ""))
            bad += bool(e)
        return 1 if bad else 0
    if a.cmd == "versions":
        r = versions(a.tag, a.deb, a.passport, a.gui)
        for c in r["checks"]:
            print(f"{'PASS' if c['ok'] else 'FAIL'} {c['what']}: {c['value']}")
        return 0 if r["ok"] else 1
    r = prepare(a.artifacts, a.out, a.tag, a.branch, a.passport, a.gui, a.reuse)
    for s in r["steps"]:
        print(f"{s['verdict']:4} {s['id']}: {s['detail'].splitlines()[-1] if s['detail'] else ''}"[:200])
    print(f"итог {r['verdict']} ({'сухой прогон' if r['dry_run'] else r['tag']}) → {a.out}")
    return 0 if r["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
