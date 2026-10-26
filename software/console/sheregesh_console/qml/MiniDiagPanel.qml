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

    MiniRow {
        objectName: "miniDiagOverall"
        label: qsTr("Overall")
        value: win.stateText(panel.st.overall) + (panel.st.activeCount > 0 ? " · " + qsTr("active events: %1").arg(panel.st.activeCount) : "")
        valueColor: win.stateColor(panel.st.overall)
    }
    MiniRow {
        objectName: "miniDiagLink"
        label: qsTr("Remote unit (Ethernet)")
        value: win.linkText(panel.st.linkView) + (panel.st.linkRttMs > 0 ? " · RTT %1 ms".arg(win.num(panel.st.linkRttMs, 1)) : "")
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
    Button {
        objectName: "miniExportLogs"
        text: qsTr("Export logs")
        font.pixelSize: 14 * win.k
        Layout.fillWidth: true
        Layout.preferredHeight: 44 * win.k
        onClicked: panel.exportResult = backend.exportLogs()
    }
    Label {
        visible: panel.exportResult !== ""
        text: qsTr("Saved: %1").arg(panel.exportResult)
        color: win.p.muted
        font.pixelSize: 12 * win.k
        wrapMode: Text.WrapAnywhere
        Layout.fillWidth: true
    }
    MiniDetails {
        pages: [{page: "DiagnosticsPage", label: qsTr("Events")}, {page: "DashboardPage", label: qsTr("Units overview")},
                {page: "LogsPage", label: qsTr("Logs")}]
    }
}
