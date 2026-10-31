# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Тестируемая модель состояний обновления A/B (S3.1; ADR-0006, предложение ADR-0009; REQ-UPD-001…006).

Модель описывает решения, а не выполняет их: реальные действия (запись слота, tryboot, mark-good) делает бэкенд.
Здесь — FakeSlots для испытаний; бэкенд разделов image-rota — slots.BlockSlots (S3.1-DEPLOY, испытан на
файлах-разделах), агент — ota.py; бэкенд RAUC — по решению ADR-0009. Модель не пишет в загрузочные разделы и не
перезагружает плату.

Состояния (персистентны в state.json на разделе данных; переживают перезагрузку и отключение питания):
  IDLE → VERIFIED (подпись, compatible, версия не ниже текущей — AUD-014) → INSTALLING → INSTALLED (неактивный слот
  записан и проверен по хешу) → TRYBOOT (однократная загрузка нового слота) → HEALTH_PENDING (новый слот загрузился,
  ждём sheregesh-health) → COMMITTED (mark-good: новый слот становится основным) | ROLLED_BACK (возврат на прежний).
  FAILED — отказ до записи слота (подпись, совместимость, ошибка записи); прежний слот не затронут.

Инварианты (проверяются тестами):
  1. mark-good только после health PASS на новом слоте;
  2. основной слот меняется только в COMMITTED; до этого любая перезагрузка или отключение питания → прежний слот;
  3. попыток загрузки нового слота не больше MAX_BOOT_ATTEMPTS (UPD-004), затем ROLLED_BACK;
  4. запись идёт только в неактивный слот; активный не изменяется никогда;
  5. откат возвращает и конфигурацию: данные новой версии в /var нового слота, прежний слот видит свой /var.
