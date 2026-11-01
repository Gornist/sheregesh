# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Руководство пользователя (tools/docs/manual.py, REQ-DOC-004, TEST-DOC-004): инструкции сборки, обслуживания и
восстановления на RU и EN с одинаковыми разделами, HTML из Markdown при сборке пакета с версией ПО на каждой
странице, в пакете — /usr/share/sheregesh/manual."""
import io
import lzma
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/docs"))
sys.path.insert(0, str(ROOT / "tools/image"))
import manual  # noqa: E402


def test_languages_have_same_pages_and_sections():
    assert manual.check() == []
    for lang in manual.LANGS:
        for page in manual.PAGES:
            text = (manual.SRC / lang / f"{page}.md").read_text(encoding="utf-8")
            assert text.count("\n## ") >= 3 and ("SPDX-License-" + "Identifier: CC-BY-SA-4.0") in text   # не тег REUSE


def test_check_reports_missing_and_different_sections(tmp_path, monkeypatch):
    for lang in manual.LANGS:
        (tmp_path / lang).mkdir()
        for page in manual.PAGES:
            (tmp_path / lang / f"{page}.md").write_text("# T\n\n## A\n")
    monkeypatch.setattr(manual, "SRC", tmp_path)
    monkeypatch.setattr(manual, "ROOT", tmp_path)
    assert manual.check() == []
    (tmp_path / "en" / "build.md").write_text("# T\n\n## A\n\n## B\n")
    (tmp_path / "ru" / "recovery.md").unlink()
    err = manual.check()
    assert any("build: разделы различаются" in e for e in err) and any("recovery.md" in e for e in err)


def test_markdown_subset_to_html():
    h = manual.to_html("<!-- c -->\n# Заголовок\n\nТекст **жирный** и `код <x>`.\n\n1. Раз\n   - вложенный\n2. Два\n\n"
                       "```sh\n  cmd --a\n```\n")
    assert "<!--" not in h and "<h1>Заголовок</h1>" in h and "<strong>жирный</strong>" in h
    assert "<code>код &lt;x&gt;</code>" in h and "<ol><li>" in h and "<ul><li>" in h
    assert "<pre><code>cmd --a</code></pre>" in h
    assert h.count("<li>") == h.count("</li>")


def test_build_stamps_version_on_every_page(tmp_path):
    files = manual.build(tmp_path, "1.2.3", "abcdef1234567890")
    pages = [f for f in files if f.parent.name in manual.LANGS]
    assert len(pages) == len(manual.LANGS) * len(manual.PAGES)
    for f in pages:
        t = f.read_text(encoding="utf-8")
        assert "1.2.3 · abcdef123456" in t and f'href="../{"en" if f.parent.name == "ru" else "ru"}/' in t
    assert "Sheregesh 1.2.3" in (tmp_path / "index.html").read_text(encoding="utf-8")


def test_deb_contains_manual_with_package_version(tmp_path):
    import build_deb
    from sheregesh import __version__, semver
    deb = build_deb.build(tmp_path, "0.0.1~test")
    data = deb.read_bytes()
    pos = data.index(b"data.tar.xz")
    size = int(data[pos + 48:pos + 58])
    with tarfile.open(fileobj=io.BytesIO(lzma.decompress(data[pos + 60:pos + 60 + size]))) as tar:
        names = set(tar.getnames())
        assert {f"./{build_deb.MANUAL_DST}/{lang}/{p}.html" for lang in manual.LANGS for p in manual.PAGES} <= names
        html = tar.extractfile(f"./{build_deb.MANUAL_DST}/ru/build.html").read().decode()
    assert f"Версия ПО {semver(__version__)}" in html


def test_manual_path_survives_image_dpkg_excludes():
    """Образ (mmdebstrap) ставит пакеты с path-exclude=/usr/share/doc/* (кроме copyright): в doc/ руководство
    исчезало (ota-90b9b9c, 2026-11-01). Путь — вне исключений, а verify_image проверяет страницы на диске."""
    import build_deb
    import verify_image as vi
    assert not build_deb.MANUAL_DST.startswith(("usr/share/doc/", "usr/share/man/", "usr/share/locale/"))
    build = '{"commit": "90b9b9c7c4faa4a3"}'
    pages = {f"/{build_deb.MANUAL_DST}/{lang}/{p}.html": "<p>Версия ПО 0.1.0-dev.0 · 90b9b9c7c4fa</p>"
             for lang in manual.LANGS for p in manual.PAGES}

    def run(files):
        class Root:
            cat = staticmethod(lambda p: files.get(p, ""))
            exists = staticmethod(lambda p: p in files)
        rep = vi.Report()
        vi.manual_checks(rep, Root)
        return rep.checks[0]["status"]
    assert run({"/usr/share/sheregesh/build.json": build, **pages}) == "PASS"
    assert run({"/usr/share/sheregesh/build.json": build}) == "FAIL"                       # каталоги есть, файлов нет
    assert run({"/usr/share/sheregesh/build.json": '{"commit": "deadbeef"}', **pages}) == "FAIL"

