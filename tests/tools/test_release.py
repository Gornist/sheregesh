# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Подготовка выпуска (tools/release/release.py): SBOM пакета в SPDX 2.3 (REQ-DOC-006), сверка SemVer тега, пакета,
паспорта и GUI и отдельной HW-версии (REQ-REL-010), комплект и отчёт проверки (REQ-REL-011). Без сети и плат:
пакет .deb собирается из заглушки теми же функциями ar/tar, что build_deb; проверки репозитория подменены."""
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/release"))
sys.path.insert(0, str(ROOT / "tools/image"))
import build_deb  # noqa: E402
import release as rel  # noqa: E402

DEPENDS = "python3 (>= 3.11), python3-yaml, gstreamer1.0-plugins-bad"


def fake_deb(dirpath, version="0.1.0~dev0+gabc1234"):
    ctrl = Path(dirpath) / "ctl"
    ctrl.mkdir(parents=True)
    (ctrl / "control").write_text(f"Package: sheregesh\nVersion: {version}\nArchitecture: all\n"
                                  f"Depends: {DEPENDS}\nDescription: test\n multi-line\n")
    data = Path(dirpath) / "data"
    (data / "usr").mkdir(parents=True)
    deb = b"!<arch>\n" + build_deb.ar_member("debian-binary", b"2.0\n", 0) + \
        build_deb.ar_member("control.tar.xz", build_deb.tar_bytes(ctrl, 0), 0) + \
        build_deb.ar_member("data.tar.xz", build_deb.tar_bytes(data, 0), 0)
    out = Path(dirpath) / f"sheregesh_{version}_all.deb"
    out.write_bytes(deb)
    return out


def test_package_sbom_lists_package_and_depends_and_is_valid_spdx(tmp_path):
    deb = fake_deb(tmp_path)
    doc = rel.package_sbom(deb, epoch=1793457876)
    assert rel.check_spdx(doc) == []
    main = doc["packages"][0]
    assert main["name"] == "sheregesh" and main["versionInfo"] == "0.1.0~dev0+gabc1234"
    assert main["licenseDeclared"] == "GPL-3.0-or-later" and main["checksums"][0]["algorithm"] == "SHA256"
    assert main["externalRefs"][0]["referenceLocator"] == "pkg:deb/sheregesh@0.1.0~dev0+gabc1234?arch=all"
    assert "SemVer 0.1.0-dev.0+gabc1234" in main["comment"]
    deps = [p["name"] for p in doc["packages"][1:]]
    assert deps == ["python3", "python3-yaml", "gstreamer1.0-plugins-bad"]
    assert sum(r["relationshipType"] == "DEPENDS_ON" for r in doc["relationships"]) == 3
    assert doc["creationInfo"]["created"] == "2026-10-31T14:44:36Z"
    assert rel.package_sbom(deb, epoch=1793457876) == doc                      # детерминирован


def test_check_spdx_reports_missing_parts():
    assert rel.check_spdx({}) != []
    doc = {"spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0", "SPDXID": "SPDXRef-DOCUMENT", "name": "x",
           "documentNamespace": "urn:x", "creationInfo": {"created": "t", "creators": ["Tool: t"]},
           "packages": [{"name": "a", "SPDXID": "SPDXRef-a", "downloadLocation": "NOASSERTION"}]}
    assert rel.check_spdx(doc) == ["нет связи DESCRIBES"]
    doc["documentDescribes"] = ["SPDXRef-a"]
    assert rel.check_spdx(doc) == []
    doc["packages"][0].pop("downloadLocation")
    assert any("downloadLocation" in e for e in rel.check_spdx(doc))


def test_deb_version_maps_to_semver():
    assert rel.deb_semver("0.1.0~dev0+g44f5c21") == "0.1.0-dev.0+g44f5c21"
    assert rel.deb_semver("1.2.3") == "1.2.3"
    assert rel.core("0.1.0-dev.0+g44f5c21") == "0.1.0-dev.0"


def _passport(tmp_path, role, software, hardware="HW-1.0 Rev A (Raspberry Pi 4 Model B Rev 1.5)"):
    p = tmp_path / f"{role}.json"
    p.write_text(json.dumps({"role": role, "software": software, "hardware": hardware}))
    return p


def test_versions_match_across_tag_package_passport_and_gui(tmp_path, monkeypatch):
    monkeypatch.setattr(rel, "__version__", "1.2.0")
    deb = tmp_path / "sheregesh_1.2.0+g1234567_all.deb"
    gui = tmp_path / "gui.json"
    gui.write_text(json.dumps({"about": {"aboutSoftware": "1.2.0",
                                         "aboutHardware": "HW-1.0 Rev A (Raspberry Pi 4 Model B Rev 1.5)"}}))
    r = rel.versions("v1.2.0", deb, [_passport(tmp_path, "console", "1.2.0")], gui)
    assert r["ok"], r
    r = rel.versions("v1.2.1", deb, [_passport(tmp_path, "console", "1.2.0")], gui)
    assert not r["ok"] and [c["what"] for c in r["checks"] if not c["ok"]] == ["тег"]
    r = rel.versions("v1.2.0", deb, [_passport(tmp_path, "remote", "1.1.9")])
    assert [c["what"] for c in r["checks"] if not c["ok"]] == ["паспорт remote (sheregesh-info)"]
    r = rel.versions("v1.2.0", deb, [_passport(tmp_path, "remote", "1.2.0", hardware="1.2.0")])
    assert [c["what"] for c in r["checks"] if not c["ok"]] == ["паспорт remote: HW-версия отдельно"]


def test_dev_source_rejects_release_tag():
    r = rel.versions("v0.1.0")
    assert rel.__version__.endswith(".dev0") and not r["ok"]          # тег выпуска на dev-исходниках — отказ


def _artifacts(tmp_path, monkeypatch):
    art = tmp_path / "art"
    art.mkdir()
    fake_deb(tmp_path / "debsrc")
    shutil.copy2(next((tmp_path / "debsrc").glob("*.deb")), art)
    (art / "sheregesh-lab-dev-pi4.img.zst").write_bytes(b"image")
    (art / "build-report.json").write_text(json.dumps({"commit": "abc1234", "image_sha256": "00",
                                                       "source_date_epoch": 1793457876}))
    (art / "shbuild-report.json").write_text(json.dumps(
        {"id": "job1", "state": "PASSED", "commit": "abc1234",
         "stages": [{"name": s, "status": "PASS"} for s in ("preflight", "checkout", "tests", "build", "gates", "boot")]}))
    sbom = rel.package_sbom(next(art.glob("*.deb")), 0)
    (art / "sheregesh-lab-dev-pi4.sbom.zst").write_text("sbom")
    monkeypatch.setattr(rel, "load_spdx", lambda p: sbom)                       # без zstd в окружении теста
    calls = []

    def run(argv, cwd=rel.ROOT):
        calls.append(argv)
        return (1, "NO-GO: 30 Must") if argv[-1] == "gate" else (0, "ok")
    monkeypatch.setattr(rel, "_run", run)
    return art, calls


def test_prepare_dry_run_builds_bundle_and_records_gate(tmp_path, monkeypatch):
    art, calls = _artifacts(tmp_path, monkeypatch)
    out = tmp_path / "out"
    r = rel.prepare(art, out, branches=("v2", "main"))
    steps = {s["id"]: s for s in r["steps"]}
    assert r["dry_run"] and r["tag"] == "v0.1.0-dev.0"
    assert steps["gate"]["verdict"] == "FAIL" and not steps["gate"]["blocking"]   # сухой прогон: записан, не стоп
    assert steps["reuse"]["verdict"] == "SKIP" and steps["shbuild"]["verdict"] == "PASS"
    assert {"history:v2", "history:main"} <= set(steps)
    assert ["history", "v2"] == calls[3][-2:] and ["history", "main"] == calls[4][-2:]
    names = sorted(p.name for p in out.iterdir())
    assert "sheregesh-0.1.0-dev.0-lab-dev-pi4.img.zst" in names                  # SemVer в имени архива
    assert {"RELEASE-NOTES.md", "release-check.md", "release-check.json", "SHA256SUMS"} <= set(names)
    assert "HW-1.0 Rev A" in (out / "RELEASE-NOTES.md").read_text()
    sums = (out / "SHA256SUMS").read_text()
    assert "sheregesh-0.1.0-dev.0-lab-dev-pi4.img.zst" in sums and "release-check.md" in sums
    assert steps["versions"]["verdict"] == "PASS" and "паспорт/GUI не переданы" in steps["versions"]["detail"]


def test_prepare_with_tag_is_strict(tmp_path, monkeypatch):
    art, _ = _artifacts(tmp_path, monkeypatch)
    r = rel.prepare(art, tmp_path / "out", tag="v0.1.0-dev.0")
    steps = {s["id"]: s for s in r["steps"]}
    assert r["verdict"] == "FAIL"
    assert steps["gate"]["blocking"] and steps["reuse"]["verdict"] == "FAIL" and steps["versions"]["verdict"] == "FAIL"


def test_prepare_rejects_artifacts_without_shbuild_pass(tmp_path, monkeypatch):
    art, _ = _artifacts(tmp_path, monkeypatch)
    rep = json.loads((art / "shbuild-report.json").read_text())
    (art / "shbuild-report.json").write_text(json.dumps({**rep, "state": "FAILED"}))
    r = rel.prepare(art, tmp_path / "out")
    assert {s["id"]: s for s in r["steps"]}["shbuild"]["verdict"] == "FAIL" and r["verdict"] == "FAIL"


@pytest.mark.skipif(shutil.which("zstd") is None, reason="нет zstd")
def test_load_spdx_reads_zst(tmp_path):
    import subprocess
    doc = rel.package_sbom(fake_deb(tmp_path), 0)
    raw = tmp_path / "x.json"
    raw.write_text(json.dumps(doc))
    subprocess.run(["zstd", "-q", str(raw), "-o", str(tmp_path / "x.sbom.zst")], check=True)
    assert rel.load_spdx(tmp_path / "x.sbom.zst") == doc


def test_reuse_lint_checks_commit_tree_not_working_copy(monkeypatch):
    """reuse lint — по дереву коммита (git archive): неотслеживаемые файлы рабочей копии не попадают в проверку."""
    seen = {}

    def run(argv, cwd=rel.ROOT):
        root = Path(argv[argv.index("--root") + 1])
        seen["files"] = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        return 0, "* Files with license information: 1 / 1\nCongratulations!"
    monkeypatch.setattr(rel, "_run", run)
    rc, txt = rel.reuse_lint("reuse", "HEAD")
    assert rc == 0 and "Congratulations" in txt and txt.startswith("коммит HEAD")
    assert "tools/security/scan.py" in seen["files"] and "REUSE.toml" in seen["files"]
    assert not any(f.startswith((".codex/", "analysis/")) for f in seen["files"])
