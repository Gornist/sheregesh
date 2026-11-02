#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""shbuild — сервис сборки образов «Шерегеша» на devbox (ADR-0018). Один файл: команды Mac и исполнитель devbox.

С Mac (проксируется на devbox по SSH; на Mac ничего не собирается):
  python3 tools/build/shbuild.py install              поставить/обновить сервис на devbox (bin, каталоги, сборщик,
                                                      открытые ключи --dev-key/--deploy-key)

Машина сборки: Linux x86-64 с Docker, qemu-user-static (binfmt aarch64), ≥ 40 ГБ диска и ≥ 6 ГБ памяти, вход по SSH
по ключу. Имена хоста — SHBUILD_HOST (команды, по умолчанию devbox) и SHBUILD_BULK_HOST (передачи, devbox-lan);
одна машина — оба одинаковые.
  python3 tools/build/shbuild.py submit [РЕВИЗИЯ]     отправить коммит (по умолчанию HEAD) — печатает id задания;
                                                      --config sheregesh-buildhost-pi5 — образ сборочного хоста (ADR-0019)
  python3 tools/build/shbuild.py status [ID]          очередь или одно задание (этапы, время, итог)
  python3 tools/build/shbuild.py wait ID              ждать конца; одна строка «ID СОСТОЯНИЕ …», код 0 — PASSED
  python3 tools/build/shbuild.py logs ID [ЭТАП]       журнал этапа (по умолчанию — последний начатый)
  python3 tools/build/shbuild.py cancel ID            отменить (сборка останавливается, контейнер тоже)
  python3 tools/build/shbuild.py fetch ID [КАТАЛОГ]   забрать артефакты PASSED на Mac (только для записи карты)

На devbox (вызывается командами выше): enqueue, worker, status, wait, logs, cancel.

