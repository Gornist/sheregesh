// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: page
    readonly property var st: win.st
    readonly property real big: 34 * win.s

    GridLayout {
        id: grid
        anchors.fill: parent
        columns: 3
        rowSpacing: 16 * win.s
        columnSpacing: 16 * win.s

        Tile {
            id: tileState
            objectName: "tileState"
            title: qsTr("System state")
            accent: win.stateColor(st.overall)
            Layout.fillWidth: true
            Layout.preferredWidth: 1
            Layout.fillHeight: true
            focus: true
            KeyNavigation.right: tilePower
            KeyNavigation.down: tileTemp
            Label { objectName: "overallLabel"; text: win.stateText(st.overall); font.pixelSize: page.big; font.bold: true; color: win.stateColor(st.overall) }
            RowLayout {
                spacing: 10 * win.s
                Rectangle { objectName: "indicatorLamp"; width: 22 * win.s; height: width; radius: width / 2; color: win.stateColor(st.indicator) }
                Label { Layout.fillWidth: true; wrapMode: Text.WordWrap; text: qsTr("Status indicator: %1").arg(win.stateText(st.indicator)) }
            }
            Label {
                visible: st.shutdownRequested !== ""
                text: qsTr("Shutdown requested (emulation): %1").arg(st.shutdownRequested)
                color: win.p.fault
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
        }

        Tile {
            id: tilePower
            objectName: "tilePower"
            title: qsTr("Power")
            accent: !st.powerAvailable ? win.p.muted : st.lowBattery ? win.p.fault : win.p.ok
            Layout.fillWidth: true
            Layout.preferredWidth: 1
            Layout.fillHeight: true
            KeyNavigation.left: tileState
            KeyNavigation.right: tileLink
            KeyNavigation.down: tileEvents
            Label {
                objectName: "powerValue"
                text: st.powerAvailable ? qsTr("%1 %").arg(st.powerPercent.toFixed(0)) : qsTr("No data")
                font.pixelSize: page.big
                font.bold: true
            }
            Label { Layout.fillWidth: true; wrapMode: Text.WordWrap;
                objectName: "powerSource"
                text: st.powerAvailable ? qsTr("Source: %1").arg(st.powerSource) : qsTr("Power telemetry unavailable")
                color: win.p.muted
            }
        }

        Tile {
            id: tileLink
            objectName: "tileLink"
            title: qsTr("Test link to remote unit")
            accent: st.peerState === "connected" ? win.p.ok : st.peerState === "lost" ? win.p.warning : win.p.fault
            Layout.fillWidth: true
            Layout.preferredWidth: 1
            Layout.fillHeight: true
            KeyNavigation.left: tilePower
            KeyNavigation.down: tileEvents
            Label {
                objectName: "peerValue"
                text: st.peerState === "connected" ? qsTr("Connected") : st.peerState === "lost" ? qsTr("No link") : qsTr("Incompatible")
                font.pixelSize: page.big
                font.bold: true
            }
            Label { Layout.fillWidth: true; wrapMode: Text.WordWrap; text: qsTr("Remote unit: %1").arg(st.peerDevice); color: win.p.muted }
            Label { Layout.fillWidth: true; wrapMode: Text.WordWrap; text: qsTr("Protocol: remote %1 / console %2").arg(st.peerProtocol).arg(st.localProtocol); color: win.p.muted }
            Label { Layout.fillWidth: true; wrapMode: Text.WordWrap;
                text: st.peerHeartbeatAge >= 0 ? qsTr("Last heartbeat %1 s ago").arg(st.peerHeartbeatAge.toFixed(1)) : qsTr("No heartbeat")
                color: win.p.muted
            }
        }

        Tile {
            id: tileTemp
            objectName: "tileTemp"
            title: qsTr("Temperature and cooling")
            accent: !st.tempAvailable ? win.p.muted : win.stateColor(st.overall === "normal" ? "normal" : "warning")
            Layout.fillWidth: true
            Layout.preferredWidth: 1
            Layout.fillHeight: true
            KeyNavigation.up: tileState
            KeyNavigation.right: tileEvents
            Label {
                objectName: "tempValue"
                text: st.tempAvailable ? qsTr("SoC %1 °C").arg(st.tempSoc.toFixed(1)) : qsTr("No data")
                font.pixelSize: page.big
                font.bold: true
            }
            Label { Layout.fillWidth: true; wrapMode: Text.WordWrap; visible: st.tempAvailable; text: qsTr("Board %1 °C").arg(st.tempBoard.toFixed(1)); color: win.p.muted }
            Label { Layout.fillWidth: true; wrapMode: Text.WordWrap;
                objectName: "fanValue"
                text: st.fanRpm >= 0 ? qsTr("Fan %1 % · %2 rpm").arg(st.fanDuty).arg(st.fanRpm) : qsTr("Fan %1 % · no speed signal").arg(st.fanDuty)
                color: win.p.muted
            }
            ProgressBar { objectName: "fanBar"; from: 0; to: 100; value: st.fanDuty; Layout.fillWidth: true }
        }

        Tile {
            id: tileEvents
            objectName: "tileEvents"
            title: qsTr("Active events")
            Layout.columnSpan: 2
            Layout.fillWidth: true
            Layout.preferredWidth: 1
            Layout.fillHeight: true
            KeyNavigation.left: tileTemp
            KeyNavigation.up: tilePower
            Label { Layout.fillWidth: true; wrapMode: Text.WordWrap;
                objectName: "noActiveLabel"
                visible: backend.activeEvents.length === 0
                text: qsTr("No active events")
                color: win.p.ok
                font.pixelSize: 24 * win.s
            }
            Repeater {
                model: backend.activeEvents
                delegate: RowLayout {
                    required property var modelData
                    spacing: 12 * win.s
                    Layout.fillWidth: true
                    Rectangle {
                        width: 14 * win.s; height: width; radius: width / 2
                        color: modelData.severity === "error" ? win.p.fault : modelData.severity === "warning" ? win.p.warning : win.p.info
                    }
                    Label { Layout.fillWidth: true; wrapMode: Text.WordWrap; text: modelData.code; font.bold: true }
                    Label { text: modelData.text; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                }
            }
        }
    }
}
