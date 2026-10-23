// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: page
    readonly property var a: backend.about
    readonly property var st: win.st

    GridLayout {
        anchors.fill: parent
        columns: 2
        rowSpacing: 16 * win.s
        columnSpacing: 16 * win.s

        Tile {
            id: consoleInfo
            objectName: "tileAboutConsole"
            title: qsTr("Console")
            Layout.fillWidth: true
            Layout.preferredWidth: 1
            Layout.fillHeight: true
            focus: true
            KeyNavigation.right: remoteInfo
            GridLayout {
                columns: 2
                columnSpacing: 16 * win.s
                rowSpacing: 6 * win.s
                Layout.fillWidth: true
                Label { text: qsTr("Device name"); color: win.p.muted }
                Label { objectName: "aboutDevice"; text: page.a.device; Layout.fillWidth: true; elide: Text.ElideRight }
                Label { text: qsTr("Software"); color: win.p.muted }
                Label { objectName: "aboutSoftware"; text: page.a.software; Layout.fillWidth: true }
                Label { text: qsTr("Build"); color: win.p.muted }
                Label { text: page.a.build; Layout.fillWidth: true }
                Label { text: qsTr("Hardware"); color: win.p.muted }
                Label { text: page.a.hardware; Layout.fillWidth: true; elide: Text.ElideRight }
                Label { text: qsTr("Protocol"); color: win.p.muted }
                Label { text: page.a.protocol; Layout.fillWidth: true }
                Label { text: qsTr("Configuration"); color: win.p.muted }
                Label { text: qsTr("schema %1, revision %2").arg(page.a.schema).arg(page.a.revision); Layout.fillWidth: true }
                Label { text: qsTr("Platform"); color: win.p.muted }
                Label { text: page.a.platform; Layout.fillWidth: true; elide: Text.ElideRight }
                Label { text: qsTr("Libraries"); color: win.p.muted }
                Label { text: page.a.qt + ", GStreamer " + page.a.gstreamer; Layout.fillWidth: true; elide: Text.ElideRight }
                Label { text: qsTr("Video codec"); color: win.p.muted }
                Label { text: page.a.videoCodec; Layout.fillWidth: true }
            }
        }

        Tile {
            id: remoteInfo
            objectName: "tileAboutRemote"
            title: qsTr("Remote unit")
            Layout.fillWidth: true
            Layout.preferredWidth: 1
            Layout.fillHeight: true
            KeyNavigation.left: consoleInfo
            GridLayout {
                columns: 2
                columnSpacing: 16 * win.s
                rowSpacing: 6 * win.s
                Layout.fillWidth: true
                Label { text: qsTr("Mode"); color: win.p.muted }
                Label { text: st.peerMode === "process" ? qsTr("separate emulator process (127.0.0.1)") : qsTr("emulated inside the console process"); Layout.fillWidth: true; wrapMode: Text.WordWrap }
                Label { text: qsTr("Device ID"); color: win.p.muted }
                Label { objectName: "aboutRemoteId"; text: st.peerDevice !== "" ? st.peerDevice : "—"; Layout.fillWidth: true }
                Label { text: qsTr("Software"); color: win.p.muted }
                Label { text: st.peerSoftware !== "" ? st.peerSoftware : "—"; Layout.fillWidth: true }
                Label { text: qsTr("Hardware"); color: win.p.muted }
                Label { text: st.peerHardware !== "" ? st.peerHardware : "—"; Layout.fillWidth: true; elide: Text.ElideRight }
                Label { text: qsTr("Protocol / session"); color: win.p.muted }
                Label { text: (st.peerProtocol !== "" ? st.peerProtocol : "—") + " / " + (st.peerSession !== "" ? st.peerSession : "—"); Layout.fillWidth: true }
                Label { text: qsTr("Uptime"); color: win.p.muted }
                Label { text: st.remoteUptime >= 0 ? qsTr("%1\u00a0s").arg(st.remoteUptime.toFixed(0)) : "—"; Layout.fillWidth: true }
            }
        }

        Tile {
            objectName: "tileAboutBoundary"
            title: qsTr("Scope of version 1.0")
            Layout.columnSpan: 2
            Layout.fillWidth: true
            Label {
                text: qsTr("Laboratory prototype for safe testing with software emulators and synthetic data. It is not a means of controlling real aircraft and contains no radio control functions.")
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
            Label {
                text: qsTr("Licences: software GPL-3.0-or-later, hardware CERN-OHL-S-2.0, documentation CC BY-SA 4.0.")
                color: win.p.muted
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
        }
    }
}
