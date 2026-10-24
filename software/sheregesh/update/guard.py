# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Охранник «один менеджер A/B» (прототип варианта C ADR-0009; ADR не утверждён).

На плате не должно быть двух компонентов, принимающих решения о слотах: dev-агента S3.1-DEPLOY
(sheregesh-ota-agent, ota.py) и RAUC. Модуль общий для обеих сторон; поведение существующего агента НЕ меняет —
его включает только обёртка GuardedAgent (отдельная точка входа) и хуки RAUC (rauc_hooks, rauc_backend).

Факты (все на общем разделе /persistent, кроме конфигурации RAUC в корне слота):
  • /etc/rauc/system.conf — RAUC сконфигурирован в этом образе ⇒ агент отказывает в install/tryboot/confirm
    (status, abort, enable, disable остаются: abort нужен, чтобы снять незавершённое обновление агента);
  • /persistent/sheregesh-ota/state.json в INSTALLING/VERIFIED/INSTALLED/TRYBOOT/HEALTH_PENDING — обновление агента
    не завершено ⇒ RAUC отказывает (pre-install, set-primary, mark-good). Читается напрямую: агент образа 57e8fe4
    об охраннике не знает, а его незавершённое состояние не должно быть перехвачено;
  • /persistent/sheregesh-rauc/boot-state.json с пробной загрузкой (trial) — обновление RAUC не завершено ⇒ агент
    отказывает в любом изменяющем действии даже без system.conf (например, после отката на слот без RAUC);
  • /persistent/update-manager.json — заявка менеджера на время изменяющего действия; проверка и заявка выполняются
    под общей блокировкой flock(/persistent/update-manager.lock) — так закрыта гонка «оба начали одновременно».
    Заявка жива, пока жив процесс (pid и boot_id совпадают) или пока состояние её владельца не завершено.
