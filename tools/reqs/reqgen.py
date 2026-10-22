#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Единый реестр требований «Шерегеш»: проверка, генерация производных файлов, гейт релиза.

Источник: docs/requirements/requirements.json. Производные файлы не редактируются вручную.

  reqgen.py validate            — проверить целостность реестра
  reqgen.py generate [--check]  — сгенерировать (или проверить актуальность) производные файлы
  reqgen.py gate                — GO/NO-GO по REQ-REL-001 (код возврата 0 = GO)
"""
import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "docs/requirements/requirements.json"
OUT_MATRIX = ROOT / "docs/requirements/TEST-MATRIX.csv"
OUT_SPEC = ROOT / "docs/tests/TEST-SPEC.md"
OUT_CHANGELOG = ROOT / "docs/requirements/CHANGELOG-REQ.md"
SRS = ROOT / "docs/srs/SHEREGESH-SRS-1.0.md"
BEGIN, END = "<!-- BEGIN GENERATED:requirements -->", "<!-- END GENERATED:requirements -->"

PRIORITIES = ("Must", "Should", "Could", "Won't")
LEVELS = ("L0", "L1", "L2", "L3", "L4", "L5")
REQ_ID = re.compile(r"^REQ-[A-Z]+-\d{3}$")
TEST_ID = re.compile(r"^TEST-[A-Z]+-[A-Z0-9]+$")
# Общие изменения, которые не меняют смысл требования (критерии, нумерация тестов, источник).
GENERIC_CHANGES = {"CHG-006", "CHG-007", "CHG-008"}


def load(path=SOURCE):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate(doc, baseline=None):
    """Возвращает список ошибок; пустой список — реестр согласован."""
    errors = []
    err = errors.append
    reqs = {r["id"]: r for r in doc["requirements"]}
    tests = {t["id"]: t for t in doc["tests"]}
    changes = {c["id"] for c in doc["changes"]}
    retired = {t["id"] for t in doc.get("retired_tests", [])}
    statuses = set(doc["statuses"])
    methods = set(doc["methods"])

    if len(reqs) != len(doc["requirements"]):
        err("повторяющиеся REQ-ID")
    if len(tests) != len(doc["tests"]):
        err("повторяющиеся TEST-ID")

    for r in doc["requirements"]:
        rid = r["id"]
        if not REQ_ID.match(rid):
            err(f"{rid}: неверный формат REQ-ID")
        if r["priority"] not in PRIORITIES:
            err(f"{rid}: неизвестный приоритет {r['priority']!r}")
        if not r.get("requirement", "").strip():
            err(f"{rid}: пустой текст требования")
        if not r.get("pass_criteria") or not all(c.strip() for c in r["pass_criteria"]):
            err(f"{rid}: нет объективных критериев PASS")
        if r["priority"] == "Must" and not r.get("tests"):
            err(f"{rid}: Must без теста")
        for tid in r.get("tests", []):
            if tid not in tests:
                err(f"{rid}: ссылка на несуществующий тест {tid}")
            elif rid not in tests[tid]["covers"]:
                err(f"{rid}: тест {tid} не указывает это требование в covers")
        for c in r.get("changes", []):
            if c not in changes:
                err(f"{rid}: ссылка на несуществующее изменение {c}")

    for t in doc["tests"]:
        tid = t["id"]
        if not TEST_ID.match(tid):
            err(f"{tid}: неверный формат TEST-ID")
        if tid in retired:
            err(f"{tid}: идентификатор выведен из употребления, но используется")
        if not t.get("covers"):
            err(f"{tid}: тест ничего не покрывает")
        for rid in t.get("covers", []):
            if rid not in reqs:
                err(f"{tid}: покрывает несуществующее требование {rid}")
            elif tid not in reqs[rid]["tests"]:
                err(f"{tid}: требование {rid} не ссылается на тест")
        if not t.get("method") or not set(t["method"]) <= methods:
            err(f"{tid}: неизвестный метод {t.get('method')}")
        for field in ("equipment", "evidence", "procedure", "pass_criteria"):
            if not t.get(field):
                err(f"{tid}: пустое поле {field}")
        if t["level_required"] not in LEVELS or t["evidence_level"] not in LEVELS:
            err(f"{tid}: неизвестный уровень доказательства")
        if t["status"] not in statuses:
            err(f"{tid}: неизвестный статус {t['status']!r}")
        if t["status"] == "PASS":
            for field in ("evidence_link", "sw_version", "hw_rev", "executed", "executor"):
                if not t.get(field):
                    err(f"{tid}: PASS без поля {field}")

    for c in doc["changes"]:
        for rid in c.get("affects", []):
            if rid != "*" and rid not in reqs:
                err(f"{c['id']}: затрагивает несуществующее требование {rid}")
        for tid in c.get("tests", []):
            if tid != "*" and tid not in tests:
                err(f"{c['id']}: затрагивает несуществующий тест {tid}")

    # REQ-REL-009: любое отличие от baseline Rev A прослеживается до содержательного изменения.
    if baseline is not None:
        base = {b["id"]: b for b in baseline}
        for rid, r in reqs.items():
            b = base.get(rid)
            specific = set(r.get("changes", [])) - GENERIC_CHANGES
            if b is None:
                if not specific:
                    err(f"{rid}: новое требование без записи об изменении")
                continue
            ra = r.get("rev_a", {})
            if ra.get("requirement") != b["requirement"] or ra.get("acceptance") != b["acceptance"]:
                err(f"{rid}: поле rev_a не совпадает с baseline")
            if (r["requirement"] != b["requirement"] or r["priority"] != b["priority"]) and not specific:
                err(f"{rid}: текст или приоритет изменён без записи об изменении")
    return errors


def level_ok(have, need):
    return LEVELS.index(have) >= LEVELS.index(need)


def gate(doc):
    """REQ-REL-001: GO только если каждый Must имеет PASS по всем тестам на требуемом уровне."""
    tests = {t["id"]: t for t in doc["tests"]}
    blockers = []
    for r in doc["requirements"]:
        if r["priority"] != "Must":
            continue
        for tid in r["tests"]:
            t = tests[tid]
            if t["status"] != "PASS":
                blockers.append(f"{r['id']} / {tid}: {t['status']}")
            elif not t.get("evidence_link"):
                blockers.append(f"{r['id']} / {tid}: PASS без ссылки на доказательство")
            elif not level_ok(t["evidence_level"], t["level_required"]):
                blockers.append(f"{r['id']} / {tid}: уровень {t['evidence_level']} < {t['level_required']}")
    return blockers


def _cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_matrix(doc):
    reqs = doc["requirements"]
    tests = {t["id"]: t for t in doc["tests"]}
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(["REQ-ID", "TEST-ID", "Раздел", "Приоритет", "Устройство", "Требование", "Критерии PASS",
                "Метод", "Требуемый уровень", "Статус", "Уровень доказательства", "Версия ПО",
                "Ревизия HW", "Ссылка на протокол", "Изменения"])
    for r in reqs:
        for tid in r["tests"]:
            t = tests[tid]
            w.writerow([r["id"], tid, r["group"], r["priority"], r["device"], r["requirement"],
                        " • ".join(r["pass_criteria"]), "+".join(t["method"]), t["level_required"],
                        t["status"], t["evidence_level"], t["sw_version"] or "TBD", t["hw_rev"] or "TBD",
                        t["evidence_link"] or "TBD", ", ".join(r["changes"])])
    return "\ufeff" + buf.getvalue()


def render_srs_block(doc):
    out = [BEGIN, "",
           "_Раздел сгенерирован `tools/reqs/reqgen.py` из `docs/requirements/requirements.json`. Не редактировать вручную._", ""]
    groups = []
    for r in doc["requirements"]:
        if r["group"] not in groups:
            groups.append(r["group"])
    for n, g in enumerate(groups, 1):
        out += [f"### 4.{n}. {g}", "",
                "| REQ-ID | Приоритет | Устройство | Требование | Критерии PASS | TEST-ID | Изменения |",
                "|---|---|---|---|---|---|---|"]
        for r in doc["requirements"]:
            if r["group"] != g:
                continue
            crit = "<br>".join(f"{i}) {_cell(c)}" for i, c in enumerate(r["pass_criteria"], 1))
            out.append(f"| `{r['id']}` | {r['priority']} | {r['device']} | {_cell(r['requirement'])} | {crit} | "
                       f"{', '.join(r['tests'])} | {', '.join(r['changes'])} |")
        out.append("")
    out.append(END)
    return "\n".join(out)


def render_spec(doc):
    reqs = {r["id"]: r for r in doc["requirements"]}
    out = ["# «Шерегеш» 1.0 — спецификация испытаний (TEST-SPEC)", "",
           f"_Сгенерировано из `docs/requirements/requirements.json` (ред. {doc['document']['revision']}, "
           f"{doc['document']['date']}). Не редактировать вручную._", "",
           "Статус `NOT TESTED` означает отсутствие доказательства и не равен PASS. "
           "PASS требует ссылки на протокол, версии ПО, HW-версии, даты, исполнителя и уровня доказательства не ниже требуемого.", "",
           "## Методы", ""]
    out += [f"- **{k}** — {v}" for k, v in doc["methods"].items()]
    out += ["", "## Уровни доказательства", ""]
    out += [f"- **{k}** — {v}" for k, v in doc["evidence_levels"].items()]
    out += ["", "## Стенд и профили нагрузки", ""]
    for p in doc["profiles"]:
        out += [f"### {p['id']} — {p['title']}", "", p["text"], ""]
    out += ["## Испытания", ""]
    for t in doc["tests"]:
        out += [f"### {t['id']}", "",
                f"- **Покрывает:** {', '.join(t['covers'])}",
                f"- **Метод:** {'+'.join(t['method'])}; **требуемый уровень:** {t['level_required']}",
                f"- **Объект:** {', '.join(t['devices'])}",
                f"- **Оборудование / эмулятор:** {t['equipment']}",
                f"- **Доказательство:** {t['evidence']}",
                f"- **Статус:** {t['status']} ({t['evidence_level']})", "",
                "**Процедура**", ""]
        out += [f"{i}. {s}" for i, s in enumerate(t["procedure"], 1)]
        out += ["", "**Критерии PASS**", ""]
        out += [f"- {c}" for c in t["pass_criteria"]]
        for rid in t["covers"]:
            out.append(f"- `{rid}` ({reqs[rid]['priority']}):")
            out += [f"  - {c}" for c in reqs[rid]["pass_criteria"]]
        out.append("")
    if doc.get("retired_tests"):
        out += ["## Выведенные из употребления идентификаторы", "", "| Было | Стало | Изменение | Примечание |", "|---|---|---|---|"]
        for t in doc["retired_tests"]:
            out.append(f"| {t['id']} | {', '.join(t['replaced_by']) or '—'} | {t['change']} | {t.get('note', '')} |")
        out.append("")
    return "\n".join(out)


def render_changelog(doc):
    out = ["# Журнал изменений требований", "",
           "_Сгенерировано из раздела `changes` реестра требований. Не редактировать вручную._", "",
           f"Baseline: `{doc['document']['baseline']}` (Rev A). Текущая редакция: {doc['document']['revision']} от {doc['document']['date']}.", ""]
    for c in doc["changes"]:
        out += [f"## {c['id']} — {c['title']}", "",
                f"- **Дата:** {c['date']}; **основание:** {c['decision']}",
                f"- **Затронутые REQ:** {', '.join(c['affects']) or '—'}",
                f"- **Затронутые TEST:** {', '.join(c['tests']) or '—'}", "", c["description"], ""]
    changed = [r for r in doc["requirements"] if "rev_a" not in r or r["requirement"] != r["rev_a"]["requirement"]]
    out += ["## Требования с изменённым текстом", "", "| REQ-ID | Rev A | Rev " + doc["document"]["revision"] + " | Изменения |", "|---|---|---|---|"]
    for r in changed:
        old = r.get("rev_a", {}).get("requirement", "— (новое)")
        out.append(f"| `{r['id']}` | {_cell(old)} | {_cell(r['requirement'])} | {', '.join(r['changes'])} |")
    out.append("")
    return "\n".join(out)


def outputs(doc):
    srs_text = SRS.read_text(encoding="utf-8")
    if BEGIN not in srs_text or END not in srs_text:
        raise SystemExit(f"{SRS}: нет маркеров генерации")
    head, rest = srs_text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    return {
        OUT_MATRIX: render_matrix(doc),
        OUT_SPEC: render_spec(doc),
        OUT_CHANGELOG: render_changelog(doc),
        SRS: head + render_srs_block(doc) + tail,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    g = sub.add_parser("generate")
    g.add_argument("--check", action="store_true", help="не писать файлы, только проверить актуальность")
    sub.add_parser("gate")
    args = ap.parse_args(argv)

    doc = load()
    baseline = json.loads((ROOT / doc["document"]["baseline"]).read_text(encoding="utf-8"))
    errors = validate(doc, baseline)
    if errors:
        print("Реестр не согласован:", *errors, sep="\n  ", file=sys.stderr)
        return 2
    if args.cmd == "validate":
        must = sum(r["priority"] == "Must" for r in doc["requirements"])
        print(f"OK: {len(doc['requirements'])} REQ ({must} Must), {len(doc['tests'])} TEST")
        return 0
    if args.cmd == "generate":
        stale = []
        for path, text in outputs(doc).items():
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current != text:
                stale.append(path.relative_to(ROOT))
                if not args.check:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
        if args.check and stale:
            print("Производные файлы устарели, выполните `tools/reqs/reqgen.py generate`:",
                  *map(str, stale), sep="\n  ", file=sys.stderr)
            return 1
        print("Актуально" if not stale else "Обновлено: " + ", ".join(map(str, stale)))
        return 0
    blockers = gate(doc)
    if blockers:
        print(f"NO-GO: {len(blockers)} незакрытых проверок Must", *blockers[:50], sep="\n  ")
        return 1
    print("GO: все Must имеют PASS с доказательствами на требуемом уровне")
    return 0


if __name__ == "__main__":
    sys.exit(main())
