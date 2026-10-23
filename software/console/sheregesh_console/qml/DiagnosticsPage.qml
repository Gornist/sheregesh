// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: page

    // Фильтр и порядок (активные сверху) — в backend.eventRows; результат слота — готовый массив
    readonly property var rows: backend.eventsRevision, backend.eventRows(filter.currentIndex)

    ColumnLayout {
        anchors.fill: parent
        spacing: 12 * win.s

        RowLayout {
            spacing: 12 * win.s
            Label { text: qsTr("Show:") }
            ComboBox {
                id: filter
                objectName: "filterBox"
                focus: true
                model: [qsTr("All events"), qsTr("Active only"), qsTr("Warnings and errors"), qsTr("Errors only")]
                implicitWidth: 360 * win.s
                KeyNavigation.down: list
            }
            Item { Layout.fillWidth: true }
            Label {
                objectName: "eventCount"
                text: qsTr("%1 of %2 events").arg(page.rows.length).arg(backend.eventCount)
                color: win.p.muted
            }
        }

        ListView {
            id: list
            objectName: "eventsList"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: 6 * win.s
            model: page.rows
            keyNavigationEnabled: true
            activeFocusOnTab: true
            highlightFollowsCurrentItem: true
            KeyNavigation.up: filter
            ScrollBar.vertical: ScrollBar {}
            Label {
                anchors.centerIn: parent
                visible: list.count === 0
                text: qsTr("No events")
                color: win.p.muted
            }
            delegate: Rectangle {
                id: row
                required property var modelData
                required property int index
                width: ListView.view.width - 16 * win.s
                height: col.implicitHeight + 20 * win.s
                radius: 6 * win.s
                color: win.p.panel
                border.width: ListView.isCurrentItem && list.activeFocus ? 3 : 1
                border.color: ListView.isCurrentItem && list.activeFocus ? win.p.accent : win.p.border
                TapHandler { onTapped: { list.currentIndex = row.index; list.forceActiveFocus() } }
                RowLayout {
                    anchors.fill: parent
                    anchors.margins: 10 * win.s
                    spacing: 14 * win.s
                    Rectangle {
                        width: 8 * win.s
                        Layout.fillHeight: true
                        radius: 4 * win.s
                        color: modelData.severity === "error" ? win.p.fault : modelData.severity === "warning" ? win.p.warning : win.p.info
                    }
                    ColumnLayout {
                        id: col
                        Layout.fillWidth: true
                        spacing: 4 * win.s
                        RowLayout {
                            spacing: 12 * win.s
                            Label { text: qsTr("%1 s").arg(modelData.t.toFixed(1)); color: win.p.muted }
                            Label { text: modelData.code; font.bold: true }
                            Label {
                                text: modelData.severity === "error" ? qsTr("error") : modelData.severity === "warning" ? qsTr("warning") : qsTr("info")
                                color: win.p.muted
                            }
                            Item { Layout.fillWidth: true }
                            Label {
                                text: modelData.active ? qsTr("ACTIVE") : modelData.resolved >= 0 ? qsTr("resolved at %1 s").arg(modelData.resolved.toFixed(1)) : qsTr("logged")
                                color: modelData.active ? win.p.warning : win.p.muted
                                font.bold: modelData.active
                            }
                        }
                        Label { text: modelData.text; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                        Label { text: modelData.action; wrapMode: Text.WordWrap; Layout.fillWidth: true; color: win.p.muted; font.pixelSize: 17 * win.s }
                    }
                }
            }
        }
    }
}
