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


    // Сообщения backend.message (итоги фоновых операций, откат настроек, выключение блока) — всплывающая строка
    // поверх любого экрана; последние 20 — в notices. Касание закрывает.
    id: cw
    readonly property real uk: Math.min(width / 1280, height / 800)
    property var notices: []
    property var notice: null
    Connections {
        target: backend
        function onMessage(level, text) {
            cw.notice = {level: level, text: text, time: new Date()}
            cw.notices = [cw.notice].concat(cw.notices).slice(0, 20)
            toastTimer.interval = level === "error" ? 12000 : level === "warning" ? 8000 : 5000
            toastTimer.restart()
        }
    }
    Timer { id: toastTimer; onTriggered: cw.notice = null }
    Rectangle {
        objectName: "toast"
        parent: Overlay.overlay
        visible: cw.notice !== null
        z: 900000
        width: Math.min(toastText.implicitWidth + 40 * cw.uk, cw.width * 0.7)
        height: toastText.implicitHeight + 24 * cw.uk
        anchors.horizontalCenter: parent.horizontalCenter
        y: cw.height * 0.12
        radius: 8 * cw.uk
        color: !cw.notice ? cw.p.panel : cw.notice.level === "error" ? "#f0a3261f"
             : cw.notice.level === "warning" ? "#f08a5a00" : "#f01f4f73"
        border.color: "#80ffffff"
        Label {
            id: toastText
            objectName: "toastText"
            anchors.centerIn: parent
            width: Math.min(implicitWidth, cw.width * 0.7 - 40 * cw.uk)
            text: cw.notice ? cw.notice.text : ""
            color: "#ffffff"
            font.pixelSize: 16 * cw.uk
            wrapMode: Text.WordWrap
            horizontalAlignment: Text.AlignHCenter
        }
        MouseArea { anchors.fill: parent; onClicked: cw.notice = null }
    }

    // Экранная клавиатура: редактируемое поле получило фокус касанием (не программно и не Tab — открытие настроек
    // её не показывает); поле в нижней половине экрана — клавиатура сверху
    function touchFocused(item) {
        var c = item.focusReason !== undefined ? item : item.parent    // поле SpinBox: причина — у самого SpinBox
        return c !== null && c.focusReason === Qt.MouseFocusReason
    }
    readonly property Item editTarget: activeFocusItem && activeFocusItem.cursorPosition !== undefined
                                       && typeof activeFocusItem.insert === "function" && !activeFocusItem.readOnly
                                       && touchFocused(activeFocusItem) ? activeFocusItem : null
    OnScreenKeyboard {
        parent: Overlay.overlay
        z: 950000
        visible: cw.editTarget !== null
        target: cw.editTarget
        k: cw.uk
        width: cw.width
        y: cw.editTarget && cw.editTarget.mapToItem(null, 0, 0).y > cw.height / 2 ? 0 : cw.height - height
    }

    // REQ-UI-005: погашенный экран — чёрный слой поверх всего; ввод до пробуждения поглощает screenGuard
    Rectangle {
        objectName: "screenBlank"
        parent: Overlay.overlay
        anchors.fill: parent
        z: 1000000
        color: "black"
        visible: screenGuard.blanked
    }
}
