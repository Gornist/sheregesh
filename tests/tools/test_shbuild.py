# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""shbuild (ADR-0018): очередь, этапы, явные конечные состояния, журнал с временем, тайм-аут и отмена, хранение."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("shbuild", ROOT / "tools/build/shbuild.py")
sb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sb)


class Fake(sb.Pipeline):
    """Этапы без внешних команд: поведение задаётся словарём {этап: исключение или None}."""
    behaviour = {}

    def __init__(self, root, job):
        super().__init__(root, job)
        self.ran = []

    def _stage(name):
        def f(self):
            self.ran.append(name)
            exc = self.behaviour.get(name)
            if exc:
                raise exc
            if name == "build":
                self.art.mkdir(parents=True, exist_ok=True)
        return f

    for _n in sb.STAGES:
        locals()[_n] = _stage(_n)
    del _n


class ShbuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bundle = self.root / "inbox" / "b.bundle"
        self.bundle.parent.mkdir()
        self.bundle.write_text("x")

    def tearDown(self):
        self.tmp.cleanup()

    def job(self, commit="a" * 40):
        return sb.new_job(self.root, commit, self.bundle)

    def run_fake(self, behaviour, commit="a" * 40):
        Fake.behaviour = behaviour
        j = self.job(commit)
        p = Fake(self.root, j)
        return p, p.run(), j.load()

    def test_sbom_gate_requires_image_and_package_sbom_and_runs_checker(self):
        p = sb.Pipeline(self.root, self.job())
        calls = []
        p.sh = lambda stage, cmd, **kw: calls.append((stage, cmd))
        p.sbom_gate()                                                # ревизия без tools/release — правило не действует
        self.assertEqual(calls, [])
        tool = p.src / "tools/release/release.py"
        tool.parent.mkdir(parents=True)
        tool.write_text("")
        p.art.mkdir(parents=True)
        (p.art / "img.sbom.zst").write_text("x")
        with self.assertRaises(sb.Fail) as e:
            p.sbom_gate()
        self.assertIn("пакета", str(e.exception))
        (p.art / "sheregesh_1_all.deb.spdx.json").write_text("{}")
        p.sbom_gate()
        stage, cmd = calls[0]
        self.assertEqual((stage, cmd[3]), ("gates", "sbom-check"))
        self.assertEqual([Path(c).name for c in cmd[4:]], ["img.sbom.zst", "sheregesh_1_all.deb.spdx.json"])

    def test_queue_and_passed(self):
        j = self.job()
        self.assertEqual([q.id for q in sb.queued(self.root)], [j.id])
        p, state, st = self.run_fake({})
        self.assertEqual(state, "PASSED")
        self.assertEqual(p.ran, list(sb.STAGES))
        self.assertEqual([s["status"] for s in st["stages"]], ["PASS"] * len(sb.STAGES))
        rep = json.loads((self.root / "jobs" / st["id"] / "report.json").read_text())
        self.assertEqual(rep["state"], "PASSED")
        self.assertTrue((self.root / "artifacts" / ("a" * 40) / "shbuild-report.json").exists())

    def test_failure_stops_and_is_explicit(self):
        # провал этапа — явное состояние и причина, дальнейшие этапы не идут (обрыв сборки не «тишина»)
        (self.root / "host.conf").write_text("parallel_boot=0\n")           # последовательный режим: после gates boot не идёт
        p, state, st = self.run_fake({"gates": sb.Fail("owners_check")})
        self.assertEqual(state, "FAILED")
        self.assertEqual(p.ran, ["preflight", "checkout", "tests", "build", "gates"])
        self.assertEqual((st["failed_stage"], st["reason"]), ("gates", "owners_check"))
        self.assertIn("FAILED", sb.line(st))

    def test_environment_error_and_unexpected_exception(self):
        _, state, st = self.run_fake({"preflight": sb.Fail("мало места", "ERROR")})
        self.assertEqual((state, st["failed_stage"]), ("ERROR", "preflight"))
        _, state, st = self.run_fake({"boot": KeyError("x")}, commit="b" * 40)
        self.assertEqual(state, "ERROR")
        self.assertIn("KeyError", st["reason"])

    def test_timeout_and_cancel_states(self):
        _, state, _ = self.run_fake({"build": TimeoutError("тайм-аут")})
        self.assertEqual(state, "TIMEOUT")
        j = self.job("c" * 40)
        (j.dir / "cancel").touch()
        Fake.behaviour = {}
        self.assertEqual(Fake(self.root, j).run(), "CANCELLED")

    def test_failed_job_artifacts_removed_passed_kept(self):
        _, _, _ = self.run_fake({"boot": sb.Fail("hostname")})
        self.assertFalse((self.root / "artifacts" / ("a" * 40)).exists())
        self.assertFalse(self.bundle.exists())                    # пакет git задания удаляется всегда

    def test_retention_keeps_last_passed(self):
        for i in range(sb.KEEP_PASSED + 2):
            self.bundle.write_text("x")
            self.run_fake({}, commit=f"{i:040d}")
        kept = sorted(p.name for p in (self.root / "artifacts").iterdir())
        self.assertEqual(kept, [f"{i:040d}" for i in range(2, sb.KEEP_PASSED + 2)])

    def test_run_logged_timestamps_timeout_cancel(self):
        log = self.root / "s.log"
        rc = sb.run_logged([sys.executable, "-c", "print('a'); print('b')"], log, 30)
        self.assertEqual(rc, 0)
        lines = log.read_text().splitlines()
        self.assertTrue(lines[0].startswith("$ "))
        self.assertRegex(lines[1], r"^\[\s*\d+\.\d\] a$")
        with self.assertRaises(TimeoutError):
            sb.run_logged([sys.executable, "-c", "import time; time.sleep(30)"], log, 1)
        cancel = self.root / "cancel"
        cancel.touch()
        with self.assertRaises(InterruptedError):
            sb.run_logged([sys.executable, "-c", "import time; time.sleep(30)"], log, 30, cancel_file=cancel)

    def test_check_maps_exit_code(self):
        with self.assertRaises(sb.Fail) as e:
            sb.check([sys.executable, "-c", "raise SystemExit(3)"], self.root / "c.log", 30, kind="ERROR")
        self.assertEqual(e.exception.kind, "ERROR")

    def test_boot_check_scenarios_and_safety(self):
        # загрузочный тест: три сценария, настоящая машина (QEMU virt, не qemu-user в контейнере — там ложно падают
        # юниты с пространствами имён), контейнер без --privileged, проба выключает машину и пишет итог в консоль
        text = (ROOT / "tools/build/boot_check.sh").read_text()
        probe = (ROOT / "tools/build/vm/probe.sh").read_text()
        init = (ROOT / "tools/build/vm/init").read_text()
        self.assertEqual(sb.BOOT_SCENARIOS, ("console", "remote", "fresh", "offline"))
        self.assertNotIn("--privileged", text)
        self.assertIn('qemu-system-aarch64 -M "$QM"', text)
        self.assertIn("--device /dev/kvm", text)                 # KVM на arm64-хосте с /dev/kvm; --privileged по-прежнему нет
        self.assertIn("SHBOOT_NO_KVM", text)
        self.assertIn("readonly=on", text)                       # модули — только чтение; образ — через оверлей
        self.assertIn("qcow2 -F raw -b", text)
        for check in ("имя после продления DHCP", "имя в запросах DHCP", "источник времени", "упавшие юниты",
                      "sshd: root только по ключу", "ключ root = ключ sheregesh-ui", "/var засеян"):
            self.assertIn(check, text)
        self.assertIn("Type=simple", init)                        # иначе is-system-running --wait ждёт саму пробу
        self.assertTrue(probe.rstrip().endswith("systemctl poweroff"))
        self.assertIn("ENV{ID_PART_ENTRY_NAME}", init)
        self.assertIn("name=shboot", text)                        # итог пробы — отдельным каналом, не консолью
        self.assertIn("в итоге пробы нет строки", text)           # потерянная строка — провал            # by-slot по меткам GPT, корень образа не меняется
        self.assertIn('mount -o ro "$SYS" /newroot', init)            # корень образа — только чтение

    def test_buildhost_profile_artifacts_gates_and_boot(self):
        # ADR-0019: образ сборочного хоста — свой каталог артефактов, свой загрузочный сценарий, без owners/order
        # (свойства A/B-образа «Шерегеша») и без SBOM пакета sheregesh
        cfg = "sheregesh-buildhost-pi5"
        self.assertIn(cfg, sb.CONFIGS)
        sha = "b" * 40
        j = sb.new_job(self.root, sha, self.bundle, {"config": cfg})
        self.assertEqual(sb.art_name(j.load()), f"{sha}.{cfg}")
        self.assertEqual(sb.art_name(self.job(sha).load()), sha)             # продуктовый образ — как было
        p = sb.Pipeline(self.root, j)
        self.assertTrue(p.buildhost)
        self.assertEqual(p.art, self.root / "artifacts" / f"{sha}.{cfg}")
        calls = []
        p.sh = lambda stage, cmd, **kw: calls.append((stage, cmd))
        tool = p.src / "tools/release/release.py"
        tool.parent.mkdir(parents=True)
        tool.write_text("")
        p.art.mkdir(parents=True)
        with self.assertRaises(sb.Fail):
            p.sbom_gate()                                                    # SBOM образа обязателен
        (p.art / "img.sbom.zst").write_text("x")
        p.sbom_gate()                                                        # пакета sheregesh не требуется
        self.assertEqual([Path(c).name for c in calls[0][1][4:]], ["img.sbom.zst"])
        calls.clear()
        (p.art / "img.img.zst").write_text("x")
        (p.art / "build-report.json").write_text(json.dumps({"verify_exit": 0, "dirty": False, "commit": sha,
                                                              "verify": {"counts": {"PASS": 34, "FAIL": 0, "SKIP": 0}}}))
        (p.art / "SHA256SUMS").write_text("")
        p.gates()
        self.assertTrue(all("owners_check.sh" not in str(c[1]) and "order_check.sh" not in str(c[1]) for c in calls))
        calls.clear()
        p.boot()
        self.assertEqual(calls[0][1][:3], ["zstd", "-qdf", "--sparse"])             # образ распаковывается один раз
        self.assertEqual([c[1][-2] for c in calls[1:]], ["buildhost"])
        self.assertEqual(sb.BUILDHOST_SCENARIOS, ("buildhost",))

    def test_swap_free_counts_disk_swap_not_zram(self):
        with tempfile.TemporaryDirectory() as d:
            swaps = Path(d) / "swaps"
            swaps.write_text("Filename Type Size Used Priority\n/dev/zram0 partition 2097152 1000 100\n"
                             "/var/swapfile file 8388608 1048576 10\n")
            real = sb.Path
            sb.Path = lambda p: swaps if p == "/proc/swaps" else real(p)
            try:
                self.assertAlmostEqual(sb.swap_free_gb(), 7.0, places=3)
            finally:
                sb.Path = real

    def test_worker_path_has_sbin_for_debugfs(self):
        import os
        old = os.environ.get("PATH")
        os.environ["PATH"] = "/usr/bin:/bin"
        try:
            parts = sb.worker_env()["PATH"].split(os.pathsep)
        finally:
            os.environ["PATH"] = old
        self.assertEqual(parts[:2], ["/usr/bin", "/bin"])
        self.assertIn("/usr/sbin", parts)
        self.assertIn("/sbin", parts)

    def test_host_flags_default_and_rollback(self):
        # быстрый откат оптимизаций: host.conf рядом с заданиями, без переустановки сервиса
        self.assertEqual(sb.host_flags(self.root), sb.FLAG_DEFAULTS)
        (self.root / "host.conf").write_text("# откат\nparallel_tests=0\nzstd_level = 10  # как раньше\nмусор=1\nparallel_gates=\n")
        f = sb.host_flags(self.root)
        self.assertEqual((f["parallel_tests"], f["zstd_level"], f["parallel_gates"]), ("0", "10", "1"))
        self.assertNotIn("мусор", f)

    def test_tests_and_build_run_together_and_tests_failure_wins(self):
        p, state, st = self.run_fake({})
        self.assertEqual(state, "PASSED")
        self.assertEqual([s["name"] for s in st["stages"] if s["name"] in ("tests", "build")].count("tests"), 1)
        self.assertEqual(st["flags"]["parallel_tests"], "1")
        # tests упали: build шёл рядом и тоже записан, итог — FAILED@tests, а не безымянная отмена
        p, state, st = self.run_fake({"tests": sb.Fail("тест красный")})
        self.assertEqual((state, st["failed_stage"]), ("FAILED", "tests"))
        self.assertIn("build", p.ran)
        self.assertEqual({s["name"]: s["status"] for s in st["stages"]}["build"], "PASS")
        self.assertNotIn("gates", p.ran)                                   # дальше задание не идёт

    def test_gates_and_boot_run_together_only_when_enabled_and_gate_failure_wins(self):
        p, state, st = self.run_fake({"gates": sb.Fail("владельцы")})        # по умолчанию выключено: boot после gates не идёт
        self.assertEqual(st["flags"]["parallel_boot"], "0")
        self.assertNotIn("boot", p.ran)
        (self.root / "host.conf").write_text("parallel_boot=1\n")
        p, state, st = self.run_fake({})
        self.assertEqual((state, st["flags"]["parallel_boot"]), ("PASSED", "1"))
        self.assertEqual([s["name"] for s in st["stages"]].count("boot"), 1)
        p, state, st = self.run_fake({"gates": sb.Fail("владельцы")})
        self.assertEqual((state, st["failed_stage"]), ("FAILED", "gates"))
        self.assertIn("boot", p.ran)                                       # шёл рядом и тоже записан
        self.assertEqual(next(s["status"] for s in st["stages"] if s["name"] == "boot"), "PASS")
        (self.root / "host.conf").write_text("parallel_boot=0\n")           # откат: boot только после gates
        p, state, st = self.run_fake({"gates": sb.Fail("владельцы")})
        self.assertNotIn("boot", p.ran)

    def test_parallel_tests_off_is_sequential(self):
        (self.root / "host.conf").write_text("parallel_tests=0\n")
        p, state, st = self.run_fake({"tests": sb.Fail("тест красный")})
        self.assertEqual((state, st["failed_stage"]), ("FAILED", "tests"))
        self.assertNotIn("build", p.ran)                                   # как раньше: build после tests
        self.assertEqual(st["flags"]["parallel_tests"], "0")

    def test_abort_file_stops_stage_commands(self):
        p = sb.Pipeline(self.root, self.job())
        self.assertFalse(sb._AnyExists(p.cancel, p.abort).exists())
        p.abort.parent.mkdir(parents=True, exist_ok=True)
        p.abort.touch()
        self.assertTrue(sb._AnyExists(p.cancel, p.abort).exists())

    def test_gates_refuse_skipped_or_missing_verify(self):
        # проверка структуры с SKIP (нет debugfs/mtools) — не PASS: на железо такой образ не идёт
        sha = "c" * 40
        for verify, text in (({"counts": {"PASS": 90, "FAIL": 0, "SKIP": 4}}, "неполная"), (None, "нет итогов"),
                             ({"counts": {"PASS": 0, "FAIL": 0, "SKIP": 0}}, "неполная")):
            j = sb.new_job(self.root, sha, self.bundle, {"config": "sheregesh-buildhost-pi5"})
            p = sb.Pipeline(self.root, j)
            p.art.mkdir(parents=True, exist_ok=True)
            rep = {"verify_exit": 0, "dirty": False, "commit": sha}
            if verify is not None:
                rep["verify"] = verify
            (p.art / "build-report.json").write_text(json.dumps(rep))
            p.sh = lambda stage, cmd, **kw: None
            with self.assertRaises(sb.Fail) as e:
                p.gates()
            self.assertIn(text, str(e.exception))

    def test_fstrim_after_job_is_best_effort_and_switchable(self):
        calls = []
        real = sb.subprocess.run

        def fake(cmd, **kw):
            calls.append(cmd)
            raise OSError("нет sudo")
        sb.subprocess.run = fake
        try:
            sb.trim_disk(self.root)                                          # ошибка не поднимается
            self.assertEqual(calls[0][:3], ["sudo", "-n", "fstrim"])
            (self.root / "host.conf").write_text("fstrim=0\n")
            calls.clear()
            sb.trim_disk(self.root)
            self.assertEqual(calls, [])                                      # откат одной строкой
        finally:
            sb.subprocess.run = real

    def test_default_host_is_pi5_and_submit_checks_installed_version(self):
        # все образы собираются на хосте по умолчанию (Pi 5); сервис другой версии (старый хост) задание не принимает
        self.assertEqual(sb.DEFAULT_HOST, "shbuild@pi5.local")
        text = (ROOT / "tools/build/shbuild.py").read_text()
        self.assertNotIn('"devbox"', text)                                  # нет жёсткого devbox в коде
        self.assertIn("check_installed()", text.split("def m_submit")[1].split("def m_fetch")[0])
        files = dict((str(dst), f) for f, dst in sb.installed_files())
        for need in ("shbuild/bin/shbuild.py", "shbuild/bin/boot_check.sh", "shbuild/bin/order_check.sh",
                     "shbuild/bin/owners_check.sh", "shbuild/bin/vm/probe.sh", "shbuild/bin/vm/init"):
            self.assertIn(need, files)
            self.assertTrue(files[need].exists())
        lines = sb.installed_manifest().splitlines()
        self.assertEqual(len(lines), len(files))
        self.assertRegex(lines[0], r"^[0-9a-f]{64}  shbuild/bin/")

    def test_old_jobs_without_config_build_product_image(self):
        p = sb.Pipeline(self.root, self.job())
        self.assertEqual((p.cfg, p.buildhost), (sb.CFG, False))


if __name__ == "__main__":
    unittest.main()