"""
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

MAX_BOOT_ATTEMPTS = 2
STATES = ("IDLE", "VERIFIED", "INSTALLING", "INSTALLED", "TRYBOOT", "HEALTH_PENDING", "COMMITTED", "ROLLED_BACK",
          "FAILED")


class UpdateError(Exception):
    pass


@dataclass
class UpdateState:
    state: str = "IDLE"
    active: str = "A"                    # основной (подтверждённый) слот
    target: str | None = None            # слот с новой версией
    version_active: str = "1.0.0"
    version_target: str | None = None
    boot_attempts: int = 0
    reason: str = ""
    history: list = field(default_factory=list)
    expect: dict = field(default_factory=dict)  # отпечатки до переключения (роль, ключи, machine-id) — confirm.py
    dev_image: bool = False              # цель — собственный образ по разовому режиму разработчика (REQ-UPD-005)

    def other(self, slot=None):
        return {"A": "B", "B": "A"}[slot or self.active]


class FakeSlots:
    """Бэкенд для испытаний: содержимое слотов и следующая загрузка — в памяти; отказы задаются флагами."""

    def __init__(self):
        self.content = {"A": "1.0.0", "B": None}
        self.tryboot_slot = None          # однократная загрузка (autoboot.txt [tryboot])
        self.default_slot = "A"           # основной (autoboot.txt [all])
        self.fail_write = False
        self.writes = []                  # журнал записей: доказательство «только неактивный слот»

    def write(self, slot, version):
        if self.fail_write:
            raise UpdateError("ошибка записи слота")
        self.writes.append(slot)
        self.content[slot] = version

    def verify(self, slot, version):
        return self.content[slot] == version

    def set_tryboot(self, slot):
        self.tryboot_slot = slot

    def set_default(self, slot):
        self.default_slot = slot

    def boot(self):
        """Что загрузит прошивка: tryboot-слот однократно (флаг сбрасывается), иначе основной."""
        slot, self.tryboot_slot = (self.tryboot_slot or self.default_slot), None
        return slot


def _ver(v):
    return tuple(int(x) for x in v.split("."))


class Updater:
    def __init__(self, backend, state_path: Path | None = None, state: UpdateState | None = None,
                 compatible: str = "sheregesh-pi4"):
        self.b = backend
        self.path = state_path
        self.compatible = compatible
        self.s = state or (self._load() if state_path and Path(state_path).exists() else UpdateState())

    # ---------- персистентность ----------
    def _load(self):
        # Состояние общее для слотов (/persistent): его мог записать агент другой версии. Неизвестные поля
        # пропускаются — иначе после отката на прежний слот агент не запустился бы (pi2, 2026-10-25).
        known = set(UpdateState.__dataclass_fields__)
        return UpdateState(**{k: v for k, v in json.loads(Path(self.path).read_text()).items() if k in known})

    def save(self):
        if self.path:
            d = asdict(self.s)
            if not d["expect"]:     # агент 57e8fe4 (образ pi1/pi2 до S3.2) не знает поля expect и упал бы на нём
                del d["expect"]
            if not d["dev_image"]:  # то же для агентов до режима разработчика (REQ-UPD-005)
                del d["dev_image"]
            tmp = Path(self.path).with_suffix(".tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False))
            tmp.replace(self.path)

    def _go(self, state, reason=""):
        if state not in STATES:
            raise ValueError(state)
        self.s.history.append(f"{self.s.state}->{state}" + (f" ({reason})" if reason else ""))
        self.s.state, self.s.reason = state, reason
        self.save()

    # ---------- шаги ----------
    def verify(self, bundle: dict):
        """bundle: {version, compatible, signature_ok}. Подпись и compatible проверяет RAUC (ADR-0005), здесь — решение."""
        if self.s.state not in ("IDLE", "COMMITTED", "ROLLED_BACK", "FAILED"):
            raise UpdateError(f"обновление уже идёт: {self.s.state}")
        if not bundle.get("signature_ok"):
            return self._go("FAILED", "подпись не проверена")
        if bundle.get("compatible") != self.compatible:
            return self._go("FAILED", f"compatible {bundle.get('compatible')!r}")
        if _ver(bundle["version"]) < _ver(self.s.version_active):
            return self._go("FAILED", "версия ниже текущей (защита от отката, AUD-014)")
        self.s.version_target, self.s.target = bundle["version"], self.s.other()
        self._go("VERIFIED")

    def install(self):
        self._need("VERIFIED")
        if self.s.target == self.s.active:
            raise UpdateError("запись в активный слот запрещена")
        self._go("INSTALLING")
        try:
            self.b.write(self.s.target, self.s.version_target)
        except UpdateError as e:
            return self._go("FAILED", str(e))
        if not self.b.verify(self.s.target, self.s.version_target):
            return self._go("FAILED", "хеш записанного слота не совпал")
        self._go("INSTALLED")

    def request_tryboot(self):
        self._need("INSTALLED")
        self.b.set_tryboot(self.s.target)
        self.s.boot_attempts = 0
        self._go("TRYBOOT")

    def on_boot(self, booted_slot):
        """Вызов при каждой загрузке (служба при старте). Определяет, где мы и что дальше."""
        if self.s.state in ("TRYBOOT", "HEALTH_PENDING"):
            if booted_slot == self.s.target:
                self.s.boot_attempts += 1
                if self.s.boot_attempts > MAX_BOOT_ATTEMPTS:
                    return self._rollback(f"превышено число попыток загрузки ({MAX_BOOT_ATTEMPTS})")
                return self._go("HEALTH_PENDING", f"попытка {self.s.boot_attempts}")
            return self._rollback("прошивка загрузила прежний слот (сбой нового или отключение питания)")
        if self.s.state == "INSTALLING":
            return self._go("FAILED", "запись прервана перезагрузкой; активный слот не изменён")

    def on_health(self, verdict: str, booted_slot):
        """Решение по отчёту sheregesh-health. PASS на новом слоте → mark-good; иначе — перезагрузка в прежний."""
        self._need("HEALTH_PENDING")
        if booted_slot != self.s.target:
            raise UpdateError("health относится не к новому слоту")
        if verdict != "PASS":
            if self.s.boot_attempts < MAX_BOOT_ATTEMPTS:
                self.b.set_tryboot(self.s.target)                 # ещё одна попытка
                return self._go("TRYBOOT", f"health {verdict}, повтор")
            return self._rollback(f"health {verdict}")
        self.b.set_default(self.s.target)                         # mark-good: только здесь меняется основной слот
        self.s.active, self.s.version_active = self.s.target, self.s.version_target
        self.s.target = self.s.version_target = None
        self._go("COMMITTED")

    def reset(self, active: str, version_active: str, reason="сброс оператором"):
        """Возврат в IDLE вне процесса загрузки нового слота (после FAILED, ROLLED_BACK, COMMITTED, до tryboot).
        Слоты не трогает; активный слот и версия берутся из фактического состояния платы."""
        if self.s.state in ("INSTALLING", "TRYBOOT", "HEALTH_PENDING"):
            raise UpdateError(f"сброс невозможен в состоянии {self.s.state}")
        self.s.active, self.s.version_active = active, version_active
        self.s.target = self.s.version_target = None
        self.s.boot_attempts = 0
        self.s.expect = {}
        self.s.dev_image = False
        self._go("IDLE", reason)

    def abort_trial(self, reason):
        """Отказ от пробной загрузки на прежнем слоте (TRYBOOT/HEALTH_PENDING): основной слот не менялся."""
        if self.s.state not in ("TRYBOOT", "HEALTH_PENDING"):
            raise UpdateError(f"нет пробной загрузки: {self.s.state}")
        return self._rollback(reason)

    def _rollback(self, reason):
        self.b.set_tryboot(None)
        self.s.target = self.s.version_target = None
        return self._go("ROLLED_BACK", reason)

    def _need(self, state):
        if self.s.state != state:
            raise UpdateError(f"ожидается {state}, сейчас {self.s.state}")
