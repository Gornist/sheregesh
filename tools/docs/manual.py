#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Руководство пользователя офлайн (REQ-DOC-004): docs/user/{ru,en}/*.md → HTML с версией ПО на каждой странице.

  python3 tools/docs/manual.py --out DIR [--version SEMVER] [--commit SHA]   собрать DIR/{ru,en}/*.html и DIR/index.html
  python3 tools/docs/manual.py --check                                      языки совпадают по страницам и разделам

Пакет sheregesh кладёт результат в /usr/share/sheregesh/manual (tools/image/build_deb.py; /usr/share/doc образ
исключает правилом dpkg path-exclude mmdebstrap): версия документации в
образе совпадает с версией ПО. Только стандартная библиотека; разметка — то подмножество Markdown, что используется
в docs/user: заголовки, абзацы, списки (с вложенностью), блоки кода, `код`, **жирный**.
"""
import argparse
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "docs/user"
LANGS = ("ru", "en")
PAGES = ("build", "support", "recovery", "faq")
TITLE = {"ru": "«Шерегеш» — руководство", "en": "Sheregesh manual"}
VERSION_LABEL = {"ru": "Версия ПО", "en": "Software version"}
STYLE = ("body{font-family:sans-serif;max-width:52em;margin:2em auto;padding:0 1em;line-height:1.5}"
         "pre{background:#f3f3f3;padding:.6em;overflow-x:auto}code{background:#f3f3f3}"
         ".ver{color:#666;font-size:.9em}nav a{margin-right:1em}")


def inline(text):
    out, parts = [], re.split(r"(`[^`]+`)", text)
    for p in parts:
        if p.startswith("`") and p.endswith("`") and len(p) > 1:
            out.append(f"<code>{html.escape(p[1:-1])}</code>")
        else:
            out.append(re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(p, quote=False)))
    return "".join(out)


def to_html(md: str) -> str:
    """Markdown подмножества docs/user → фрагмент HTML."""
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    out, para, lists = [], [], []          # lists — стек (отступ, тег)
    lines = md.splitlines()
    i = 0

    def flush():
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    def close_lists(indent=-1):
        while lists and lists[-1][0] > indent:
            out.append(f"</li></{lists.pop()[1]}>")
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            pad = min((len(c) - len(c.lstrip()) for c in code if c.strip()), default=0)
            out.append("<pre><code>" + html.escape("\n".join(c[pad:] for c in code)) + "</code></pre>")
            i += 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            flush()
            close_lists()
            n = len(m.group(1))
            out.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
        elif re.match(r"^\s*([-*]|\d+\.)\s+", line):
            flush()
            indent = len(line) - len(line.lstrip())
            tag = "ol" if re.match(r"^\s*\d+\.", line) else "ul"
            text = re.sub(r"^\s*([-*]|\d+\.)\s+", "", line)
            if lists and indent == lists[-1][0]:
                out.append("</li><li>")
            elif not lists or indent > lists[-1][0]:
                out.append(f"<{tag}><li>")
                lists.append((indent, tag))
            else:
                close_lists(indent)
                out.append("</li><li>")
            out.append(inline(text))
        elif not stripped:
            flush()
        elif lists and line.startswith(" "):
            out.append(" " + inline(stripped))            # продолжение пункта списка
        else:
            close_lists()
            para.append(stripped)
        i += 1
    flush()
    close_lists()
    return "\n".join(out)


def headings(md: str):
    return [len(m.group(1)) for m in re.finditer(r"^(#{1,4})\s", re.sub(r"```.*?```", "", md, flags=re.S), re.M)]


def check():
    """Ошибки соответствия языков: тот же набор страниц и та же структура заголовков."""
    err = []
    for page in PAGES:
        files = {lang: SRC / lang / f"{page}.md" for lang in LANGS}
        missing = [str(f.relative_to(ROOT)) for f in files.values() if not f.exists()]
        if missing:
            err.append(f"нет {', '.join(missing)}")
            continue
        h = {lang: headings(f.read_text(encoding="utf-8")) for lang, f in files.items()}
        if len({tuple(v) for v in h.values()}) != 1:
            err.append(f"{page}: разделы различаются по языкам {h}")
    return err


def page(lang, name, body, version, commit):
    title = re.search(r"<h1>(.*?)</h1>", body)
    nav = " ".join(f'<a href="{p}.html">{p}</a>' for p in PAGES)
    other = [x for x in LANGS if x != lang][0]
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width"><title>{title.group(1) if title else name} — {TITLE[lang]}</title>'
            f"<style>{STYLE}</style></head><body><nav>{nav} <a href=\"../{other}/{name}.html\">{other.upper()}</a>"
            f'</nav><p class="ver">{VERSION_LABEL[lang]} {html.escape(version)}'
            f"{' · ' + html.escape(commit[:12]) if commit else ''}</p>\n{body}\n</body></html>\n")


def build(out: Path, version: str, commit: str = ""):
    err = check()
    if err:
        raise SystemExit("руководство: " + "; ".join(err))
    files = []
    for lang in LANGS:
        d = out / lang
        d.mkdir(parents=True, exist_ok=True)
        for name in PAGES:
            body = to_html((SRC / lang / f"{name}.md").read_text(encoding="utf-8"))
            f = d / f"{name}.html"
            f.write_text(page(lang, name, body, version, commit), encoding="utf-8")
            files.append(f)
    links = "".join(f'<li>{TITLE[lang]}: ' + ", ".join(f'<a href="{lang}/{p}.html">{p}</a>' for p in PAGES) + "</li>"
                    for lang in LANGS)
    idx = out / "index.html"
    idx.write_text(f'<!doctype html><html><head><meta charset="utf-8"><title>Sheregesh {html.escape(version)}</title>'
                   f"<style>{STYLE}</style></head><body><h1>Sheregesh {html.escape(version)}</h1><ul>{links}</ul>"
                   "</body></html>\n", encoding="utf-8")
    return [idx, *files]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--version")
    ap.add_argument("--commit", default="")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    if a.check or not a.out:
        err = check()
        print("\n".join(err) or "руководство: языки совпадают")
        return 1 if err else 0
    sys.path.insert(0, str(ROOT / "software"))
    from sheregesh import __version__, semver
    for f in build(a.out, a.version or semver(__version__), a.commit):
        print(f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
