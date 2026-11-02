// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Раздел «Диагностика»: общее состояние, канал с блоком, видео, USB, события, экспорт журналов, версии.
ColumnLayout {
    id: panel
    spacing: 8 * win.k
    readonly property var st: backend.status
    readonly property var rx: backend.videoReceiver
    property string exportResult: ""
    property bool exportOk: true
    property bool exportWaiting: false     // выгрузка идёт в фоне: итог — по её завершении (lastOk, lastExport)
    Connections {
        target: backend
        function onConfigBusyChanged() {
            if (panel.exportWaiting && !backend.configBusy) {
                panel.exportWaiting = false
                panel.exportOk = backend.lastOk           // неудача — текст причины, а не имя прошлого файла
                panel.exportResult = backend.lastOk ? qsTr("Saved: %1").arg(backend.lastExport) : backend.lastResult
            }
        }
    }
    function svcState(s) {
        return s === "active" ? qsTr("running") : s === "failed" ? qsTr("failed") : s === "activating" ? qsTr("starting")
             : s === "inactive" ? qsTr("stopped") : s === "deactivating" ? qsTr("stopping") : s
    }
    function startExport(remote) {
        if (remote ? backend.exportRemoteLogs() : backend.exportLogs()) {
            panel.exportResult = ""
            panel.exportWaiting = true
        }
    }

    MiniRow {
        objectName: "miniDiagOverall"
        label: qsTr("Overall")
        value: win.stateText(panel.st.overall) + (panel.st.activeCount > 0 ? " · " + qsTr("active events: %1").arg(panel.st.activeCount) : "")
        valueColor: win.stateColor(panel.st.overall)
    }
    MiniRow {
        objectName: "miniDiagLink"
        label: qsTr("Remote unit (Ethernet)")
        value: win.linkText(panel.st.linkView) + (panel.st.linkRttMs > 0 ? " · " + qsTr("latency %1 ms").arg(win.num(panel.st.linkRttMs, 1)) : "")
        valueColor: win.linkColor(panel.st.linkView)
    }
    MiniRow { label: qsTr("Link security"); value: panel.st.linkTls !== "" ? panel.st.linkTls + " · " + panel.st.linkHost : "—" }
    MiniRow {
        label: qsTr("Video")
        value: qsTr("%1 · %2 fps shown · restarts %3").arg(panel.rx.state === "playing" ? qsTr("playing") : panel.rx.state).arg(win.num(panel.rx.fps, 0)).arg(panel.rx.restarts)
        valueColor: panel.rx.state === "playing" ? win.p.ok : win.p.warning
    }
    MiniRow {
        label: qsTr("USB input")
        value: backend.sticks.present ? backend.sticks.deviceName : qsTr("no device")
        valueColor: backend.sticks.present ? win.p.ok : win.p.warning
    }
    MiniRow { label: qsTr("Remote unit software"); value: panel.st.peerSoftware !== "" ? panel.st.peerSoftware : "—" }
    MiniRow { label: qsTr("Console software"); value: "%1 · %2 %3".arg(panel.st.software).arg(qsTr("protocol")).arg(panel.st.localProtocol) }
    Repeater {                              // REQ-SW-001: обязательные службы пульта и блока (блок — через status)
        model: panel.st.services || []
        MiniRow {
            required property var modelData
            objectName: "miniSvc_" + modelData.name
            label: (modelData.side === "console" ? qsTr("Console service: %1") : qsTr("Remote unit service: %1")).arg(modelData.name)
            value: qsTr("%1 · restarts %2").arg(panel.svcState(modelData.state)).arg(modelData.restarts)
            valueColor: modelData.state === "active" ? win.p.ok : win.p.fault
        }
    }
    Button {
        objectName: "miniExportLogs"
        text: qsTr("Export logs")
        font.pixelSize: 14 * win.k
        Layout.fillWidth: true
        Layout.preferredHeight: 44 * win.k
        enabled: !backend.configBusy
        onClicked: panel.startExport(false)
    }
    Button {
        objectName: "miniExportRemoteLogs"
        text: qsTr("Export remote unit log")
        font.pixelSize: 14 * win.k
        Layout.fillWidth: true
        Layout.preferredHeight: 44 * win.k
        enabled: !backend.configBusy && panel.st.peerState === "connected"
        onClicked: panel.startExport(true)
    }
    RowLayout {
        visible: panel.exportWaiting
        Layout.fillWidth: true
        BusyIndicator { running: panel.exportWaiting; Layout.preferredWidth: 28 * win.k; Layout.preferredHeight: 28 * win.k }
        Label { objectName: "miniExportBusy"; text: qsTr("Exporting…"); color: win.p.muted; font.pixelSize: 13 * win.k }
    }
    Label {
        objectName: "miniExportResult"
        visible: panel.exportResult !== "" && !panel.exportWaiting
        text: panel.exportResult
        color: panel.exportOk ? win.p.muted : win.p.fault
        font.pixelSize: 12 * win.k
        wrapMode: Text.WrapAnywhere
        Layout.fillWidth: true
    }
    MiniDetails {
        pages: [{page: "DiagnosticsPage", label: qsTr("Events")}, {page: "DashboardPage", label: qsTr("Units overview")},
                {page: "LogsPage", label: qsTr("Logs")}]
    }
}
