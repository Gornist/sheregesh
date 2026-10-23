# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Каталог диагностических событий (REQ-UI-003): код, важность, текст и действия RU/EN."""
from dataclasses import dataclass

INFO, WARNING, ERROR = "info", "warning", "error"
SEVERITY_ORDER = {INFO: 0, WARNING: 1, ERROR: 2}


@dataclass(frozen=True)
class Fault:
    code: str
    severity: str
    text: dict      # язык → шаблон с {параметрами}
    action: dict    # язык → рекомендуемые действия
    latched: bool = True   # True — состояние (активно/устранено); False — разовое событие журнала


CATALOG = {f.code: f for f in [
    Fault("TEMP-HIGH", WARNING,
          {"ru": "Высокая температура {sensor}: {value:.1f} °C (порог {limit:.0f} °C)",
           "en": "High {sensor} temperature: {value:.1f} °C (limit {limit:.0f} °C)"},
          {"ru": "Проверьте вентиляцию и нагрузку; вентилятор переведён на повышенные обороты.",
           "en": "Check ventilation and load; the fan has been sped up."}),
    Fault("TEMP-CRIT", ERROR,
          {"ru": "Критическая температура {sensor}: {value:.1f} °C (порог {limit:.0f} °C)",
           "en": "Critical {sensor} temperature: {value:.1f} °C (limit {limit:.0f} °C)"},
          {"ru": "Запрошено штатное выключение. Устраните причину перегрева до повторного включения.",
           "en": "A clean shutdown has been requested. Remove the cause of overheating before restarting."}),
    Fault("TEMP-NODATA", WARNING,
          {"ru": "Нет данных датчиков температуры", "en": "No temperature sensor data"},
          {"ru": "Вентилятор работает на полных оборотах. Проверьте службу диагностики.",
           "en": "The fan runs at full speed. Check the diagnostics service."}),
    Fault("FAN-TACH", WARNING,
          {"ru": "Нет сигнала оборотов вентилятора", "en": "No fan speed signal"},
          {"ru": "Вентилятор переведён на 100 %. Проверьте разъём вентилятора при обслуживании.",
           "en": "The fan is set to 100 %. Check the fan connector during service."}),
    Fault("FAN-CTRL", WARNING,
          {"ru": "Вентилятор не управляется: {rpm} об/мин при заданных {duty:.0f} %",
           "en": "Fan does not respond to control: {rpm} rpm at {duty:.0f} % requested"},
          {"ru": "Охлаждение продолжает работать на полных оборотах. Проверьте линию управления при обслуживании.",
           "en": "Cooling continues at full speed. Check the control line during service."}),
    Fault("FAN-STALL", ERROR,
          {"ru": "Вентилятор остановлен при заданных {duty:.0f} %", "en": "Fan stalled at {duty:.0f} % requested"},
          {"ru": "Охлаждение не работает. При дальнейшем росте температуры будет выполнено штатное выключение.",
           "en": "Cooling has failed. A clean shutdown will follow if the temperature keeps rising."}),
    Fault("PWR-LOW", WARNING,
          {"ru": "Низкий заряд: {percent:.0f} %", "en": "Low battery: {percent:.0f} %"},
          {"ru": "Подключите внешнее питание.", "en": "Connect external power."}),
    Fault("PWR-CRIT", ERROR,
          {"ru": "Критический заряд: {percent:.0f} %", "en": "Critical battery: {percent:.0f} %"},
          {"ru": "Подключите внешнее питание; при сигнале модуля будет выполнено штатное выключение.",
           "en": "Connect external power; a clean shutdown follows on the module signal."}),
    Fault("PWR-NODATA", INFO,
          {"ru": "Телеметрия питания недоступна", "en": "Power telemetry unavailable"},
          {"ru": "Показания питания не отображаются. Проверьте модуль питания.",
           "en": "Power readings are not shown. Check the power module."}),
    Fault("PWR-SOURCE", INFO,
          {"ru": "Источник питания: {source}", "en": "Power source: {source}"},
          {"ru": "Действий не требуется.", "en": "No action required."}, latched=False),
    Fault("PEER-LOST", WARNING,
          {"ru": "Нет связи с удалённым блоком более {timeout:.0f} с", "en": "No link to the remote unit for over {timeout:.0f} s"},
          {"ru": "Проверьте кабель Ethernet и питание удалённого блока. Переподключение автоматическое.",
           "en": "Check the Ethernet cable and remote unit power. Reconnection is automatic."}),
    Fault("PEER-INCOMPAT", ERROR,
          {"ru": "Несовместимая версия протокола удалённого блока: {peer} (пульт {local})",
           "en": "Incompatible remote unit protocol version: {peer} (console {local})"},
          {"ru": "Обновите ПО одного из устройств до совместимой версии.",
           "en": "Update the software of one device to a compatible version."}),
    Fault("CFG-RECOVERED", WARNING,
          {"ru": "Конфигурация восстановлена при запуске ({detail})", "en": "Configuration recovered at startup ({detail})"},
          {"ru": "Проверьте настройки; подробности — в журнале.", "en": "Review the settings; see the log for details."},
          latched=False),
    Fault("CFG-ROLLBACK", WARNING,
          {"ru": "Ревизия {revision} не прошла проверку служб и отменена; активна {active}",
           "en": "Revision {revision} failed the service check and was reverted; {active} is active"},
          {"ru": "Исправьте значения и примените снова.", "en": "Correct the values and apply again."}, latched=False),
    Fault("CFG-APPLIED", INFO,
          {"ru": "Конфигурация применена: ревизия {revision}", "en": "Configuration applied: revision {revision}"},
          {"ru": "Действий не требуется.", "en": "No action required."}, latched=False),
    Fault("SYS-SHUTDOWN-REQ", ERROR,
          {"ru": "Запрошено штатное выключение: {reason}", "en": "Clean shutdown requested: {reason}"},
          {"ru": "Эмуляция: выключение не выполняется, событие записано.",
           "en": "Emulation: no shutdown is performed, the event is logged."}, latched=False),
    Fault("EMU-TEST", WARNING,
          {"ru": "Тестовое диагностическое событие №{detail}", "en": "Test diagnostic event #{detail}"},
          {"ru": "Событие создано генератором эмулятора; действий не требуется.",
           "en": "Event created by the emulator generator; no action required."}),
]}


def render(code, lang, params):
    f = CATALOG[code]
    try:
        return f.text[lang].format(**params), f.action[lang]
    except (KeyError, ValueError, IndexError):
        return f.text[lang], f.action[lang]
