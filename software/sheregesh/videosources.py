# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Список видеоисточников удалённого блока и выбор источника пультом (S4).

Телеметрия status.video.sources — не более MAX_SOURCES объектов {id, input, name, available, active}:
  synthetic       встроенный генератор (есть всегда);
  v4l2:<id>       устройство захвата V4L2; <id> — videoinput.stable_id (VID:PID[:serial], имя ссылки by-path, …),
                  у одинаковых адаптеров без серийного номера — имя ссылки by-path (порт USB);
  rtsp:<n>        n-й адрес из rtsp_urls файла video-input.json (адрес текущего описания «url», которого нет
                  в списке, — следующим номером). В name и id учётных данных и строки запроса нет.
input — synthetic | hdmi | cvbs | usb | v4l2 | rtsp. active — источник текущего описания (ровно один, если описание
указывает на что-то; выбранное, но отсутствующее устройство V4L2 показывается с available=false).

Обнаружение V4L2 — sysfs и ссылки /dev/v4l (videoinput.scan(probe="non-usb"): USB не открываются, платформенные узлы
захвата — только QUERYCAP/ENUMINPUT/ENUMSTD без захвата, чтобы знать CVBS/HDMI): занятые и служебные узлы не
открываются. Доступность RTSP — TCP-соединение с сервером (без сеанса RTSP) в фоновом потоке. Обнаружение и проверки
выполняются не чаще раза в interval_s (2 с); флаг active пересчитывается при каждом снимке (дёшево, без ввода-вывода).
Выбор (resolve) проверяет id по тем же правилам и возвращает описание для videoinput.open_source; сохранение —
save_spec (атомарно, прочие ключи файла, в т. ч. rtsp_urls, не трогаются)."""
import json
import os
import re
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import videoinput

MAX_SOURCES = 16
INTERVAL_S = 2.0
ID_RE = re.compile(r"[A-Za-z0-9._:@+/-]{1,96}")
INPUTS = ("synthetic", "hdmi", "cvbs", "usb", "v4l2", "rtsp")
RTSP_KEYS = ("codec", "transport", "latency_ms", "timeout_s")


def rtsp_items(raw, spec=None):
    """Адреса RTSP: [(n, {"url", "codec"?, "transport"?, "latency_ms"?, "timeout_s"?, "name"?})]. raw — содержимое
    video-input.json (rtsp_urls: строки или объекты; transport — auto | tcp | udp, проверяется при открытии источника,
    как и codec). Неверные элементы пропускаются, номера остальных не сдвигаются. Адрес текущего
    описания (source=rtsp), которого нет в списке, добавляется следующим номером."""
    urls = raw.get("rtsp_urls") if isinstance(raw, dict) else None
    out = []
    for n, item in enumerate(urls if isinstance(urls, list) else []):
        item = {"url": item} if isinstance(item, str) else item
        if not isinstance(item, dict) or not _rtsp_ok(item.get("url")):
            continue
        out.append((n, {k: item[k] for k in ("url", "name", *RTSP_KEYS) if k in item}))
    if spec and str(spec.get("source", "")).lower() == "rtsp" and _rtsp_ok(spec.get("url")) and \
            not any(it["url"] == spec["url"] for _n, it in out):
        n = len(urls) if isinstance(urls, list) else 0
        out.append((n, {"url": spec["url"], **{k: spec[k] for k in RTSP_KEYS if k in spec}}))
    return out


def _rtsp_ok(url):
    if not isinstance(url, str) or len(url) > 1024:
        return False
    try:
        p = urlsplit(url)
        return p.scheme in ("rtsp", "rtsps", "rtspt") and bool(p.hostname)
    except ValueError:
        return False


def rtsp_name(item):
    """Имя для оператора: метка из конфигурации или «RTSP host:port/path» — без учётных данных и строки запроса."""
    label = item.get("name")
    if isinstance(label, str) and label.strip():
        return videoinput.redact(label.strip())[:64]
    p = urlsplit(item["url"])
    port = p.port or (322 if p.scheme == "rtsps" else 554)
    return f"RTSP {p.hostname}:{port}{p.path}"[:64]


def v4l2_nodes(nodes):
    """[(id, Node)] устройств захвата; одинаковые VID:PID без серийного номера различаются портом (by-path)."""
    cap = [n for n in nodes if n.capture]
    ids = [videoinput.stable_id(n) for n in cap]
    out, seen = [], set()
    for node, sid in zip(cap, ids):
        if ids.count(sid) > 1:
            sid = os.path.basename(node.by_path) if node.by_path else node.name
        sid = re.sub(r"[^A-Za-z0-9._:@+/-]", "", sid)[:90]
        if sid and sid not in seen:
            seen.add(sid)
            out.append((f"v4l2:{sid}", node))
    return out


def _node_for(device, nodes):
    """Узел описания V4L2 (как videoinput.open_v4l2: любой селектор, «» — единственное устройство)."""
    cap = [(i, n) for i, n in v4l2_nodes(nodes)]
    if not device:
        return cap[0] if len(cap) == 1 else None
    for i, n in cap:
        if device == i[5:] or device in n.selectors() or (n.usb_id and n.usb_id.startswith(device + ":")):
            return i, n
    return None


def active_id(spec, rtsp, nodes):
    """id источника текущего описания; для отсутствующего устройства V4L2 — id по его селектору."""
    kind = str(spec.get("source") or "synthetic").lower()
    if kind == "synthetic":
        return "synthetic"
    if kind == "rtsp":
        return next((f"rtsp:{n}" for n, it in rtsp if it["url"] == spec.get("url")), "")
    if kind in ("v4l2", "uvc"):
        device = str(spec.get("device") or "")
        found = _node_for(device, nodes)
        if found:
            return found[0]
        label = re.sub(r"[^A-Za-z0-9._:@+/-]", "", os.path.basename(device) if device.startswith("/dev/") else device)
        return f"v4l2:{label[:90] or 'auto'}"
    return ""


def build(spec, rtsp, nodes, rtsp_up=None):
    """Список для телеметрии. rtsp_up — {url: bool} последней проверки (нет — считается доступным)."""
    rtsp_up = rtsp_up or {}
    out = [{"id": "synthetic", "input": "synthetic", "name": videoinput.SyntheticSource.name[:64], "available": True}]
    for sid, n in v4l2_nodes(nodes):
        inp = n.input_type.lower()
        out.append({"id": sid, "input": inp if inp in INPUTS else "v4l2", "name": (n.card or n.name)[:64],
                    "available": True})
    for n, it in rtsp:
        out.append({"id": f"rtsp:{n}", "input": "rtsp", "name": rtsp_name(it), "available": rtsp_up.get(it["url"], True)})
    act = active_id(spec, rtsp, nodes)
    if act and not any(e["id"] == act for e in out):          # выбранное устройство V4L2 сейчас отсутствует
        dev = str(spec.get("device") or "")
        out.insert(1, {"id": act, "input": "usb" if str(spec.get("source")).lower() == "uvc" else "v4l2",
                       "name": videoinput.redact(os.path.basename(dev) or "V4L2 device")[:64], "available": False})
    for e in out:
        e["active"] = e["id"] == act
    if len(out) > MAX_SOURCES:
        keep = out[:MAX_SOURCES]
        if act and not any(e["active"] for e in keep):
            keep[-1] = next(e for e in out if e["active"])
        out = keep
    return out


def resolve(source_id, spec, rtsp, nodes):
    """Описание источника для id из списка или None (неизвестный id). Выбор уже активного источника сохраняет его
    настройки (режим, формат, стандарт)."""
    if not isinstance(source_id, str) or not ID_RE.fullmatch(source_id):
        return None
    if source_id == active_id(spec, rtsp, nodes) and source_id in {e["id"] for e in build(spec, rtsp, nodes)}:
        return dict(spec)
    if source_id == "synthetic":
        return {"source": "synthetic"}
    if source_id.startswith("rtsp:"):
        for n, it in rtsp:
            if source_id == f"rtsp:{n}":
                return {"source": "rtsp", "url": it["url"], "codec": it.get("codec", "h264"),
                        **{k: it[k] for k in ("transport", "latency_ms", "timeout_s") if k in it}}
        return None
    for sid, _n in v4l2_nodes(nodes):
        if sid == source_id:
            return {"source": "v4l2", "device": sid[5:], "mode": "auto", "format": "auto", "standard": "auto"}
    return None


def save_spec(path, spec, spec_keys):
    """Записать описание источника в video-input.json атомарно (временный файл, fsync, rename). Ключи описания
    (spec_keys) заменяются, остальные (rtsp_urls и т. п.) сохраняются. Нечитаемый файл не перезаписывается —
    ValueError; ошибка записи — OSError."""
    path = Path(path)
    raw = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ValueError(f"{path}: unreadable, not overwritten: {e}") from None
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: not a JSON object, not overwritten")
    out = {k: v for k, v in raw.items() if k not in spec_keys}
    out.update({k: v for k, v in spec.items() if k in spec_keys})
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o600                                      # адреса RTSP могут содержать учётные данные
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


class Catalog:
    """Кэш обнаружения для телеметрии. state() → (описание, rtsp_items) текущего выбора; вызывается из любого потока.
    Фоновый поток раз в interval_s: sysfs (без открытия узлов) и TCP-проверка серверов RTSP."""

    def __init__(self, state, scan_kw=None, connect=socket.create_connection, interval_s=INTERVAL_S,
                 rtsp_timeout_s=1.0, clock=time.monotonic):
        self._state, self._scan_kw, self._connect = state, dict(scan_kw or {}), connect
        self.interval_s, self.rtsp_timeout_s, self._clock = interval_s, rtsp_timeout_s, clock
        self._nodes, self._rtsp_up = [], {}
        self.refreshes = 0
        self._last = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def nodes(self):
        """USB — по sysfs без открытия; платформенные узлы захвата — QUERYCAP/ENUMINPUT без захвата (тип CVBS/HDMI)."""
        try:
            return videoinput.scan(probe="non-usb", **self._scan_kw)
        except OSError:
            return []

    def _up(self, url):
        p = urlsplit(url)
        try:
            self._connect((p.hostname, p.port or (322 if p.scheme == "rtsps" else 554)),
                          timeout=self.rtsp_timeout_s).close()
            return True
        except (OSError, ValueError):
            return False

    def refresh(self, force=False):
        """Обнаружение и проверки — не чаще раза в interval_s (force — испытания)."""
        now = self._clock()
        if not force and self._last is not None and now - self._last < self.interval_s:
            return False
        self._last = now
        _spec, rtsp = self._state()
        nodes = self.nodes()
        up = {it["url"]: self._up(it["url"]) for _n, it in rtsp}
        with self._lock:
            self._nodes, self._rtsp_up = nodes, up
            self.refreshes += 1
        return True

    def snapshot(self):
        spec, rtsp = self._state()
        with self._lock:
            nodes, up = self._nodes, dict(self._rtsp_up)
        return build(spec, rtsp, nodes, up)

    def resolve(self, source_id):
        spec, rtsp = self._state()
        return resolve(source_id, spec, rtsp, self.nodes())

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="video-sources", daemon=True)
            self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:                                    # noqa: BLE001 — список не должен ронять блок
                pass
            self._stop.wait(self.interval_s)

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
