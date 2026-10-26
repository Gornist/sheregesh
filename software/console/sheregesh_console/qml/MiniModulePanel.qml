// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Раздел «Модуль»: профиль, backend (эмулятор — SIM), состояние, интерфейс, выход и RSSI/LQ (только от модуля).
ColumnLayout {
    id: panel
    spacing: 8 * win.k
    readonly property var mv: backend.module
    readonly property bool ok: mv.support === "ok"
    readonly property bool sim: ok && mv.backendSetting !== "crsf-uart"
    readonly property var link: (mv.crsf || {}).link || {}

    function stateText(s) {
        switch (s) {
        case "DISCONNECTED": return qsTr("Module absent")
        case "DETECTED": return qsTr("Module detected")
        case "INITIALIZING": return qsTr("Initializing")
        case "READY": return qsTr("Ready")
        case "FAULT": return qsTr("Fault")
        case "RECOVERING": return qsTr("Recovering")
        case "DISABLED": return qsTr("Disabled")
        default: return s !== "" ? s : "—"
        }
    }
    function supportText() {
        switch (mv.support) {
        case "no-link": return qsTr("No data: no link to the remote unit")
        case "old-remote": return qsTr("The remote unit does not support the external module (protocol %1)").arg(mv.protocolNote)
        case "absent": return qsTr("The remote unit does not report the module state")
        case "local": return qsTr("The external module is available only with a remote unit")
        default: return ""
        }
    }
    function backendText(b) {
        switch (b) {
        case "lab-emulator": return qsTr("laboratory emulator")
        case "crsf-pty-lab": return qsTr("CRSF to the ELRS emulator")
        case "crsf-uart": return qsTr("CRSF over UART")
        default: return b !== "" ? b : "—"
        }
    }

    Rectangle {
        objectName: "miniModuleSim"
        visible: panel.sim
        Layout.fillWidth: true
        implicitHeight: simText.implicitHeight + 10 * win.k
        radius: 4 * win.k
        color: Qt.darker(win.p.warning, 2.2)
        border.color: win.p.warning
        Label {
            id: simText
            anchors.centerIn: parent
            width: parent.width - 16 * win.k
            text: qsTr("SIM — the module is emulated; there is no radio and no real telemetry.")
            font.pixelSize: 12 * win.k
            wrapMode: Text.WordWrap
        }
    }
    Label {
        visible: !panel.ok
        text: panel.supportText()
        color: win.p.warning
        font.pixelSize: 13 * win.k
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
    }
    MiniRow { label: qsTr("Profile"); value: panel.mv.profileName !== "" ? panel.mv.profileName : "—" }
    MiniRow { label: qsTr("Backend"); value: panel.backendText(panel.mv.backendSetting) + (panel.sim ? " · SIM" : "") }
    MiniRow {
        objectName: "miniModuleState"
        label: qsTr("State")
        value: panel.ok ? panel.stateText(panel.mv.state) : "—"
        valueColor: panel.mv.state === "READY" ? win.p.ok : panel.mv.state === "FAULT" ? win.p.fault : win.p.text
    }
    MiniRow { label: qsTr("Interface"); value: panel.mv.profileInterface !== "" ? panel.mv.profileInterface : "—" }
    MiniRow {
        label: qsTr("Output")
        value: !panel.ok ? "—" : panel.mv.output === "live" ? qsTr("commands") : panel.mv.output === "neutral" ? qsTr("neutral") : qsTr("none")
        valueColor: panel.mv.output === "live" ? win.p.ok : win.p.text
    }
    MiniRow {
        visible: panel.mv.isCrsf
        label: "RSSI / LQ"
        value: panel.link.rssi !== undefined && panel.link.rssi !== null
               ? "%1 dBm · %2 %".arg(panel.link.rssi).arg(panel.link.lq !== undefined && panel.link.lq !== null ? panel.link.lq : "—")
                 + (panel.sim ? " · SIM" : "")
               : "—"
    }
    MiniRow {
        visible: panel.mv.uartMissing
        label: "UART"
        value: qsTr("not available on this board (new image required)")
        valueColor: win.p.warning
    }
    MiniRow {
        visible: panel.mv.errorActive || panel.mv.lastErrorText !== ""
        label: qsTr("Error")
        value: panel.mv.lastErrorText !== "" ? panel.mv.lastErrorText : panel.mv.lastErrorCode
        valueColor: panel.mv.errorActive ? win.p.fault : win.p.muted
    }
    MiniDetails { pages: [{page: "ModulePage", label: qsTr("Module page")}] }
}
