# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""sheregesh-deployd — привилегированный установщик выпусков приложения (S3.1-DEPLOY, только dev-образ).

  sheregesh-deployd install | rollback | factory | status

Запускается как sheregesh-deploy@<действие>.service (root, oneshot); polkit разрешает sheregesh-ui только
«start» этих трёх экземпляров. Параметров нет: пакет берётся из единственного места
~sheregesh-ui/.cache/sheregesh-deploy/incoming/{bundle.tar.xz,bundle.tar.xz.sig}.

install: копия пакета читается в память (O_NOFOLLOW, владелец sheregesh-ui, предел размера) → подпись
ssh-keygen -Y по /etc/sheregesh/deploy/allowed_signers (пространство sheregesh-deploy) → строгий разбор и SHA-256
каждого файла → распаковка в /var/lib/sheregesh-deploy/releases/.tmp-* (root, 0755/0644), RELEASE.json
последним, rename в releases/<id> → атомарное переключение symlink current (rename) с отметкой pending →
перезапуск только служб роли платы (пульт: video-rx и console; блок: video-tx и remote-net; без роли: эмулятор и
пульт) → ожидание READY (Type=notify) и окно наблюдения без перезапусков → pending снимается.
Если служба не вышла в READY или перезапустилась в окне — current возвращается на прежний выпуск (или заводской
код) и службы перезапускаются снова: итог ROLLED_BACK. После перезагрузки до подтверждения launcher сам выбирает
прежний выпуск (pending с другим boot_id).
rollback: переключение на предыдущий выпуск с той же проверкой; factory: снять current (заводской /usr).

