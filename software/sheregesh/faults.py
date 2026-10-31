# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Каталог диагностических событий (REQ-UI-003): код, важность, текст и действия RU/EN."""
import re
import string
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
    Fault("LINK-AUTH-FAIL", ERROR,
          {"ru": "Отказ аутентификации канала с удалённым блоком: {reason}",
           "en": "Remote unit link authentication failed: {reason}"},
          {"ru": "Проверьте сертификаты устройств и CA стенда (sheregesh-link status); соединение без аутентификации не устанавливается.",
           "en": "Check device certificates and the lab CA (sheregesh-link status); no connection is made without authentication."}),
    Fault("PEER-LOST", WARNING,
          {"ru": "Нет связи с удалённым блоком более {timeout:.0f} с", "en": "No link to the remote unit for over {timeout:.0f} s"},
          {"ru": "Проверьте кабель Ethernet и питание удалённого блока. Переподключение автоматическое.",
           "en": "Check the Ethernet cable and remote unit power. Reconnection is automatic."}),
    Fault("PEER-SLOW", WARNING,
          {"ru": "Задержка heartbeat удалённого блока: {age:.1f} с (таймаут {timeout:.0f} с)",
           "en": "Remote unit heartbeat delayed: {age:.1f} s (timeout {timeout:.0f} s)"},
          {"ru": "Связь сохраняется; проверьте нагрузку удалённого блока и линию.",
           "en": "The link is kept; check the remote unit load and the line."}),
    Fault("SVC-RESTART", WARNING,
          {"ru": "Служба {service} перезапущена после сбоя (перезапусков с загрузки: {restarts})",
           "en": "Service {service} was restarted after a failure (restarts since boot: {restarts})"},
          {"ru": "Работа продолжается. Если повторяется, сохраните журналы (экспорт) и сообщите разработчикам.",
           "en": "Operation continues. If it repeats, export the logs and report to the developers."}, latched=False),
    Fault("SVC-DOWN", ERROR,
          {"ru": "Не работает служба: {services}", "en": "Service not running: {services}"},
          {"ru": "Функции этой службы недоступны. Перезапустите устройство; если не помогло — экспорт журналов.",
           "en": "Functions of this service are unavailable. Restart the device; if that does not help, export the logs."}),
    Fault("LINK-BAD-MSG", WARNING,
          {"ru": "Повреждённое сообщение от удалённого блока отброшено ({reason}, всего {count})",
           "en": "Corrupted message from the remote unit discarded ({reason}, total {count})"},
          {"ru": "Сообщение не обработано. При повторении соединение будет переустановлено.",
           "en": "The message was not processed. The link is re-established if this repeats."}, latched=False),
    Fault("LINK-HB-TIMEOUT", INFO,
          {"ru": "Соединение переустанавливается: нет heartbeat {age:.1f} с",
           "en": "Reconnecting: no heartbeat for {age:.1f} s"},
          {"ru": "Действий не требуется; переподключение автоматическое.", "en": "No action required; reconnection is automatic."},
          latched=False),
    Fault("REMOTE-EVENT", INFO,
          {"ru": "Удалённый блок сообщил событие {code}", "en": "The remote unit reported event {code}"},
          {"ru": "Подробности — в журнале удалённого блока.", "en": "See the remote unit log for details."}, latched=False),
    Fault("REMOTE-ALERT", WARNING,
          {"ru": "Удалённый блок сообщает об активных событиях: {codes}", "en": "The remote unit reports active events: {codes}"},
          {"ru": "Проверьте состояние удалённого блока на экране диагностики.",
           "en": "Check the remote unit state on the diagnostics screen."}),
    Fault("VIDEO-LOST", WARNING,
          {"ru": "Нет видеопотока", "en": "Video stream missing"},
          {"ru": "Интерфейс работает; поток восстановится автоматически.", "en": "The interface keeps working; the stream recovers automatically."}),
    Fault("PEER-INCOMPAT", ERROR,
          {"ru": "Удалённый блок несовместим: протокол {peer}, пульт {local} (причина: {reason})",
           "en": "Remote unit incompatible: protocol {peer}, console {local} (reason: {reason})"},
          {"ru": "Обновите ПО одного из устройств до совместимой версии.",
           "en": "Update the software of one device to a compatible version."}),
    Fault("MODULE-FAULT", WARNING,
          {"ru": "Внешний модуль: ошибка {code}", "en": "External module fault: {code}"},
          {"ru": "Состояние и восстановление — на экране «Модуль».",
           "en": "State and recovery are shown on the «Module» screen."}),
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


class _RuFormatter(string.Formatter):
    """Дробные значения с запятой: 75,3 °C."""
    def format_field(self, value, spec):
        text = super().format_field(value, spec)
        return text.replace(".", ",") if isinstance(value, float) else text


_FORMATTERS = {"ru": _RuFormatter(), "en": string.Formatter()}
_UNIT = re.compile(r"(\d) (°C|%|с|мс|s|ms|об/мин|rpm)(?=[^\w%]|$)")


def render(code, lang, params):
    """Текст события и действия на языке lang; число и единица связаны неразрывным пробелом."""
    f = CATALOG[code]
    try:
        text = _FORMATTERS[lang].format(f.text[lang], **params)
    except (KeyError, ValueError, IndexError):
        text = f.text[lang]
    return _UNIT.sub("\\1\u00a0\\2", text), f.action[lang]
