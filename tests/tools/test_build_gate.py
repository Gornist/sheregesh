# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""ADR-0018: образ «Шерегеша» собирает только shbuild, на железо — только артефакты задания PASSED.
docker-build.sh без задания shbuild отказывает; shdeploy ota-build и flash_sd.py — без отчёта PASSED."""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools/build"), str(ROOT / "tools/deploy")]
import gate  # noqa: E402

_spec = importlib.util.spec_from_file_location("shdeploy_gate", ROOT / "tools/deploy/shdeploy.py")
shdeploy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shdeploy)

STAGES = [{"name": n, "status": "PASS"} for n in gate.REQUIRED]


class BuildGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, **kw):
        rep = {"id": "j1", "commit": "c" * 40, "state": "PASSED", "stages": STAGES, **kw}
        (self.d / "shbuild-report.json").write_text(json.dumps(rep))

    def test_passed_report_is_required(self):
        with self.assertRaisesRegex(gate.GateError, "только shbuild"):
            gate.require_passed(self.d)
        self.report()
        self.assertEqual(gate.require_passed(self.d, "c" * 40)["id"], "j1")

    def test_failed_job_missing_stage_and_other_commit_refused(self):
        self.report(state="FAILED")
        with self.assertRaisesRegex(gate.GateError, "только PASSED"):
            gate.require_passed(self.d)
        self.report(stages=[s for s in STAGES if s["name"] != "boot"])  # без загрузочного теста — нельзя
        with self.assertRaisesRegex(gate.GateError, "boot"):
            gate.require_passed(self.d)
        self.report()
        with self.assertRaisesRegex(gate.GateError, "коммит"):
            gate.require_passed(self.d, "d" * 40)

    def test_rule_applies_to_sheregesh_images_only(self):
        self.assertTrue(gate.is_sheregesh_image("x/sheregesh-lab-dev-pi4.img.zst"))
        self.assertTrue(gate.is_sheregesh_image("x/any.img.zst", {"config": "sheregesh-lab-dev-pi4"}))
        self.assertFalse(gate.is_sheregesh_image("x/pi3.img.zst", {"image": "pi3"}))   # карта камеры pi3

    def test_docker_build_refuses_without_shbuild_job(self):
        env = {k: v for k, v in os.environ.items() if k != "SHBUILD_JOB"}
        r = subprocess.run(["bash", str(ROOT / "os/docker-build.sh"), "sheregesh-lab-dev-pi4"], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertIn("shbuild.py submit", r.stderr)
        self.assertIn("SHBUILD_JOB=self.job.id", (ROOT / "tools/build/shbuild.py").read_text())

    def test_ota_build_refuses_without_shbuild_report(self):
        (self.d / "build-report.json").write_text(json.dumps({"commit": "c" * 40, "source_date_epoch": 1,
                                                             "config": "sheregesh-lab-dev-pi4"}))
        a = argparse.Namespace(image=self.d / "x.img.zst", build_report=str(self.d / "build-report.json"),
                               out=self.d / "o", key=None)
        with self.assertRaisesRegex(shdeploy.CliError, "shbuild"):
            shdeploy.cmd_ota_build(a)
        with self.assertRaisesRegex(shdeploy.CliError, "build-report"):
            shdeploy.cmd_ota_build(argparse.Namespace(build_report=None))

    def test_flash_sd_checks_gate_before_writing(self):
        text = (ROOT / "tools/image/flash_sd.py").read_text()
        head = text[text.index("def main("):]
        # проверка — до запуска записи под sudo (--write-as-root в конце main)
        self.assertLess(head.index("build_gate.require_passed"), head.rindex("--write-as-root"))


if __name__ == "__main__":
    unittest.main()