Установщик не исполняет и не компилирует присланный код от root: с ним работают только ssh-keygen -Y verify
(до разбора), строгий разбор tar в памяти и запись обычных файлов. Байт-код не создаётся — Python компилирует
выпуск в памяти уже в непривилегированной службе (0,2 с на Pi 4 для всего кода); компилятор CPython от root на
недоверенном входе (ошибки разбора, исчерпание памяти с угрозой OOM для sshd) исключён (аудит S3.1-DEPLOY).
Путей, команд и имён служб извне не принимает; SSH, sheregesh-health, sheregesh-link и ретранслятор — из /usr.
"""
import fcntl
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import launcher, sig
from .bundle import MAX_BUNDLE, BundleError, parse_bundle

ACTIONS = ("install", "rollback", "factory", "status")
UNITS = {"remote": ["sheregesh-video-tx", "sheregesh-remote-net"],
         "console": ["sheregesh-video-rx", "sheregesh-console"],
         "standalone": ["sheregesh-remote-emu", "sheregesh-console"]}
OPTIONAL_UNITS = frozenset({"sheregesh-video-tx", "sheregesh-video-rx"})
KEEP_RELEASES = 3
EX_USAGE = 64
HISTORY = 20


@dataclass
class Settings:
    deploy_dir: Path = Path(launcher.DEPLOY_DIR)
    incoming: Path = Path("/home/sheregesh-ui/.cache/sheregesh-deploy/incoming")
    signers: Path = Path("/etc/sheregesh/deploy/allowed_signers")
    config_dir: Path = Path("/home/sheregesh-ui/.config/sheregesh")
    owner: str = "sheregesh-ui"
    require_root: bool = True          # владелец каталогов выпуска — root (в тестах выключается)
    restart_timeout: float = 180.0     # TimeoutStartSec пульта 90 с + остановка
    window: float = 40.0               # окно наблюдения: больше WatchdogSec=20 пульта
    rollback_window: float = 20.0
    poll: float = 1.0
    boot_id: str = field(default_factory=launcher.boot_id)


class DeployError(Exception):
    pass


class Systemd:
    """Управление службами через systemctl (установщик работает от root)."""

    PROPS = "LoadState,ActiveState,SubState,Result,NRestarts,MainPID,InvocationID,ConditionResult"

    def __init__(self, systemctl="systemctl"):
        self.systemctl = systemctl

    def show(self, unit):
        r = subprocess.run([self.systemctl, "show", "-p", self.PROPS, f"{unit}.service"], capture_output=True,
                           text=True, timeout=30)
        return dict(x.split("=", 1) for x in r.stdout.splitlines() if "=" in x)

    def restart(self, unit, timeout):
        """Сброс счётчика неудач (StartLimitBurst) и перезапуск с ожиданием задания (READY для Type=notify)."""
        subprocess.run([self.systemctl, "reset-failed", f"{unit}.service"], capture_output=True, timeout=30)
        try:
            r = subprocess.run([self.systemctl, "restart", f"{unit}.service"], capture_output=True, text=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"нет READY за {timeout:.0f} с"
        return r.returncode == 0, ((r.stderr.strip().splitlines() or ["ok"])[0][:160])


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Installer:
    def __init__(self, settings: Settings | None = None, services=None, clock=time.monotonic, sleep=time.sleep,
                 out=print):
        self.cfg = settings or Settings()
        self.svc = services or Systemd()
        self.clock, self.sleep, self.out = clock, sleep, out
        self.dir = Path(self.cfg.deploy_dir)
        self.releases = self.dir / "releases"

    # ---------- состояние ----------
    def load(self):
        try:
            s = json.loads((self.dir / "state.json").read_text())
            return s if isinstance(s, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, state):
        tmp = self.dir / "state.json.tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.dir / "state.json")
        _fsync_dir(self.dir)

    def current(self):
        cur = self.dir / "current"
        if not cur.is_symlink():
            return None
        target = os.readlink(cur)
        return target[len("releases/"):] if target.startswith("releases/") else None

    def _set_current(self, rid):
        cur = self.dir / "current"
        if rid is None:
            if cur.is_symlink() or cur.exists():
                cur.unlink()
        else:
            tmp = self.dir / "current.tmp"
            if tmp.is_symlink() or tmp.exists():
                tmp.unlink()
            os.symlink(f"releases/{rid}", tmp)
            os.replace(tmp, cur)                    # rename(2): атомарно для читающих launcher
        _fsync_dir(self.dir)

    def role(self):
        c = Path(self.cfg.config_dir)
        return "remote" if (c / "remote.role").exists() else "console" if (c / "console.role").exists() else "standalone"

    # ---------- входящий пакет ----------
    def _read_owned(self, path, limit, dir_fd=None):
        try:
            fd = os.open(path.name if dir_fd is not None else path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=dir_fd)
        except OSError as e:
            raise DeployError(f"нет файла {path.name}: {e.strerror}") from None
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise DeployError(f"{path.name}: не обычный файл")
            if st.st_uid != pwd.getpwnam(self.cfg.owner).pw_uid:
                raise DeployError(f"{path.name}: владелец не {self.cfg.owner}")
            if st.st_size > limit:
                raise DeployError(f"{path.name}: больше {limit} байт")
            data = f.read(limit + 1)
        if len(data) > limit:
            raise DeployError(f"{path.name}: больше {limit} байт")
        return data

    def _take_incoming(self):
        """Каталог входящих — настоящий каталог владельца (не ссылка); файлы открываются и удаляются через его fd,
        так что подмена каталога или пути между проверкой и действием root ничего не даёт."""
        inc = Path(self.cfg.incoming)
        try:
            dfd = os.open(inc, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as e:
            raise DeployError(f"каталог входящих {inc}: {e.strerror} (ссылка или нет каталога)") from None
        try:
            if os.fstat(dfd).st_uid != pwd.getpwnam(self.cfg.owner).pw_uid:
                raise DeployError(f"каталог входящих: владелец не {self.cfg.owner}")
            try:
                return (self._read_owned(inc / "bundle.tar.xz", MAX_BUNDLE, dfd),
                        self._read_owned(inc / "bundle.tar.xz.sig", sig.MAX_SIG, dfd))
            finally:
                for name in ("bundle.tar.xz", "bundle.tar.xz.sig"):
                    try:
                        os.unlink(name, dir_fd=dfd)
                    except OSError:
                        pass
        finally:
            os.close(dfd)

    # ---------- выпуск на диске ----------
    def _materialize(self, manifest, files):
        rid = manifest["id"]
        rel = self.releases / rid
        self.releases.mkdir(mode=0o755, parents=True, exist_ok=True)
        if rel.exists():
            try:
                old = launcher.verify_release(str(rel), self.cfg.require_root)
                if old.get("files") == manifest["files"]:
                    self.out(f"выпуск {rid} уже установлен и цел — используется повторно")
                    return rid
                raise DeployError(f"выпуск {rid} уже есть с другим содержимым")
            except launcher.Rejected as e:
                self.out(f"выпуск {rid} на диске повреждён ({e}) — переустановка")
                shutil.rmtree(rel)
        need = sum(len(b) for b in files.values()) * 3 + 32 * 2**20
        if shutil.disk_usage(self.dir).free < need:
            raise DeployError(f"мало места на {self.dir}: нужно {need // 2**20} МБ")
        tmp = self.releases / f".tmp-{rid}-{os.getpid()}"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(mode=0o755)
        try:
            for path, data in sorted(files.items()):
                dst = tmp / path
                dst.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(dst, manifest["files"][path]["mode"])
                # Время коммита у всех файлов: порядок записи не делает .ts «новее» .qm (pi1, 57e8fe4), выпуск
                # воспроизводим. Подписанное значение из манифеста; без него — 0 (одинаково для всех файлов).
                stamp = int(manifest.get("commit_time") or 0)
                os.utime(dst, (stamp, stamp))
            record = dict(manifest, installed_at=_now())
            with open(tmp / "RELEASE.json", "w") as f:
                json.dump(record, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp / "RELEASE.json", 0o644)
            for d, _, _ in os.walk(tmp):
                os.chmod(d, 0o755)
            os.rename(tmp, rel)
            _fsync_dir(self.releases)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        launcher.verify_release(str(rel), self.cfg.require_root)       # та же проверка, что при старте службы
        return rid

    def _gc(self, state):
        keep = {state.get("current"), state.get("previous")}
        rels = []
        for p in self.releases.glob("*"):
            if p.name.startswith(".tmp-"):
                shutil.rmtree(p, ignore_errors=True)
            elif p.is_dir():
                rels.append(p)
        rels.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        keep |= {p.name for p in rels[:KEEP_RELEASES]}
        for p in rels:
            if p.name not in keep:
                shutil.rmtree(p, ignore_errors=True)
                self.out(f"удалён старый выпуск {p.name}")

    # ---------- службы ----------
    def _watch(self, units, window):
        """Перезапуск служб роли и окно наблюдения. → (успех, {служба: пояснение})."""
        details, ok = {}, True
        for unit in units:
            before = self.svc.show(unit)
            if unit in OPTIONAL_UNITS and before.get("LoadState") == "not-found":
                details[unit] = "нет юнита в старом образе — не оценивается"
                continue
            if before.get("LoadState") in ("error", "bad-setting", "masked"):
                details[unit] = f"юнит не загружен: {before['LoadState']}"
                ok = False
                continue
            started, msg = self.svc.restart(unit, self.cfg.restart_timeout)
            s = self.svc.show(unit)
            if s.get("ConditionResult") == "no" and s.get("ActiveState") != "active":
                details[unit] = "не запускается по условиям юнита (роль/ключи) — не оценивается"
                continue
            if not started or s.get("ActiveState") != "active":
                details[unit] = f"не вышла в READY: {s.get('ActiveState')}/{s.get('SubState')} {s.get('Result', '')} {msg}"
                ok = False
                continue
            details[unit] = s
        watched = {u: s for u, s in details.items() if isinstance(s, dict)}
        if ok and watched:
            deadline = self.clock() + window
            while self.clock() < deadline and ok:
                self.sleep(self.cfg.poll)
                for unit, before in watched.items():
                    now = self.svc.show(unit)
                    if (now.get("ActiveState") != "active" or now.get("InvocationID") != before.get("InvocationID")
                            or now.get("NRestarts") != before.get("NRestarts")):
                        details[unit] = (f"упала в окне наблюдения: {now.get('ActiveState')}/{now.get('SubState')}, "
                                         f"перезапусков {before.get('NRestarts')}→{now.get('NRestarts')}")
                        ok = False
        for unit, s in list(details.items()):
            if isinstance(s, dict):
                details[unit] = f"READY, без перезапусков {window:.0f} с"
        return ok, details

    # ---------- действия ----------
    def _record(self, state, action, result, reason, **extra):
        last = {"action": action, "result": result, "reason": reason, "at": _now(), **extra}
        state["last"] = last
        state["history"] = ([*state.get("history", []), last])[-HISTORY:]
        state["current"] = self.current()
        self.save(state)
        self.out(f"{action}: {result} — {reason}")
        return {"OK": 0, "REJECTED": 2}.get(result, 1)

    def _recover_pending(self, state):
        """Прежний запуск прерван после переключения: возвращаем то, что было до него."""
        p = state.get("pending")
        if isinstance(p, dict):
            if self.current() == p.get("release"):
                self._set_current(p.get("previous"))
            self.out(f"незавершённое переключение на {p.get('release')} отменено")
            state.pop("pending", None)
            self.save(state)

    def _switch(self, state, action, new):
        prev = self.current()
        units = UNITS[self.role()]
        state["pending"] = {"release": new, "previous": prev, "boot_id": self.cfg.boot_id, "at": _now()}
        self.save(state)
        self._set_current(new)
        self.out(f"{action}: {prev or 'заводской'} → {new or 'заводской'}; службы: {', '.join(units)}")
        ok, details = self._watch(units, self.cfg.window)
        if ok:
            state.pop("pending", None)
            if new != prev:
                state["previous"] = prev
            state["current"] = new
            self._gc(state)
            return self._record(state, action, "OK", f"{new or 'заводской код'} работает", release=new,
                                previous=state.get("previous"), units=details)
        self._set_current(prev)
        state.pop("pending", None)
        rb_ok, rb = self._watch(units, self.cfg.rollback_window)
        return self._record(state, action, "ROLLED_BACK" if rb_ok else "BROKEN",
                            f"{new or 'заводской код'} не прошёл проверку служб; возврат на {prev or 'заводской код'}"
                            + ("" if rb_ok else " — НО и прежняя версия не поднялась"),
                            release=prev, rejected=new, units=details, rollback_units=rb)

    def run(self, action):
        if action not in ACTIONS:
            raise DeployError(f"неизвестное действие {action!r}")
        if action == "status":
            return self.status()
        self.dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        os.umask(0o022)
        with open(self.dir / ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.load()
            self._recover_pending(state)
            if action == "install":
                try:
                    data, signature = self._take_incoming()
                    who = sig.verify(data, signature, self.cfg.signers, sig.NS_APP)
                    manifest, files = parse_bundle(data)
                except (DeployError, sig.SignatureError, BundleError) as e:
                    return self._record(state, action, "REJECTED", str(e))
                self.out(f"подпись: {who}; выпуск {manifest['id']} (коммит {manifest['commit'][:12]})")
                try:
                    rid = self._materialize(manifest, files)
                except (DeployError, OSError, launcher.Rejected) as e:
                    return self._record(state, action, "FAILED", f"установка не удалась: {e}")
                return self._switch(state, action, rid)
            if action == "rollback":
                prev = state.get("previous")
                if not prev or prev == self.current():
                    return self._record(state, action, "REJECTED", "нет предыдущего выпуска (используйте factory)")
                try:
                    launcher.verify_release(str(self.releases / prev), self.cfg.require_root)
                except (launcher.Rejected, OSError) as e:
                    return self._record(state, action, "REJECTED", f"предыдущий выпуск {prev} повреждён: {e}")
                return self._switch(state, action, prev)
            if self.current() is None:
                return self._record(state, action, "OK", "уже заводской код")
            return self._switch(state, action, None)

    def status(self):
        state = self.load()
        root, rid, why = launcher.select(str(self.dir), require_root=self.cfg.require_root)
        rels = sorted(p.name for p in self.releases.glob("*") if p.is_dir() and not p.name.startswith(".")) \
            if self.releases.is_dir() else []
        print(json.dumps({"role": self.role(), "current": self.current(), "previous": state.get("previous"),
                          "launcher": why, "code_root": root, "releases": rels, "pending": state.get("pending"),
                          "last": state.get("last")}, ensure_ascii=False, indent=1))
        return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # Коды: 0 — OK, 1 — возврат/сбой установки, 2 — пакет отвергнут (для юнита это итог, SuccessExitStatus=1 2);
    # 64 — неверный вызов (иное имя экземпляра, не root): юнит завершается неуспешно.
    if len(argv) != 1 or argv[0] not in ACTIONS:
        print(f"использование: sheregesh-deployd {{{'|'.join(ACTIONS)}}}", file=sys.stderr)
        return EX_USAGE
    if argv[0] != "status" and os.geteuid() != 0:
        print("sheregesh-deployd: действие выполняется службой sheregesh-deploy@<действие> (root)", file=sys.stderr)
        return EX_USAGE
    try:
        return Installer(out=lambda m: print(m, flush=True)).run(argv[0])
    except DeployError as e:
        print(f"sheregesh-deployd: {e}", file=sys.stderr)
        return EX_USAGE


if __name__ == "__main__":
    sys.exit(main())
