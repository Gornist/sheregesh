// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: page
    function accepted(r) {
        switch (levelBox.currentIndex) {
        case 1: return r.level !== "info"
        case 2: return r.level === "error"
        default: return true
        }
    }
    readonly property var rows: backend.logs.filter(accepted)

    ColumnLayout {
        anchors.fill: parent
        spacing: 12 * win.s
        RowLayout {
            spacing: 12 * win.s
            Layout.fillWidth: true
            Label { text: qsTr("Level:") }
            ComboBox {
                id: levelBox
                objectName: "logLevelBox"
                focus: true
                model: [qsTr("All records"), qsTr("Warnings and errors"), qsTr("Errors only")]
                implicitWidth: 360 * win.s
                KeyNavigation.right: exportButton
                KeyNavigation.down: list
            }
            Label { objectName: "logCount"; text: qsTr("%1 of %2 records").arg(page.rows.length).arg(backend.logs.length); color: win.p.muted }
            Item { Layout.fillWidth: true }
            Button {
                id: exportButton
                objectName: "exportLogsButton"
                text: qsTr("Export to USB")
                KeyNavigation.left: levelBox
                onClicked: backend.exportLogs()
            }
        }
        Label {
            objectName: "logsResult"
            text: backend.lastResult
            visible: text !== ""
            color: win.p.muted
            Layout.fillWidth: true
            elide: Text.ElideRight
        }
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: win.p.panel
            radius: 8 * win.s
            border.color: list.activeFocus ? win.p.accent : win.p.border
            ListView {
                id: list
                objectName: "logList"
                anchors.fill: parent
                anchors.margins: 10 * win.s
                clip: true
                model: page.rows
                keyNavigationEnabled: true
                activeFocusOnTab: true
                KeyNavigation.up: levelBox
                ScrollBar.vertical: ScrollBar {}
                delegate: Label {
                    required property var modelData
                    width: ListView.view.width - 16 * win.s
                    text: modelData.time + "  " + modelData.level.toUpperCase().padEnd(7) + "  " + modelData.source.padEnd(8) + "  " + modelData.text
                    font.family: "DejaVu Sans Mono"
                    font.pixelSize: 17 * win.s
                    color: modelData.level === "error" ? win.p.fault : modelData.level === "warning" ? win.p.warning : win.p.text
                    elide: Text.ElideRight
                }
            }
        }
        Label {
            text: qsTr("Export removes secrets by template and writes a SHA-256 checksum next to the file.")
            color: win.p.muted
            font.pixelSize: 16 * win.s
        }
    }
}