"""
import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

RAUC = "rauc"
AGENT = "sheregesh-ota"
AGENT_MUTATING = ("install", "tryboot", "confirm")
AGENT_BUSY = ("VERIFIED", "INSTALLING", "INSTALLED", "TRYBOOT", "HEALTH_PENDING")


@dataclass
class GuardPaths:
    rauc_conf: Path = Path("/etc/rauc/system.conf")
    agent_state: Path = Path("/persistent/sheregesh-ota/state.json")
    rauc_state: Path = Path("/persistent/sheregesh-rauc/boot-state.json")
    claim: Path = Path("/persistent/update-manager.json")
    lock: Path = Path("/persistent/update-manager.lock")
    boot_id: Path = Path("/proc/sys/kernel/random/boot_id")

    @classmethod
    def under(cls, persistent, rauc_conf=None, boot_id=None):
        p = Path(persistent)
        return cls(rauc_conf=Path(rauc_conf) if rauc_conf else cls.rauc_conf,
                   agent_state=p / "sheregesh-ota/state.json", rauc_state=p / "sheregesh-rauc/boot-state.json",
                   claim=p / "update-manager.json", lock=p / "update-manager.lock",
                   boot_id=Path(boot_id) if boot_id else cls.boot_id)


def _json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


@dataclass
class Guard:
    paths: GuardPaths = field(default_factory=GuardPaths)

    # ---------- факты ----------
    def boot_id(self):
        try:
            return Path(self.paths.boot_id).read_text().strip()
        except OSError:
            return ""

    def agent_state(self):
        return (_json(self.paths.agent_state) or {}).get("state", "IDLE")

    def rauc_trial(self):
        return (_json(self.paths.rauc_state) or {}).get("trial")

    def rauc_configured(self):
        return Path(self.paths.rauc_conf).exists()

    def busy(self, manager):
        """Незавершённое обновление менеджера по его собственному постоянному состоянию."""
        if manager == AGENT:
            return self.agent_state() in AGENT_BUSY
        return bool(self.rauc_trial())

    def claim_info(self):
        c = _json(self.paths.claim)
        if not isinstance(c, dict) or c.get("manager") not in (RAUC, AGENT):
            return None
        alive = False
        if c.get("boot_id") == self.boot_id() and isinstance(c.get("pid"), int):
            try:
                os.kill(c["pid"], 0)
                alive = True
            except ProcessLookupError:
                alive = False
            except PermissionError:
                alive = True
        c["live"] = alive or self.busy(c["manager"])
        return c

    def facts(self):
        return {"rauc_configured": self.rauc_configured(), "agent_state": self.agent_state(),
                "rauc_trial": self.rauc_trial(), "claim": self.claim_info()}

    # ---------- решение ----------
    def check(self, manager, action):
        """→ (разрешено, причина). Без блокировки; изменяющие действия — через claim()."""
        if manager not in (RAUC, AGENT):
            return False, f"неизвестный менеджер {manager!r}"
        other = RAUC if manager == AGENT else AGENT
        if manager == AGENT:
            if action not in AGENT_MUTATING:
                return True, "действие не меняет слоты"
            if self.rauc_configured():
                return False, (f"в образе сконфигурирован RAUC ({self.paths.rauc_conf}) — он единственный менеджер "
                               f"обновлений; агент отказывает в {action}")
            if self.rauc_trial():
                return False, f"не завершено обновление RAUC (пробная загрузка {self.rauc_trial()}); агент отказывает"
        elif self.busy(AGENT):
            return False, (f"не завершено обновление агента sheregesh-ota (состояние {self.agent_state()}); "
                           f"RAUC отказывает в {action}; снимите его: sheregesh-ota-client abort")
        c = self.claim_info()
        if c and c["live"] and c["manager"] == other:
            return False, f"слоты заняты менеджером {other} ({c.get('action')}, pid {c.get('pid')})"
        return True, "разрешено"

    @contextmanager
    def _locked(self):
        Path(self.paths.lock).parent.mkdir(parents=True, exist_ok=True)
        with open(self.paths.lock, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def claim(self, manager, action, pid=None):
        """Проверка и заявка под общей блокировкой. → (разрешено, причина)."""
        with self._locked():
            ok, why = self.check(manager, action)
            if not ok:
                return ok, why
            data = {"manager": manager, "action": action, "pid": pid or os.getpid(), "boot_id": self.boot_id(),
                    "time": int(time.time())}
            tmp = Path(str(self.paths.claim) + ".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.paths.claim)
            return True, why

    def release(self, manager):
        with self._locked():
            c = _json(self.paths.claim)
            if isinstance(c, dict) and c.get("manager") == manager:
                Path(self.paths.claim).unlink(missing_ok=True)


# ---------------------------------------------------------------- агент с охранником (отдельная точка входа)


def guarded_agent_class():
    """Подкласс ota.Agent: изменяющие действия только при разрешении охранника. Импорт ota — лениво (ota тянет
    модуль подписи; хукам RAUC он не нужен)."""
    from . import ota

    class GuardedAgent(ota.Agent):
        def __init__(self, cfg=None, guard=None, **kw):
            super().__init__(cfg, **kw)
            self.guard = guard or Guard(GuardPaths.under(Path(self.cfg.state_dir).parent))

        def handle(self, request, stream, emit):
            action = request.get("action")
            if action not in AGENT_MUTATING:
                return super().handle(request, stream, emit)
            ok, why = self.guard.claim(AGENT, action)
            if not ok:
                return {"result": "REFUSED", "reason": why}
            try:
                return super().handle(request, stream, emit)
            finally:
                self.guard.release(AGENT)        # дальше незавершённость видна по state.json агента

    return GuardedAgent


def main(argv=None):
    """sheregesh-update-guard status | check MANAGER ACTION — диагностика (ничего не меняет)."""
    argv = sys.argv[1:] if argv is None else argv
    g = Guard(GuardPaths(rauc_conf=Path(os.environ.get("SHEREGESH_RAUC_SYSTEM_CONF", GuardPaths.rauc_conf))))
    if argv[:1] == ["status"]:
        print(json.dumps(g.facts(), ensure_ascii=False))
        return 0
    if len(argv) == 3 and argv[0] == "check":
        ok, why = g.check(argv[1], argv[2])
        print(json.dumps({"allowed": ok, "reason": why}, ensure_ascii=False))
        return 0 if ok else 1
    print("использование: guard status | check {rauc|sheregesh-ota} ACTION", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
