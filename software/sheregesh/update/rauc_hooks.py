# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Хуки RAUC и адаптер health для разметки image-rota (прототип варианта C ADR-0009; в образ не входит).

  sheregesh-rauc pre-install    — [handlers] pre-install: охранник «один менеджер», установка только с основного
                                  слота вне пробной загрузки, цель RAUC = неактивный слот image-rota;
  sheregesh-rauc post-install   — [handlers] post-install: сверка цели RAUC с slots.detect(), /var целевого слота
                                  (slots.VarSeeder: /var нового system + образец var-seed + конфигурация пульта),
                                  отпечатки для критериев подтверждения (confirm.expect_from_board) → install.json;
  sheregesh-rauc tryboot        — однократная пробная загрузка слота, назначенного RAUC (set-primary): свежий перенос
                                  конфигурации, счётчик попыток (UPD-004, не больше MAX_BOOT_ATTEMPTS), reboot «0 tryboot»;
  sheregesh-rauc on-boot        — учёт загрузки: попытки исчерпаны и прошивка вернула основной → `rauc status
                                  mark-bad other` (решение записывает RAUC); сбой между autoboot.txt и состоянием;
  sheregesh-rauc health         — на пробной загрузке: критерии confirm.gates (переиспользуются, не копируются) →
                                  PASS: `rauc status mark-good booted` (бэкенд делает слот основным);
                                  FAIL: `rauc status mark-bad booted` + обычная перезагрузка → прошивка грузит [all];
  sheregesh-rauc status         — состояние бэкенда, охранника и последней установки (JSON).
Решения об установке, подтверждении и откате записывает RAUC (status mark-good/mark-bad, set-primary бэкенда);
модули слотов и health только проверяют и исполняют.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from . import confirm, slots
from . import guard as guard_mod
from .model import UpdateError
from .rauc_backend import BackendError, Config, TrybootBackend

MANAGER_UNIT = "rauc.service"          # вместо сокета агента в критерии unit.* (см. RaucProbe)


class HookError(Exception):
    pass


def _env_slots(env):
    """Целевые слоты RAUC из окружения обработчика: {класс: (имя, устройство, bootname)}."""
    out = {}
    for n in env.get("RAUC_TARGET_SLOTS", "").split():
        out[env.get(f"RAUC_SLOT_CLASS_{n}", "")] = (env.get(f"RAUC_SLOT_NAME_{n}"), env.get(f"RAUC_SLOT_DEVICE_{n}"),
                                                    env.get(f"RAUC_SLOT_BOOTNAME_{n}"))
    return out


