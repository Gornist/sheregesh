// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Раздел «Управление»: аппаратура USB, передача команд (включение — с подтверждением), состояние блока, стики.
ColumnLayout {
    id: panel
    spacing: 8 * win.k
    readonly property var sv: backend.sticks
    readonly property var mv: backend.module

    function stateText() {
        switch (sv.support) {
        case "no-link": return qsTr("No link to the remote unit")
        case "old-remote": return qsTr("Not supported by the remote unit")
        }
        switch (sv.remoteState) {
        case "IDLE": return qsTr("No commands")
        case "ACTIVE": return qsTr("Commands are arriving")
        case "FAILSAFE": return qsTr("FAILSAFE: no fresh commands")
        default: return qsTr("No data from the remote unit")
        }
    }
    function reasonText() {
        switch (sv.remoteReason) {
        case "start": return qsTr("No commands since the remote unit started")
        case "fresh": return qsTr("Fresh commands from the console")
        case "disabled": return qsTr("Transmission disabled by the operator")
        case "timeout": return qsTr("No commands for longer than %1 ms: channels centered, buttons released").arg(sv.timeoutMs)
        case "link-lost": return qsTr("Link session lost: channels centered, buttons released")
        case "device-lost": return qsTr("Control device disconnected: channels centered, buttons released")
        default: return sv.remoteReason
        }
    }
    function outputText() {
        if (mv.support !== "ok")
            return qsTr("no data")
        switch (mv.output) {
        case "neutral": return qsTr("CRSF output: neutral")
        case "live": return qsTr("CRSF output: commands go to the module")
        default: return qsTr("no radio output")
        }
    }

    MiniRow {
        objectName: "miniControlDevice"
        label: qsTr("Device")
        value: sv.present ? sv.deviceName : qsTr("not connected")
        valueColor: sv.present ? win.p.text : win.p.warning
    }
    MiniRow {
        label: "USB"
        value: sv.present ? qsTr("connected · %1 axes, %2 buttons").arg(sv.axisCount).arg(sv.buttonCount) : qsTr("no device")
        valueColor: sv.present ? win.p.ok : win.p.warning
    }
    Label {
        visible: sv.notice === "device-lost" && !sv.enabled
        text: qsTr("The control device was disconnected: transmission is off. Enable it again after reconnecting.")
        color: win.p.warning
        font.pixelSize: 12 * win.k
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
    }
    Button {
        objectName: "miniSticksToggle"
        text: sv.enabled ? qsTr("Turn transmission off") : qsTr("Turn transmission on…")
        enabled: sv.enabled || sv.support === "ok"
        highlighted: sv.enabled
        font.pixelSize: 14 * win.k
        Layout.fillWidth: true
        Layout.preferredHeight: 44 * win.k
        onClicked: {
            if (sv.enabled) sv.setEnabled(false, false)        // выключение — сразу, без подтверждения (безопасно)
            else sticksDialog.open()
        }
    }
    Dialog {                                    // REQ-SYS-009: включение — только в отдельном диалоге подтверждения
        id: sticksDialog
        objectName: "miniSticksDialog"
        parent: Overlay.overlay
        anchors.centerIn: Overlay.overlay
        modal: true
        width: Math.min(640 * win.k, win.width - 32 * win.k)
        height: 260 * win.k                     // явная высота: иначе цикл привязок implicitHeight в Fusion Dialog
        standardButtons: Dialog.Ok | Dialog.Cancel
        title: qsTr("Enable command transmission?")
        onOpened: standardButton(Dialog.Ok).forceActiveFocus()   // без сенсора: Enter — да, Esc — нет
        Shortcut {
            sequences: ["Return", "Enter"]
            enabled: sticksDialog.opened
            onActivated: sticksDialog.accept()
        }
        Label {
            objectName: "miniSticksQuestion"
            text: qsTr("Axes and buttons are sent to the remote unit 50 times per second; without fresh commands for %1 ms it goes to FAILSAFE.").arg(sv.timeoutMs)
            wrapMode: Text.WordWrap
            width: sticksDialog.availableWidth
        }
        onAccepted: sv.setEnabled(true, true)
    }
    MiniRow {
        objectName: "miniControlState"
        label: qsTr("Remote unit")
        value: panel.stateText()
        valueColor: sv.remoteState === "FAILSAFE" ? win.p.fault : sv.remoteState === "ACTIVE" ? win.p.ok : win.p.text
    }
    MiniRow { visible: sv.remoteReason !== ""; label: qsTr("Reason"); value: panel.reasonText() }
    MiniRow { label: qsTr("Output"); value: panel.outputText() + (mv.support === "ok" && mv.backendSetting !== "crsf-uart" ? " · SIM" : "") }
    MiniRow {
        label: qsTr("Rate · latency")
        value: sv.rateHz > 0 ? "%1 Hz · %2 ms".arg(win.num(sv.rateHz, 0)).arg(sv.latencyMs >= 0 ? win.num(sv.latencyMs, 0) : "—") : "—"
    }
    Label { text: qsTr("Sticks"); color: win.p.muted; font.pixelSize: 11 * win.k; font.bold: true; Layout.topMargin: 4 * win.k }
    Repeater {
        model: panel.visible ? Math.min(sv.axisCount, 8) : 0       // скрытая панель не перерисовывает стики
        delegate: RowLayout {
            required property int index
            Layout.fillWidth: true
            spacing: 8 * win.k
            Label { text: (index + 1); color: win.p.muted; font.pixelSize: 11 * win.k; Layout.preferredWidth: 14 * win.k }
            Rectangle {
                id: track
                readonly property int level: sv.axes[index] || 0
                Layout.fillWidth: true
                height: 8 * win.k
                radius: 2 * win.k
                color: win.p.bg
                border.color: win.p.border
                Rectangle {
                    readonly property real half: (track.width - 2) / 2
                    x: 1 + half + (track.level < 0 ? half * track.level / 1000 : 0)
                    y: 1
                    width: half * Math.abs(track.level) / 1000
                    height: track.height - 2
                    color: win.p.accent
                }
                Rectangle { x: track.width / 2 - 1; width: 2; height: track.height; color: win.p.muted }
            }
        }
    }
    Flow {
        objectName: "miniButtons"
        visible: sv.buttonCount > 0
        Layout.fillWidth: true
        spacing: 4 * win.k
        Repeater {
            model: panel.visible ? Math.min(sv.buttonCount, 24) : 0
            delegate: Rectangle {
                required property int index
                width: 12 * win.k; height: width; radius: 2 * win.k
                color: ((sv.buttons >> index) & 1) === 1 ? win.p.accent : win.p.bg
                border.color: win.p.border
            }
        }
    }
    MiniDetails { pages: [{page: "SticksPage", label: qsTr("Control page")}] }
}
