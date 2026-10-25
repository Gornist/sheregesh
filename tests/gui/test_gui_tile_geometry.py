# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Геометрия плиток главного экрана при длинных строках (дефект E2E hw gui-pi1/03-04-lost-dashboard.png).

На 1280×800 при обрыве («Переподключение…», «Удалённый блок: remote-emu-01 · ПО 0.1.0.dev0») текст плитки связи
выходил за правый край плитки. test_e2e_gui_screens этого не ловил: он снимает только CONNECTED и DISCONNECTED
(«Нет связи» — короткая строка), а RECONNECTING с длинной крупной надписью не проверялся. Причина: крупная надпись
плитки без Layout.fillWidth имеет в ColumnLayout фиксированную ширину = implicitWidth, колонка не может сжаться уже
неё, и вместе с ней расширяются и переносимые строки (они переносятся по ширине «Переподключение…», а не плитки).

Третья причина — шрифт: на пульте (Debian) DejaVu Sans заметно шире шрифта по умолчанию macOS, и «Переподключение…»
помещалось на машине разработчика. Поэтому здесь шрифт приложения — DejaVu Sans, а без него Verdana (та же ширина,
семейство Bitstream Vera), если есть.

Здесь состояние задаётся детерминированно (бэкенд без таймера, поля status подменяются) с худшими длинными строками;
проверка — offscreen (L3), не физический дисплей.
"""
import pytest
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication
from PySide6.QtQuickControls2 import QQuickStyle

from sheregesh_console.app import PAGE_NAMES, Ui, compile_translations
from sheregesh_console.backend import Backend
from sheregesh_console.qtwait import pump
from test_e2e_gui_screens import TILES, find, outside_window, rect, tile_overflow

QQuickStyle.setStyle("Fusion")
SIZES = [(1280, 800), (1920, 1200)]

LONG_DEVICE = "remote-emu-01-laboratory-bench-unit"
STATES = {
    "reconnecting": {"peerLink": "RECONNECTING", "linkView": "CONNECTING", "peerState": "disconnected",
                     "peerDetail": "refused", "peerDevice": "remote-emu-01", "peerSoftware": "0.1.0.dev0",
                     "peerProtocol": "1.1", "linkSecure": True, "linkTls": "", "linkHost": "192.168.1.171",
                     "linkRttMs": -1, "peerHeartbeatAge": -1, "remoteAvailable": False},
    "reconnecting-long": {"peerLink": "RECONNECTING", "linkView": "CONNECTING", "peerState": "disconnected",
                          "peerDetail": "handshake-timeout", "peerDevice": LONG_DEVICE,
                          "peerSoftware": "0.1.0.dev0+g43ea9a9.dirty", "peerRole": "remote",
                          "peerProtocol": "1.1", "linkSecure": True, "linkTls": "TLSv1.3",
                          "linkHost": "fd00:5e:4e:1::171", "linkRttMs": 1234.5, "linkAuthFailures": 12,
                          "peerHeartbeatAge": 12345.6, "remoteAvailable": False},
    "auth-failed": {"peerLink": "AUTH_FAILED", "linkView": "ERROR", "peerState": "disconnected",
                    "peerDetail": "auth certificate verify failed: unable to get local issuer certificate",
                    "peerDevice": LONG_DEVICE, "peerSoftware": "0.1.0.dev0", "linkSecure": True,
                    "linkTls": "", "linkHost": "192.168.1.171", "linkRttMs": -1, "linkAuthFailures": 3},
    "incompatible": {"peerLink": "INCOMPATIBLE", "linkView": "ERROR", "peerState": "incompatible",
                     "peerDetail": "protocol 2.0 is not supported (console 1.1)", "peerDevice": LONG_DEVICE,
                     "peerSoftware": "2.0.0", "linkSecure": False, "peerMode": "process"},
}


def device_like_font():
    """Шрифт с метриками устройства: DejaVu Sans (Debian), иначе Verdana; None — оставить системный."""
    families = set(QFontDatabase.families())
    return next((f for f in ("DejaVu Sans", "Verdana") if f in families), None)


@pytest.fixture
def wide_font(qapp):
    old = QGuiApplication.font()
    family = device_like_font()
    if family:
        f = QFont(old)
        f.setFamily(family)
        QGuiApplication.setFont(f)
    yield family
    QGuiApplication.setFont(old)


@pytest.fixture
def ui(qapp, tmp_path, wide_font):
    compile_translations()
    u = Ui(Backend(tmp_path / "data", autostart=False))
    yield u
    u.close()


def text_problems(u):
    """Надпись в плитке: нарисованный текст не шире самого элемента (иначе он вылезает за элемент, даже если
    элемент формально внутри плитки)."""
    bad = []
    for name in TILES:
        def walk(item):
            for ch in item.childItems():
                if not ch.isVisible() or ch.width() <= 0:
                    continue
                if ch.inherits("QQuickText") and ch.property("contentWidth") > ch.width() + 1:
                    bad.append(f"{name}/{ch.objectName() or ch.property('text')[:30]} шире элемента")
                walk(ch)
        walk(find(u, name))
    return bad


def force_status(u, **fields):
    u.backend._status.update(fields)
    u.backend.statusChanged.emit()
    pump(60)


@pytest.mark.test_id("TEST-UI-002")
@pytest.mark.parametrize("state", list(STATES))
@pytest.mark.parametrize("lang", ["ru", "en"])
def test_link_tile_text_stays_inside_tile(ui, state, lang):
    if ui.backend.language != lang:
        form = dict(ui.backend.config)
        form.update(language=lang)
        ui.backend.applyForm(form, True)
        pump(60)
    force_status(ui, **STATES[state])
    ui.window.setProperty("currentPage", PAGE_NAMES.index("dashboard"))
    for w, h in SIZES:
        ui.window.resize(w, h)
        pump(200)
        problems = tile_overflow(ui) + outside_window(ui) + text_problems(ui)
        assert problems == [], f"{lang} {w}×{h} {state}: {problems}"
        peer = find(ui, "peerValue")
        tile = rect(find(ui, "tileLink"))
        assert rect(peer).width() <= tile.width(), "крупная надпись шире плитки"
        assert peer.property("text"), "надпись состояния пуста"
        assert not peer.property("truncated"), "крупная надпись сокращена — должна уменьшаться, а не обрезаться"


@pytest.mark.test_id("TEST-UI-002")
def test_reconnecting_identity_wraps_inside_link_tile_at_1280(ui):
    """Тот самый текст дефекта: «Удалённый блок: remote-emu-01 · ПО 0.1.0.dev0» переносится в пределах плитки."""
    force_status(ui, **STATES["reconnecting"])
    ui.window.setProperty("currentPage", PAGE_NAMES.index("dashboard"))
    ui.window.resize(1280, 800)
    pump(200)
    tile = rect(find(ui, "tileLink"))
    for name in ("peerValue", "peerDetail", "peerIdentity"):
        r = rect(find(ui, name))
        assert tile.left() <= r.left() and r.right() <= tile.right(), (name, r, tile)
    assert "remote-emu-01" in find(ui, "peerIdentity").property("text")
