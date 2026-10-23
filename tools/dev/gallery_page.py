# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Страница визуальной приёмки из результата gallery.py: python3 tools/dev/gallery_page.py DIR [--defects FILE.json]

Пишет DIR/index.html (снимки рядом, относительными ссылками).
"""
import argparse
import html
import json
from pathlib import Path

TEMPLATE = r"""<title>Визуальная приёмка S1.5</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Раскладка: строка сводки, панель фильтров, сетка кадров 16:10 (как экран 1920×1200 пульта). */
:root {
  --bg: #f3f5f7; --surface: #ffffff; --fg: #1a2027; --muted: #5b6672; --line: #d5dbe1;
  --accent: #1f5fbf; --ok: #1a7f37; --warn: #9a6700; --crit: #c4232b;
  --sans: "IBM Plex Sans", "Segoe UI", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "DejaVu Sans Mono", monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #12161b; --surface: #1b2128; --fg: #e4e8ec; --muted: #98a3ae; --line: #2e3740;
  --accent: #6ea8ff; --ok: #4cc26a; --warn: #e0a83a; --crit: #ff6b6b; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #12161b; --surface: #1b2128; --fg: #e4e8ec; --muted: #98a3ae; --line: #2e3740;
  --accent: #6ea8ff; --ok: #4cc26a; --warn: #e0a83a; --crit: #ff6b6b; color-scheme: dark }
body { background: var(--bg); color: var(--fg); font-family: var(--sans); font-size: 15px; line-height: 1.5; }
.wrap { max-width: 1280px; margin: 0 auto; padding-block: 28px 48px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: grid; gap: 24px; }
h1 { font-size: 1.6rem; font-weight: 600; margin: 0; text-wrap: balance; }
h2 { font-size: 1.05rem; font-weight: 600; margin: 0; }
.lede { color: var(--muted); max-width: 70ch; margin: 0; }
.facts { display: flex; flex-wrap: wrap; gap: 8px 20px; font-family: var(--mono); font-size: 0.85rem; color: var(--muted); }
.facts b { color: var(--fg); font-weight: 500; }
.defects { background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 16px 18px; display: grid; gap: 10px; }
.defects ol { margin: 0; padding-left: 1.3em; display: grid; gap: 6px; max-width: 95ch; }
.defects li span { font-family: var(--mono); font-size: 0.78rem; color: var(--ok); text-transform: uppercase; letter-spacing: 0.04em; margin-left: 6px; }
.filters { display: grid; gap: 10px; position: sticky; top: env(safe-area-inset-top, 0px); z-index: 5; background: var(--bg); padding-block: 10px; border-bottom: 1px solid var(--line); }
body::before { content: ""; position: fixed; inset: 0 0 auto; height: env(safe-area-inset-top, 0px); z-index: 10; background: var(--bg); }
.row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.row > span { width: 6.5rem; font-size: 0.78rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
.chip { font: inherit; font-size: 0.85rem; border: 1px solid var(--line); background: var(--surface); color: var(--fg);
  border-radius: 999px; padding: 3px 12px; cursor: pointer; }
.chip[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: var(--bg); }
.chip:focus-visible, .shot:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.count { font-family: var(--mono); font-size: 0.85rem; color: var(--muted); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }
.shot { display: grid; gap: 6px; text-align: left; background: none; border: 0; padding: 0; color: inherit; font: inherit; cursor: zoom-in; min-width: 0; }
.shot img { width: 100%; aspect-ratio: 16 / 10; object-fit: cover; border: 1px solid var(--line); border-radius: 4px; background: var(--surface); max-width: 100%; }
.cap { display: flex; flex-wrap: wrap; gap: 4px 8px; font-size: 0.8rem; color: var(--muted); }
.cap b { color: var(--fg); font-weight: 500; }
.pill { font-family: var(--mono); font-size: 0.72rem; padding: 0 6px; border-radius: 3px; border: 1px solid currentColor; }
.pill.normal { color: var(--ok); } .pill.warning, .pill.no-video, .pill.no-peer { color: var(--warn); } .pill.critical { color: var(--crit); }
dialog { border: 1px solid var(--line); background: var(--surface); color: var(--fg); padding: 12px; border-radius: 6px; max-width: min(96vw, 1920px); }
dialog img { width: 100%; height: auto; display: block; }
dialog::backdrop { background: rgb(0 0 0 / 0.7); }
dialog .bar { display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 8px; font-size: 0.85rem; flex-wrap: wrap; }
@media (prefers-reduced-motion: reduce) { * { scroll-behavior: auto !important; } }
</style>
<div class="wrap">
  <header style="display:grid; gap:10px">
    <h1>Визуальная приёмка «Шерегеш» S1.5</h1>
    <p class="lede">Реальные снимки GUI пульта 1920×1200, снятые без дисплея (offscreen, программный рендерер) при работающем отдельном процессе-эмуляторе удалённого блока и синтетическом видео. Только эмуляция: оборудование не подключалось.</p>
    <div class="facts" id="facts"></div>
  </header>
  <section class="defects" aria-labelledby="dh">
    <h2 id="dh">Найденные визуальные дефекты</h2>
    <ol id="defects"></ol>
  </section>
  <section class="filters" aria-label="Фильтры" id="filters"></section>
  <div class="count" id="count"></div>
  <div class="grid" id="grid"></div>
</div>
<dialog id="viewer"><div class="bar"><span id="vcap"></span><button class="chip" id="vclose">Закрыть</button></div><img id="vimg" alt=""></dialog>
<script>
const ITEMS = __ITEMS__;
const DEFECTS = __DEFECTS__;
const META = __META__;
const LABELS = {
  state: {normal: "штатное", warning: "предупреждение", critical: "критическая ошибка", "no-peer": "нет второго процесса", "no-video": "нет видео"},
  lang: {ru: "RU", en: "EN"}, theme: {dark: "тёмная", light: "светлая"},
  page: {dashboard: "главная", video: "видео", diagnostics: "диагностика", power: "питание", logs: "журналы", settings: "настройки", about: "о системе"},
};
const DIMS = [["state", "Состояние"], ["lang", "Язык"], ["theme", "Тема"], ["scale", "Масштаб"], ["page", "Страница"]];
const sel = {state: "normal", lang: "ru", theme: "dark", scale: "100", page: "*"};
const label = (d, v) => (LABELS[d] && LABELS[d][v]) || (d === "scale" ? v + " %" : v);
function values(d) { return [...new Set(ITEMS.map(i => String(i[d])))]; }
function renderFacts() {
  const f = document.getElementById("facts");
  f.innerHTML = [["снимков", ITEMS.length], ["коммит", META.commit], ["разрешение", "1920×1200"], ["снято", META.generated]]
    .map(([k, v]) => `<span>${k}: <b>${v}</b></span>`).join("");
  document.getElementById("defects").innerHTML = DEFECTS.map(d => `<li>${d.text}<span>${d.status}</span></li>`).join("");
}
function renderFilters() {
  const box = document.getElementById("filters");
  box.innerHTML = DIMS.map(([d, t]) => {
    const vals = ["*", ...values(d)];
    return `<div class="row" role="group" aria-label="${t}"><span>${t}</span>` + vals.map(v =>
      `<button class="chip" data-d="${d}" data-v="${v}" aria-pressed="${sel[d] === v}">${v === "*" ? "все" : label(d, v)}</button>`).join("") + `</div>`;
  }).join("");
  box.querySelectorAll(".chip").forEach(b => b.addEventListener("click", () => { sel[b.dataset.d] = b.dataset.v; renderFilters(); renderGrid(); }));
}
function renderGrid() {
  const list = ITEMS.filter(i => DIMS.every(([d]) => sel[d] === "*" || String(i[d]) === sel[d]));
  document.getElementById("count").textContent = list.length ? `Показано ${list.length} из ${ITEMS.length}` : "Нет снимков для выбранного сочетания: для нештатных состояний сняты масштабы 100 и 150 % и профильные страницы.";
  document.getElementById("grid").innerHTML = list.map(i => `<button class="shot" data-f="${i.file}">
      <img src="${i.file}" alt="${label("page", i.page)}, ${label("state", i.state)}, ${i.lang}, ${label("theme", i.theme)}, ${i.scale} %" loading="lazy" width="1920" height="1200">
      <span class="cap"><span class="pill ${i.state}">${label("state", i.state)}</span><b>${label("page", i.page)}</b><span>${label("lang", i.lang)} · ${label("theme", i.theme)} · ${i.scale} %</span></span>
    </button>`).join("");
  document.querySelectorAll(".shot").forEach(b => b.addEventListener("click", () => {
    const i = ITEMS.find(x => x.file === b.dataset.f);
    document.getElementById("vimg").src = i.file;
    document.getElementById("vcap").textContent = `${label("page", i.page)} · ${label("state", i.state)} · ${label("lang", i.lang)} · ${label("theme", i.theme)} · ${i.scale} % · связь: ${i.peer}, видео: ${i.video}`;
    document.getElementById("viewer").showModal();
  }));
}
document.getElementById("vclose").addEventListener("click", () => document.getElementById("viewer").close());
document.getElementById("viewer").addEventListener("click", e => { if (e.target.id === "viewer") e.target.close(); });
renderFacts(); renderFilters(); renderGrid();
</script>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--defects")
    ap.add_argument("--commit", default="—")
    a = ap.parse_args()
    d = Path(a.dir)
    items = json.loads((d / "index.json").read_text(encoding="utf-8"))
    defects = json.loads(Path(a.defects).read_text(encoding="utf-8")) if a.defects else []
    for x in defects:
        x["text"] = html.escape(x["text"])
    import time
    meta = {"commit": a.commit, "generated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}
    page = (TEMPLATE.replace("__ITEMS__", json.dumps(items, ensure_ascii=False))
            .replace("__DEFECTS__", json.dumps(defects, ensure_ascii=False))
            .replace("__META__", json.dumps(meta, ensure_ascii=False)))
    (d / "index.html").write_text(page, encoding="utf-8")
    print(d / "index.html")


if __name__ == "__main__":
    main()
