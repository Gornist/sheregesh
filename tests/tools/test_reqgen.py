# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Тесты единого реестра требований и гейта релиза (TEST-REL-001, TEST-REL-002, TEST-REL-009)."""
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/reqs"))
import reqgen  # noqa: E402


def _pass_all(doc):
    for t in doc["tests"]:
        t.update(status="PASS", evidence_level=t["level_required"], evidence_link="release/evidence/x",
                 sw_version="1.0.0", hw_rev="HW-1.0 Rev A", executed="2026-12-01", executor="lab")
    return doc


class RegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = reqgen.load()
        cls.baseline = json.loads((ROOT / cls.doc["document"]["baseline"]).read_text(encoding="utf-8"))

    def doc_copy(self):
        return copy.deepcopy(self.doc)

    def test_repository_registry_is_consistent(self):
        self.assertEqual(reqgen.validate(self.doc, self.baseline), [])

    def test_every_baseline_requirement_is_kept(self):
        ids = {r["id"] for r in self.doc["requirements"]}
        self.assertTrue({b["id"] for b in self.baseline} <= ids)

    def test_every_must_has_objective_criteria_and_test(self):
        for r in self.doc["requirements"]:
            if r["priority"] == "Must":
                self.assertTrue(r["pass_criteria"], r["id"])
                self.assertTrue(r["tests"], r["id"])

    def test_generated_files_are_current(self):
        for path, text in reqgen.outputs(self.doc).items():
            self.assertEqual(path.read_text(encoding="utf-8"), text, f"{path} устарел")

    def test_gate_is_no_go_while_not_tested(self):
        self.assertTrue(reqgen.gate(self.doc))

    def test_gate_go_only_when_all_must_pass(self):
        self.assertEqual(reqgen.gate(_pass_all(self.doc_copy())), [])

    def test_gate_rejects_fail_missing_evidence_and_low_level(self):
        for mutate in (lambda t: t.update(status="FAIL"),
                       lambda t: t.update(evidence_link=None),
                       lambda t: t.update(evidence_level="L3")):
            doc = _pass_all(self.doc_copy())
            mutate(next(t for t in doc["tests"] if t["id"] == "TEST-CFG-002"))
            self.assertTrue(reqgen.gate(doc))

    def test_gate_ignores_should(self):
        doc = _pass_all(self.doc_copy())
        next(t for t in doc["tests"] if t["id"] == "TEST-P-007").update(status="FAIL")
        self.assertEqual(reqgen.gate(doc), [])

    def test_pass_without_evidence_fields_is_invalid(self):
        doc = self.doc_copy()
        doc["tests"][0]["status"] = "PASS"
        self.assertTrue(any("PASS без поля" in e for e in reqgen.validate(doc)))

    def test_silent_priority_change_is_rejected(self):
        doc = self.doc_copy()
        r = next(r for r in doc["requirements"] if r["id"] == "REQ-R-003")
        r["priority"], r["changes"] = "Should", ["CHG-006"]
        self.assertTrue(any("без записи об изменении" in e for e in reqgen.validate(doc, self.baseline)))

    def test_retired_test_id_cannot_be_reused(self):
        doc = self.doc_copy()
        doc["tests"][0]["id"] = "TEST-PWR-003"
        self.assertTrue(any("выведен из употребления" in e for e in reqgen.validate(doc)))

    def test_dangling_references_are_rejected(self):
        doc = self.doc_copy()
        doc["requirements"][0]["tests"] = ["TEST-NOPE-001"]
        self.assertTrue(any("несуществующий тест" in e for e in reqgen.validate(doc)))


if __name__ == "__main__":
    unittest.main()
