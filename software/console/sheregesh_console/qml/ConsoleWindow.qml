// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Общее окно пульта: масштаб, палитра, функции текстов состояний. Наследуют Main.qml (вкладки) и
// MainMinimal.qml (видео на весь экран); страницы обращаются к ним через id win корня.
ApplicationWindow {
    objectName: "mainWindow"
    width: 1920
    height: 1200
    visible: true
    title: qsTr("Sheregesh — console (laboratory emulation)")

    readonly property real s: backend.uiScale / 100
    readonly property var p: backend.palette
    readonly property var st: backend.status

    function stateColor(state) {
        return state === "fault" ? p.fault : state === "warning" ? p.warning : state === "boot" ? p.boot : p.ok
    }
    function num(v, digits) {        // дробная часть: запятая в RU, точка в EN; без разделителя тысяч
        var t = Number(v).toFixed(digits)
        return backend.language === "ru" ? t.replace(".", ",") : t
    }
    // Обобщённое состояние канала с удалённым блоком (backend.link_view): индикатор виден на всех экранах
    function linkColor(v) {
        return v === "CONNECTED" ? p.ok : v === "ERROR" ? p.fault : v === "CONNECTING" ? p.info : p.warning
    }
    function linkText(v) {
        return v === "CONNECTED" ? qsTr("Remote unit: connected") : v === "ERROR" ? qsTr("Remote unit: link error")
             : v === "CONNECTING" ? qsTr("Remote unit: connecting…") : qsTr("Remote unit: no link")
    }
    // Источник видео (backend.videoSource, sheregesh_console.videosource): тип входа и состояние источника
    function videoInputText(input) {
        return input === "synthetic" ? qsTr("Synthetic") : input === "hdmi" ? "HDMI" : input === "cvbs" ? "CVBS"
             : input === "usb" ? qsTr("USB (UVC)") : input === "v4l2" ? "V4L2" : input === "rtsp" ? "RTSP"
             : input !== "" ? input.toUpperCase() : qsTr("unknown")
    }
    function videoSourceStateText(state) {
        return state === "streaming" ? qsTr("streaming") : state === "no-signal" ? qsTr("no signal")
             : state === "no-device" ? qsTr("no device") : state === "starting" ? qsTr("starting")
             : state === "connect-error" ? qsTr("connection error") : state === "error" ? qsTr("error")
             : state === "reconnecting" ? qsTr("reconnecting") : qsTr("not reported")
    }
    function stateText(state) {
        return state === "fault" ? qsTr("Fault") : state === "warning" ? qsTr("Warning")
             : state === "boot" ? qsTr("Starting") : qsTr("Normal")
    }

    color: p.bg
    font.pixelSize: 20 * s
    palette.window: p.bg
    palette.windowText: p.text
    palette.base: p.panel
    palette.alternateBase: p.bg
    palette.text: p.text
    palette.button: p.panel
    palette.buttonText: p.text
    palette.highlight: p.accent
    palette.highlightedText: "#ffffff"
    palette.placeholderText: p.muted
    palette.mid: p.border
    palette.light: p.panel
    palette.dark: p.border

}