class Hooks:
    def __init__(self, cfg: Config | None = None, run=subprocess.run, seeder=None, probe=None, detect=slots.detect,
                 env=None, sleep=time.sleep):
        self.cfg = cfg or Config()
        self.run, self.seeder, self.probe, self.detect = run, seeder, probe, detect
        self.env = os.environ if env is None else env
        self.sleep = sleep
        self.backend = TrybootBackend(self.cfg)
        self.guard = self.cfg.guard()

    @property
    def install_file(self):
        return Path(self.cfg.state_dir) / "install.json"

    def _seeder(self):
        return self.seeder or slots.VarSeeder(self.cfg.paths)

    def _rauc(self, *args):
        r = self.run([*self.cfg.rauc_cmd, *args], capture_output=True, text=True)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()

    def _layout_target(self):
        """Слой слотов image-rota (4 независимых признака) и согласие с RAUC: цель — неактивный слот."""
        layout = self.detect(self.cfg.paths)
        tgt = _env_slots(self.env)
        rootfs = tgt.get("rootfs")
        if not rootfs:
            raise HookError(f"RAUC не передал целевой слот rootfs (RAUC_TARGET_SLOTS={self.env.get('RAUC_TARGET_SLOTS')!r})")
        name, dev, bootname = rootfs
        if (bootname or "").upper() != layout.other.name.upper():
            raise HookError(f"RAUC пишет слот {bootname}, а неактивный по image-rota — {layout.other.name.upper()}")
        if os.path.realpath(dev or "") != os.path.realpath(layout.other.system):
            raise HookError(f"устройство RAUC {dev} ≠ неактивный system image-rota {layout.other.system}")
        boot = tgt.get("boot")
        if boot and os.path.realpath(boot[1] or "") != os.path.realpath(layout.other.boot):
            raise HookError(f"устройство RAUC {boot[1]} ≠ неактивный boot image-rota {layout.other.boot}")
        return layout, name, dev

    # ---------- обработчики RAUC ----------
    def pre_install(self):
        ok, why = self.guard.claim(guard_mod.RAUC, "install")
        if not ok:
            raise HookError(why)
        st = self.backend.status()
        if st.get("error"):
            raise HookError(st["error"])
        if st["booted"] != st["committed"] or st["tryboot"]:
            raise HookError(f"установка только с основного слота вне пробной загрузки (загружен {st['booted']}, "
                            f"основной {st['committed']}, tryboot={st['tryboot']})")
        layout, _, dev = self._layout_target()
        return f"pre-install: цель {layout.other.name.upper()} ({dev}), основной {st['committed']} не затрагивается"

    def post_install(self):
        try:
            layout, name, dev = self._layout_target()
            self._seeder().seed(layout, layout.other.name, dev)
            rec = {"target": layout.other.name.upper(), "rauc_slot": name, "version": self.env.get("RAUC_MF_VERSION"),
                   "compatible": self.env.get("RAUC_MF_COMPATIBLE"), "expect": confirm.expect_from_board(self.cfg.root),
                   "time": int(time.time())}
            Path(self.cfg.state_dir).mkdir(parents=True, exist_ok=True)
            slots.write_atomic(self.install_file, json.dumps(rec, ensure_ascii=False, indent=1) + "\n")
        finally:
            self.guard.release(guard_mod.RAUC)
        return f"post-install: /var слота {rec['target']} подготовлен, версия {rec['version']}, отпечатки сняты"

    # ---------- пробная загрузка ----------
    def tryboot(self, reboot=True):
        ok, why = self.guard.check(guard_mod.RAUC, "tryboot")
        if not ok:
            raise HookError(why)
        b = self.backend
        with b.locked():
            st, committed = b.load(), b.committed()
            booted, trying = b.booted()
            trial = st["trial"]
            if not trial:
                raise HookError("пробная загрузка не назначена (RAUC не устанавливал слот или он помечен плохим)")
            if booted != committed or trying:
                raise HookError(f"пробная загрузка только с основного слота (загружен {booted}, tryboot={trying})")
            if trial["attempts"] >= self.cfg.max_attempts:
                raise HookError(f"попытки исчерпаны ({trial['attempts']}/{self.cfg.max_attempts}): "
                                f"rauc status mark-bad other и новая установка")
            layout = self.detect(self.cfg.paths)
            if layout.other.name.upper() != trial["slot"]:
                raise HookError(f"назначенный слот {trial['slot']} не неактивный по image-rota")
            seeder = self._seeder()
            seeder.carry(seeder.target_var(layout, layout.other.name))       # свежая конфигурация пульта
            trial["attempts"] += 1
            b.save(st)
            b._render(committed, trial["slot"])
        if reboot:
            self.run(list(self.cfg.tryboot_cmd), capture_output=True)
        return f"tryboot: слот {trial['slot']} однократно (попытка {trial['attempts']}/{self.cfg.max_attempts})"

    def on_boot(self):
        b = self.backend
        st, committed = b.load(), b.committed()
        booted, trying = b.booted()
        trial = st["trial"]
        if trial and trial["slot"] == committed == booted:
            with b.locked():                       # сбой после записи autoboot.txt, до состояния — завершить
                st = b.load()
                st["slots"][committed], st["trial"] = "good", None
                b.save(st)
            return f"on-boot: {committed} уже основной — подтверждение завершено"
        if trial and booted == committed and not trying and trial["attempts"] >= self.cfg.max_attempts:
            rc, out = self._rauc("status", "mark-bad", "other")
            return f"on-boot: попытки исчерпаны, прошивка вернула {committed}; rauc mark-bad other → rc={rc} {out[-200:]}"
        return f"on-boot: загружен {booted} (tryboot={trying}), основной {committed}, проба {trial}"

    # ---------- health → mark-good / mark-bad ----------
    def health(self, timeout=None, step=10):
        b = self.backend
        s = b.status()
        trial = s.get("trial") or {}
        if s.get("error") or not s["tryboot"] or trial.get("slot") != s["booted"]:
            return {"result": "SKIP", "reason": f"не пробная загрузка назначенного слота: {s}"}
        ok, why = self.guard.check(guard_mod.RAUC, "mark-good")
        if not ok:
            return {"result": "REFUSED", "reason": why}
        try:
            inst = json.loads(self.install_file.read_text())
        except (OSError, ValueError):
            inst = {}
        checks = []
        if inst.get("target") != s["booted"]:
            passed = False
            checks = [{"id": "install-record", "status": "FAIL", "mandatory": True,
                       "detail": f"нет записи установки RAUC для слота {s['booted']} ({self.install_file})"}]
        else:
            state = SimpleNamespace(target=s["booted"], version_target=inst.get("version"), expect=inst.get("expect"))
            layout = self.detect(self.cfg.paths)
            probe = self.probe or RaucProbe(self.cfg.root, self.run)
            deadline = time.monotonic() + (self.cfg.health_timeout if timeout is None else timeout)
            while True:
                passed, checks = confirm.gates(layout, state, probe, lab_video=self.cfg.lab_video)
                if passed or time.monotonic() >= deadline:
                    break
                self.sleep(step)
            checks = [dict(c, id=f"unit.update-manager({MANAGER_UNIT})") if c["id"] == "unit.sheregesh-ota.socket"
                      else c for c in checks]
        failed = [c["id"] for c in checks if c["mandatory"] and c["status"] != "PASS"]
        if passed:
            rc, out = self._rauc("status", "mark-good", "booted")
            result = "COMMITTED" if rc == 0 else "ERROR"
        else:
            rc, out = self._rauc("status", "mark-bad", "booted")
            self.run(list(self.cfg.reboot_cmd), capture_output=True)
            result = "ROLLED_BACK"
        report = {"result": result, "slot": s["booted"], "failed": failed, "rauc_rc": rc, "rauc": out[-400:],
                  "checks": checks, "time": int(time.time())}
        Path(self.cfg.state_dir).mkdir(parents=True, exist_ok=True)
        slots.write_atomic(Path(self.cfg.state_dir) / "last-health.json",
                           json.dumps(report, ensure_ascii=False, indent=1) + "\n")
        return report

    def status(self):
        try:
            inst = json.loads(self.install_file.read_text())
            inst.pop("expect", None)
        except (OSError, ValueError):
            inst = None
        return {"backend": self.backend.status(), "guard": self.guard.facts(), "install": inst}


class RaucProbe(confirm.Probe):
    """confirm.Probe, где критерий «менеджер обновлений active» проверяет rauc.service, а не сокет dev-агента
    (в образе с RAUC агент не управляет слотами). Остальные критерии — без изменений."""

    def unit(self, name):
        return super().unit(MANAGER_UNIT if name == "sheregesh-ota.socket" else name)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmds = ("pre-install", "post-install", "tryboot", "on-boot", "health", "status")
    if len(argv) != 1 or argv[0] not in cmds:
        print(f"использование: sheregesh-rauc {{{'|'.join(cmds)}}}", file=sys.stderr)
        return 2
    try:
        h = Hooks(Config.load())
        cmd = argv[0]
        if cmd in ("health", "status"):
            res = h.health() if cmd == "health" else h.status()
            print(json.dumps(res, ensure_ascii=False))
            return 0 if cmd == "status" or res["result"] in ("COMMITTED", "SKIP") else 1
        print(getattr(h, cmd.replace("-", "_"))())
        return 0
    except (HookError, BackendError, UpdateError, OSError) as e:
        print(f"sheregesh-rauc {argv[0]}: ОТКАЗ: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