Этапы задания: preflight → checkout → tests → build → gates → boot → (итог). Любой провал останавливает задание.
Конечные состояния всегда явные: PASSED, FAILED (дефект кода/образа), ERROR (среда), CANCELLED, TIMEOUT.
Одно задание за раз (flock); остальные ждут в очереди."""
import argparse
import datetime as dt
import fcntl
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HOME = Path(os.environ.get("SHBUILD_HOME", Path.home() / "shbuild"))
DEVBOX = os.environ.get("SHBUILD_HOST", "devbox")            # команды
DEVBOX_BULK = os.environ.get("SHBUILD_BULK_HOST", "devbox-lan")   # большие передачи (пакет git, артефакты)
REMOTE_BIN = "shbuild/bin/shbuild.py"
CFG = "sheregesh-lab-dev-pi4"
TERMINAL = ("PASSED", "FAILED", "ERROR", "CANCELLED", "TIMEOUT")
STAGES = ("preflight", "checkout", "tests", "build", "gates", "boot")
TIMEOUTS = {"preflight": 60, "checkout": 300, "tests": 900, "build": 5400, "gates": 1800, "boot": 1800}
MIN_DISK_GB = 40          # образ 7 ГБ, корень в tar, persistent, рабочий том сборки
MIN_MEM_GB = 6            # OOM devbox во время сборки (s5-ota/DEVBOX-REBOOT.md): ОЗУ + свободная подкачка на диске
MIN_RAM_GB = 1.0          # ниже — не помогает никакая подкачка (Pi 5 на 2 ГБ с подкачкой на NVMe, ADR-0019)
KEEP_PASSED = 5           # артефакты последних PASSED; задания с keep — всегда
BOOT_SCENARIOS = ("console", "remote", "fresh", "offline")
BUILDHOST_PREFIX = "sheregesh-buildhost-"   # ADR-0019: образ сборочного хоста — не продуктовый, свои гейты и загрузочный тест
BUILDHOST_SCENARIOS = ("buildhost",)
CONFIGS = (CFG, "sheregesh-buildhost-pi5")


def art_name(st):
    """Каталог артефактов задания: коммит (продуктовый образ) или коммит.конфигурация (другой образ того же коммита)."""
    cfg = (st.get("options") or {}).get("config") or CFG
    return st["commit"] if cfg == CFG else f"{st['commit']}.{cfg}"


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Fail(Exception):
    """Провал этапа: kind FAILED — дефект кода или образа, ERROR — среда."""

    def __init__(self, msg, kind="FAILED"):
        super().__init__(msg)
        self.kind = kind


# ---------------------------------------------------------------- хранилище заданий
class Job:
    def __init__(self, root, jid):
        self.root, self.id = root, jid
        self.dir = root / "jobs" / jid

    @property
    def state_file(self):
        return self.dir / "state.json"

    def load(self):
        return json.loads(self.state_file.read_text())

    def save(self, st):
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
        tmp.replace(self.state_file)

    def update(self, **kw):
        st = self.load()
        st.update(kw)
        self.save(st)
        return st

    def log(self, stage):
        return self.dir / "logs" / f"{stage}.log"


def new_job(root, commit, bundle, options=None):
    base = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + commit[:7]
    for n in range(1, 100):                                   # тот же коммит в ту же секунду — суффикс
        j = Job(root, base if n == 1 else f"{base}-{n}")
        try:
            (j.dir / "logs").mkdir(parents=True)
            break
        except FileExistsError:
            continue
    st = {"id": j.id, "commit": commit, "bundle": str(bundle), "options": options or {}, "state": "QUEUED",
          "created": now(), "stage": None, "stages": [], "reason": None}
    j.save(st)
    return j


def jobs(root):
    d = root / "jobs"
    return sorted((Job(root, p.name) for p in d.iterdir() if (p / "state.json").exists()), key=lambda j: j.id) \
        if d.exists() else []


def queued(root):
    return [j for j in jobs(root) if j.load()["state"] == "QUEUED"]


# ---------------------------------------------------------------- запуск этапа
def run_logged(cmd, log, timeout, cwd=None, env=None, cancel_file=None):
    """Команда списком (без shell) с журналом: строки с временем от начала этапа. → код возврата.
    Тайм-аут и отмена убивают всю группу процессов; TimeoutError / InterruptedError."""
    t0 = time.monotonic()
    with open(log, "a", encoding="utf-8", errors="replace") as out:
        out.write(f"$ {shlex.join(map(str, cmd))}\n")
        out.flush()
        p = subprocess.Popen(list(map(str, cmd)), cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             start_new_session=True, text=True, errors="replace")

        def pump():
            for line in p.stdout:
                out.write(f"[{time.monotonic() - t0:7.1f}] {line.rstrip(chr(10))}\n")
                out.flush()

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            while p.poll() is None:
                if time.monotonic() - t0 > timeout:
                    raise TimeoutError(f"тайм-аут {timeout} с")
                if cancel_file is not None and cancel_file.exists():
                    raise InterruptedError("отменено")
                time.sleep(0.2)
        except (TimeoutError, InterruptedError):
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            raise
        finally:
            reader.join(timeout=5)
            p.stdout.close()
        return p.returncode


def check(cmd, log, timeout, kind="FAILED", **kw):
    rc = run_logged(cmd, log, timeout, **kw)
    if rc != 0:
        raise Fail(f"{Path(str(cmd[0])).name} … код {rc}", kind)


# ---------------------------------------------------------------- этапы
class Pipeline:
    """Этапы одного задания. Внешние команды можно подменить (тесты): self.sh — функция запуска."""

    def __init__(self, root, job, bin_dir=None):
        self.root, self.job = root, job
        self.st = job.load()
        self.commit = self.st["commit"]
        self.bin = bin_dir or Path(__file__).resolve().parent
        self.src = job.dir / "src"
        self.cfg = (self.st.get("options") or {}).get("config") or CFG
        self.buildhost = self.cfg.startswith(BUILDHOST_PREFIX)
        self.art = root / "artifacts" / art_name(self.st)
        self.cancel = job.dir / "cancel"
        self.sde = None

    def sh(self, stage, cmd, kind="FAILED", **kw):
        check(cmd, self.job.log(stage), TIMEOUTS[stage], kind=kind, cancel_file=self.cancel, **kw)

    # 0
    def preflight(self):
        log = self.job.log("preflight")
        free = shutil.disk_usage(self.root).free / 2**30
        ram, swap = meminfo_gb(), swap_free_gb()
        mem = ram + swap
        binfmt = Path("/proc/sys/fs/binfmt_misc/qemu-aarch64")
        native = platform.machine() in ("aarch64", "arm64")          # arm64-хост: эмуляция не нужна (ADR-0019)
        lines = [f"диск свободно {free:.1f} ГБ (нужно ≥ {MIN_DISK_GB})", f"память доступна {mem:.1f} ГБ = ОЗУ {ram:.1f} + подкачка на диске {swap:.1f} (≥ {MIN_MEM_GB}, ОЗУ ≥ {MIN_RAM_GB})",
                 "binfmt qemu-aarch64: не нужен (нативный arm64)" if native else
                 f"binfmt qemu-aarch64: {binfmt.read_text().splitlines()[0] if binfmt.exists() else 'нет'}"]
        log.write_text("\n".join(lines) + "\n")
        if free < MIN_DISK_GB:
            raise Fail(lines[0], "ERROR")
        if mem < MIN_MEM_GB or ram < MIN_RAM_GB:
            raise Fail(lines[1], "ERROR")
        if not native and (not binfmt.exists() or "enabled" not in binfmt.read_text()):
            raise Fail("binfmt qemu-aarch64 не зарегистрирован (привилегированные контейнеры его стирают)", "ERROR")
        self.sh("preflight", ["docker", "info", "--format", "{{.ServerVersion}}"], kind="ERROR")
        busy = subprocess.run(["docker", "ps", "-q", "--filter", "volume=sheregesh-image-work"],
                              capture_output=True, text=True).stdout.split()
        if busy:
            raise Fail(f"идёт другая сборка образа (контейнеры {busy})", "ERROR")

    # 1
    def checkout(self):
        repo = self.root / "repo.git"
        bundle = Path(self.st["bundle"])
        self.sh("checkout", ["git", "-C", repo, "fetch", "-q", bundle, f"+{self.commit}:refs/shbuild/{self.job.id}"],
                kind="ERROR")
        self.sh("checkout", ["git", "-C", repo, "worktree", "add", "-q", "--detach", self.src, self.commit], kind="ERROR")
        self.sde = int(git(repo, "log", "-1", "--format=%ct", self.commit))
        # пакет git этого коммита для build.json/версии внутри сборки (docker-build.sh SHEREGESH_BUNDLE)
        self.sh("checkout", ["git", "-C", repo, "bundle", "create", "-q", self.job.dir / "src.bundle",
                             f"refs/shbuild/{self.job.id}"], kind="ERROR")
        self.job.update(source_date_epoch=self.sde)

    # 2
    def tests(self):
        # как CI (ci.yml): стандартная библиотека, без зависимостей приложения
        self.sh("tests", ["python3", "-m", "unittest", "discover", "-s", "tests/tools", "-p", "test_*.py"], cwd=self.src)

    # 3
    def build(self):
        env = dict(os.environ, SHEREGESH_SRC=str(self.src), SHEREGESH_BUNDLE=str(self.job.dir / "src.bundle"),
                   SOURCE_DATE_EPOCH=str(self.sde), SHEREGESH_BUILD_AS_ROOT="1",
                   SSH_PUBKEY=str(self.root / "keys/dev.pub"), DEPLOY_PUBKEY=str(self.root / "keys/deploy.pub"),
                   SHEREGESH_CONTAINER_NAME=f"shbuild-{self.job.id}", SHBUILD_JOB=self.job.id)
        builder = self.st.get("options", {}).get("builder_image")
        script = (self.src / "os/docker-build.sh").read_text()
        if not builder and "SHEREGESH_BUILDER_IMAGE" in script:     # старые коммиты сборщик не умеют — как раньше
            ref = (self.src / "os/rpi-image-gen.ref").read_text().strip()
            log = self.job.log("build")
            with open(log, "a") as f:
                r = subprocess.run(["bash", self.bin / "make_builder.sh", ref], stdout=subprocess.PIPE, stderr=f,
                                   text=True, timeout=TIMEOUTS["build"])
            if r.returncode != 0:
                raise Fail(f"make_builder.sh: код {r.returncode}", "ERROR")
            builder = r.stdout.strip()
        if builder:
            env["SHEREGESH_BUILDER_IMAGE"] = builder
        try:
            self.sh("build", ["bash", self.src / "os/docker-build.sh", self.cfg], env=env)
        finally:
            stop_containers(self.src)
        out = self.src / "build/image" / self.cfg
        if self.art.exists():
            shutil.rmtree(self.art)
        self.art.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(out), str(self.art))

    # 4
    def gates(self):
        log = self.job.log("gates")
        rep = json.loads((self.art / "build-report.json").read_text())
        notes = [f"verify_exit {rep.get('verify_exit')}", f"dirty {rep.get('dirty')}", f"commit {rep.get('commit')}"]
        with open(log, "a") as f:
            f.write("\n".join(notes) + "\n")
        if rep.get("verify_exit") != 0:
            raise Fail("verify_image не пройден (build-report verify_exit)")
        if rep.get("dirty") is not False:
            raise Fail("дерево сборки dirty")
        if rep.get("commit") != self.commit:
            raise Fail(f"build-report: коммит {rep.get('commit')} ≠ {self.commit}")
        self.sh("gates", ["shasum" if platform.system() == "Darwin" else "sha256sum", "-c", "SHA256SUMS"], cwd=self.art)
        self.sbom_gate()
        img = next(self.art.glob("*.img.zst"))
        if self.buildhost:      # owners-эталон и порядок юнитов — свойства A/B-образа «Шерегеша», здесь неприменимы
            return
        owners = self.art / "owners.txt"
        with open(owners, "w") as f:
            rc = subprocess.run(["bash", self.bin / "owners_check.sh", img], stdout=f,
                                stderr=open(log, "a"), timeout=TIMEOUTS["gates"]).returncode
        if rc != 0:
            raise Fail(f"owners_check: код {rc}", "ERROR")
        # эталон — в репозитории рядом с рецептом (tools/image/owners-baseline.txt); файл машины сборки — переопределение
        base = self.root / "baseline/owners.txt"
        if not base.exists():
            base = self.src / "tools/image/owners-baseline.txt"
        if not base.exists():
            raise Fail("нет эталона владельцев: tools/image/owners-baseline.txt в ревизии или ~/shbuild/baseline/owners.txt",
                       "ERROR")
        diff = subprocess.run(["diff", base, owners], capture_output=True, text=True)
        with open(log, "a") as f:
            f.write(f"owners против {base}:\n{diff.stdout or 'совпадает'}\n")
        if diff.returncode != 0:
            raise Fail("владельцы файлов системного раздела отличаются от эталона (owners_check)")
        self.sh("gates", ["bash", self.bin / "order_check.sh", img, "8"])

    def sbom_gate(self):
        """REQ-DOC-006: SBOM образа (*.sbom.zst) и пакета (*.deb.spdx.json) есть и это SPDX 2.3 — проверяет инструмент
        выпуска собираемой ревизии; у ревизий без него (до tools/release) правило не действует."""
        tool = self.src / "tools/release/release.py"
        if not tool.exists():
            return
        image = sorted(self.art.glob("*.sbom.zst"))
        package = sorted(self.art.glob("*.deb.spdx.json"))
        if self.buildhost:      # пакета sheregesh в образе сборочного хоста нет — нужен SBOM самого образа
            if not image:
                raise Fail("нет SBOM образа")
            return self.sh("gates", ["python3", "-I", str(tool), "sbom-check", *map(str, image)])
        if not image or not package:
            raise Fail("нет SBOM " + " и ".join(n for n, f in (("образа", image), ("пакета", package)) if not f))
        self.sh("gates", ["python3", "-I", str(tool), "sbom-check", *map(str, image + package)])

    # 5
    def boot(self):
        img = next(self.art.glob("*.img.zst"))
        for sc in (BUILDHOST_SCENARIOS if self.buildhost else BOOT_SCENARIOS):
            self.sh("boot", ["bash", self.bin / "boot_check.sh", img, sc, self.job.id])

    def run(self):
        st = self.job.update(state="RUNNING", started=now())
        for stage in STAGES:
            if self.cancel.exists():
                return self.finish("CANCELLED", stage, "отменено")
            rec = {"name": stage, "started": now()}
            st = self.job.load()
            st["stage"] = stage
            st["stages"].append(rec)
            self.job.save(st)
            t0 = time.monotonic()
            status, reason = "PASS", None
            try:
                getattr(self, stage)()
            except Fail as e:
                status, reason = e.kind, str(e)
            except TimeoutError as e:
                status, reason = "TIMEOUT", str(e)
            except InterruptedError:
                status, reason = "CANCELLED", "отменено"
            except Exception as e:                                  # непредвиденное — тоже явный конец, не обрыв
                status, reason = "ERROR", f"{type(e).__name__}: {e}"
            st = self.job.load()
            st["stages"][-1].update(status=status, seconds=round(time.monotonic() - t0, 1), ended=now())
            self.job.save(st)
            if status != "PASS":
                return self.finish(status, stage, reason)
        return self.finish("PASSED", None, None)

    def finish(self, state, stage, reason):
        st = self.job.update(state=state, failed_stage=stage, reason=reason, ended=now())
        report = {k: st.get(k) for k in ("id", "commit", "state", "failed_stage", "reason", "source_date_epoch",
                                         "created", "started", "ended", "stages")}
        (self.job.dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
        if state == "PASSED":
            (self.art / "shbuild-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
        cleanup(self.root, self.job, keep_artifacts=(state == "PASSED"))
        return state


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def meminfo_gb():
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 2**20
    except OSError:
        pass
    return float("inf")


def swap_free_gb():
    """Свободная подкачка на диске, ГБ. zram не считается: он сам занимает ОЗУ."""
    total = 0
    try:
        for line in Path("/proc/swaps").read_text().splitlines()[1:]:
            f = line.split()
            if len(f) >= 4 and "zram" not in f[0]:
                total += max(int(f[2]) - int(f[3]), 0)
    except (OSError, ValueError):
        pass
    return total / 2**20


def stop_containers(src):
    """Контейнеры сборки этого дерева (docker run --rm живёт после смерти клиента) — остановить."""
    ids = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True).stdout.split()
    for cid in ids:
        mounts = subprocess.run(["docker", "inspect", "-f", "{{range .Mounts}}{{.Source}} {{end}}", cid],
                                capture_output=True, text=True).stdout
        if str(src) in mounts:
            subprocess.run(["docker", "stop", "-t", "5", cid], capture_output=True)


def cleanup(root, job, keep_artifacts):
    """Дерево задания и ссылка в repo.git — всегда; артефакты провала — удалить; PASSED — последние KEEP_PASSED."""
    repo = root / "repo.git"
    src = job.dir / "src"
    if src.exists():
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(src)], capture_output=True)
        shutil.rmtree(src, ignore_errors=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True)
    for f in ("src.bundle",):
        (job.dir / f).unlink(missing_ok=True)
    st = job.load()
    Path(st["bundle"]).unlink(missing_ok=True)
    if not keep_artifacts:
        art = root / "artifacts" / art_name(st)
        if art.exists() and not (art / "keep").exists() and not (art / "shbuild-report.json").exists():
            shutil.rmtree(art, ignore_errors=True)
    passed = [j for j in jobs(root) if j.load()["state"] == "PASSED"]
    keep = {art_name(j.load()) for j in passed[-KEEP_PASSED:]}
    for art in (root / "artifacts").glob("*") if (root / "artifacts").exists() else []:
        if art.name not in keep and not (art / "keep").exists():
            shutil.rmtree(art, ignore_errors=True)


# ---------------------------------------------------------------- исполнитель
def worker(root, once=False):
    lock = open(root / "worker.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0                                    # исполнитель уже работает
    while True:
        q = queued(root)
        if not q:
            return 0
        Pipeline(root, q[0]).run()
        if once:
            return 0


def worker_env():
    """Окружение исполнителя: запущенный по SSH процесс получает PATH без sbin (Debian: debugfs, mke2fs, losetup
    лежат в /usr/sbin) — тесты и гейты ищут их через PATH (Pi 5, ADR-0019)."""
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for d in ("/usr/local/sbin", "/usr/sbin", "/sbin"):
        if d not in parts:
            parts.append(d)
    return dict(os.environ, PATH=os.pathsep.join(p for p in parts if p))


def spawn_worker(root):
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "worker"], start_new_session=True, env=worker_env(),
                     stdout=open(root / "worker.log", "a"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)


def line(st):
    """Одна строка состояния задания (для wait и людей)."""
    parts = []
    for s in st.get("stages", []):
        t = f"({s['seconds']:.0f}s)" if "seconds" in s else ""
        parts.append(f"{s['name']}:{s.get('status', '…')}{t}")
    tail = f" — {st['failed_stage']}: {st['reason']}" if st.get("reason") else ""
    return f"{st['id']} {st['state']} [{' '.join(parts)}]{tail}"


# ---------------------------------------------------------------- команды devbox
def d_enqueue(a):
    opts = {k: v for k, v in (("builder_image", a.builder), ("config", a.config)) if v}
    j = new_job(HOME, a.commit, Path(a.bundle).resolve(), opts or None)
    spawn_worker(HOME)
    print(j.id)
    return 0


def find(jid):
    j = Job(HOME, jid)
    if not j.state_file.exists():
        raise SystemExit(f"нет задания {jid}")
    return j


def d_status(a):
    if a.id:
        st = find(a.id).load()
        print(line(st))
        for s in st["stages"]:
            print(f"  {s['name']:9} {s.get('status', 'идёт'):9} {s.get('seconds', '')}")
        return 0
    for j in jobs(HOME)[-15:]:
        print(line(j.load()))
    return 0


def d_wait(a):
    j = find(a.id)
    while (st := j.load())["state"] not in TERMINAL:
        time.sleep(5)
    print(line(st))
    return 0 if st["state"] == "PASSED" else 1


def d_logs(a):
    j = find(a.id)
    st = j.load()
    stage = a.stage or (st["stages"][-1]["name"] if st["stages"] else "preflight")
    p = j.log(stage)
    sys.stdout.write("".join(p.read_text(errors="replace").splitlines(True)[-a.lines:]) if p.exists() else "(нет журнала)\n")
    return 0


def d_cancel(a):
    j = find(a.id)
    (j.dir / "cancel").touch()
    if j.load()["state"] == "QUEUED":
        j.update(state="CANCELLED", reason="отменено до запуска", ended=now())
    print(f"{a.id}: отмена запрошена")
    return 0


# ---------------------------------------------------------------- команды Mac
def ssh(*args, bulk=False, check_rc=True, capture=False):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", DEVBOX_BULK if bulk else DEVBOX, *args],
                       capture_output=capture, text=True)
    if check_rc and r.returncode != 0:
        raise SystemExit(r.returncode)
    return r


def remote(cmd, *args, check_rc=True):
    return ssh(f"python3 {REMOTE_BIN} {cmd} " + shlex.join(args), check_rc=check_rc)


def m_install(a):
    here = Path(__file__).resolve().parent
    repo = here.parents[1]
    ssh("mkdir -p shbuild/bin shbuild/jobs shbuild/artifacts shbuild/inbox shbuild/keys shbuild/baseline && "
        "(test -d shbuild/repo.git || git init -q --bare shbuild/repo.git)")
    files = [here / "shbuild.py", here / "boot_check.sh", here / "make_builder.sh", repo / "tools/image/owners_check.sh",
             repo / "tools/image/order_check.sh"]
    subprocess.run(["scp", "-q", *map(str, files), f"{DEVBOX_BULK}:shbuild/bin/"], check=True)
    ssh("mkdir -p shbuild/bin/vm")
    vm = [here / "vm" / f for f in ("init", "probe.sh", "make_vmkit.sh")]
    subprocess.run(["scp", "-q", *map(str, vm), f"{DEVBOX_BULK}:shbuild/bin/vm/"], check=True)
    ssh("bash shbuild/bin/vm/make_vmkit.sh >/dev/null && echo 'набор машины: ~/shbuild/vm'")
    ssh("chmod +x shbuild/bin/*")
    # Открытые ключи, которые сборка вшивает в DEV-образ: вход (dev.pub — SSH sheregesh-ui и root) и проверка
    # подписи выпусков shdeploy (deploy.pub). Закрытые ключи не покидают эту машину.
    for name, local in (("dev", a.dev_key), ("deploy", a.deploy_key)):
        local = Path(local).expanduser()
        have = subprocess.run(["ssh", DEVBOX, f"test -s shbuild/keys/{name}.pub"]).returncode == 0
        if local.is_file():
            if not have or a.replace_keys:
                subprocess.run(["scp", "-q", str(local), f"{DEVBOX_BULK}:shbuild/keys/{name}.pub"], check=True)
                print(f"ключ {name}.pub ← {local}")
        elif not have:
            print(f"НЕТ ключа {name}.pub: создайте {local.with_suffix('')} — "
                  f"ssh-keygen -t ed25519 -f {local.with_suffix('')} -C sheregesh-{name} — и повторите install")
    ssh("ls shbuild/keys; test -s shbuild/baseline/owners.txt && echo 'эталон owners: локальный (переопределение)' "
        "|| echo 'эталон owners: из ревизии (tools/image/owners-baseline.txt)'")
    print(f"установлено: {DEVBOX}:~/shbuild")
    return 0


def m_submit(a):
    sha = subprocess.run(["git", "rev-parse", a.rev + "^{commit}"], capture_output=True, text=True, check=True).stdout.strip()
    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"shbuild-{sha[:12]}-{uuid.uuid4().hex[:6]}.bundle"
    ref = f"refs/shbuild/submit-{sha[:12]}"
    subprocess.run(["git", "update-ref", ref, sha], check=True)
    try:
        subprocess.run(["git", "bundle", "create", "-q", str(tmp), ref], check=True)
    finally:
        subprocess.run(["git", "update-ref", "-d", ref], check=True)
    subprocess.run(["scp", "-q", str(tmp), f"{DEVBOX_BULK}:shbuild/inbox/"], check=True)
    tmp.unlink()
    args = ["enqueue", sha, f"shbuild/inbox/{tmp.name}"] + (["--builder", a.builder] if a.builder else []) \
        + (["--config", a.config] if a.config else [])
    return remote(*args).returncode


def m_fetch(a):
    r = ssh(f"python3 {REMOTE_BIN} status {shlex.quote(a.id)}", capture=True)
    first = r.stdout.splitlines()[0].split()
    if first[1] != "PASSED":
        raise SystemExit(f"{a.id}: {first[1]} — забирать можно только PASSED")
    st = json.loads(ssh(f"cat shbuild/jobs/{shlex.quote(a.id)}/state.json", capture=True).stdout)
    name = art_name(st)
    dest = Path(a.dest or f"build/image/{name[:12] if name == st['commit'] else name.replace('sheregesh-', '')[:60]}")
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(["rsync", "-a", f"{DEVBOX_BULK}:shbuild/artifacts/{name}/", f"{dest}/"], check=True)
    print(dest)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    on_mac = platform.system() == "Darwin"
    s = sub.add_parser("install")
    s.add_argument("--dev-key", default="~/.ssh/id_ed25519.pub", help="открытый ключ входа на платы DEV-образа")
    s.add_argument("--deploy-key", default="~/.ssh/sheregesh-deploy.pub", help="открытый ключ подписи выпусков shdeploy")
    s.add_argument("--replace-keys", action="store_true", help="заменить ключи, уже лежащие на машине сборки")
    s = sub.add_parser("submit")
    s.add_argument("rev", nargs="?", default="HEAD")
    s.add_argument("--builder", help="образ контейнера-сборщика (SHEREGESH_BUILDER_IMAGE)")
    s.add_argument("--config", choices=CONFIGS[1:], help="не продуктовый образ того же коммита (по умолчанию " + CFG + ")")
    s = sub.add_parser("enqueue")
    s.add_argument("commit")
    s.add_argument("bundle")
    s.add_argument("--builder")
    s.add_argument("--config", choices=CONFIGS[1:])
    sub.add_parser("worker")
    s = sub.add_parser("status")
    s.add_argument("id", nargs="?")
    for name in ("wait", "cancel"):
        sub.add_parser(name).add_argument("id")
    s = sub.add_parser("logs")
    s.add_argument("id")
    s.add_argument("stage", nargs="?", choices=STAGES)
    s.add_argument("-n", "--lines", type=int, default=60)
    s = sub.add_parser("fetch")
    s.add_argument("id")
    s.add_argument("dest", nargs="?")
    a = ap.parse_args(argv)
    if on_mac:
        if a.cmd == "install":
            return m_install(a)
        if a.cmd == "submit":
            return m_submit(a)
        if a.cmd == "fetch":
            return m_fetch(a)
        if a.cmd in ("status", "wait", "cancel"):
            return remote(a.cmd, *([a.id] if a.id else []), check_rc=False).returncode
        if a.cmd == "logs":
            return remote("logs", a.id, *([a.stage] if a.stage else []), "-n", str(a.lines), check_rc=False).returncode
        raise SystemExit(f"{a.cmd}: только на devbox")
    HOME.mkdir(parents=True, exist_ok=True)
    return {"enqueue": d_enqueue, "worker": lambda a: worker(HOME), "status": d_status, "wait": d_wait,
            "logs": d_logs, "cancel": d_cancel}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
