// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: page
    readonly property var st: win.st
    readonly property real big: 34 * win.s

    function levelColor(pct, low) {
        return low || pct <= st.batteryCritical ? win.p.fault : pct <= st.batteryWarning ? win.p.warning : win.p.ok
    }

    GridLayout {
        anchors.fill: parent
        columns: 2
        rowSpacing: 16 * win.s
        columnSpacing: 16 * win.s

        Tile {
            id: consoleTile
            objectName: "tileConsolePower"
            title: qsTr("Console power (synthetic)")
            accent: st.powerAvailable ? page.levelColor(st.powerPercent, st.lowBattery) : win.p.muted
            Layout.fillWidth: true
            Layout.preferredWidth: 1
            Layout.fillHeight: true
            focus: true
            KeyNavigation.right: remoteTile
            Label {
                objectName: "consolePowerValue"
                text: st.powerAvailable ? qsTr("%1\u00a0%").arg(st.powerPercent.toFixed(0)) : qsTr("No data")
                font.pixelSize: page.big
                font.bold: true
            }
            ProgressBar { visible: st.powerAvailable; from: 0; to: 100; value: st.powerPercent; Layout.fillWidth: true }
            Label { Layout.fillWidth: true; wrapMode: Text.WordWrap; visible: st.powerAvailable; text: qsTr("Source: %1").arg(st.powerSource) }
            Label { Layout.fillWidth: true; wrapMode: Text.WordWrap; visible: st.powerAvailable; text: st.lowBattery ? qsTr("Low battery signal: yes") : qsTr("Low battery signal: no"); color: st.lowBattery ? win.p.fault : win.p.muted }
            Label { Layout.fillWidth: true; wrapMode: Text.WordWrap; visible: st.powerAvailable; text: st.undervoltage ? qsTr("Undervoltage: yes") : qsTr("Undervoltage: no"); color: st.undervoltage ? win.p.fault : win.p.muted }
            Label { Layout.fillWidth: true; wrapMode: Text.WordWrap; visible: !st.powerAvailable; text: qsTr("Power telemetry unavailable: values are not shown."); color: win.p.muted }
            Label { Layout.fillWidth: true; wrapMode: Text.WordWrap; text: qsTr("Thresholds: warning %1\u00a0%, critical %2\u00a0%").arg(st.batteryWarning).arg(st.batteryCritical); color: win.p.muted }
        }

        Tile {
            id: remoteTile
            objectName: "tileRemotePower"
            title: qsTr("Remote unit power (synthetic, via link)")
            accent: st.remotePowerPercent >= 0 ? page.levelColor(st.remotePowerPercent, st.remoteLowBattery) : win.p.muted
            Layout.fillWidth: true
            Layout.preferredWidth: 1
            Layout.fillHeight: true
            KeyNavigation.left: consoleTile
            Label {
                objectName: "remotePowerValue"
                text: st.remotePowerPercent >= 0 ? qsTr("%1\u00a0%").arg(st.remotePowerPercent.toFixed(0)) : qsTr("No data")
                font.pixelSize: page.big
                font.bold: true
            }
            ProgressBar { visible: st.remotePowerPercent >= 0; from: 0; to: 100; value: st.remotePowerPercent; Layout.fillWidth: true }
            Label { Layout.fillWidth: true; wrapMode: Text.WordWrap; visible: st.remotePowerPercent >= 0; text: qsTr("Source: %1").arg(st.remotePowerSource) }
            Label {
                Layout.fillWidth: true; wrapMode: Text.WordWrap
                text: st.peerMode === "emulated" ? qsTr("The remote unit is emulated inside the console process: no power telemetry.")
                    : st.remotePowerPercent < 0 ? qsTr("No data from the remote unit.") : qsTr("External battery pack outside the IP54 boundary (D3).")
                color: win.p.muted
            }
        }

        Tile {
            objectName: "tilePowerHistory"
            title: qsTr("Console charge history (samples: %1)").arg(backend.powerHistory.length)
            Layout.columnSpan: 2
            Layout.fillWidth: true
            Layout.fillHeight: true
            Canvas {
                id: chart
                objectName: "powerChart"
                Layout.fillWidth: true
                Layout.preferredHeight: 220 * win.s
                property var values: backend.powerHistory
                onValuesChanged: requestPaint()
                onWidthChanged: requestPaint()
                onPaint: {
                    var ctx = getContext("2d")
                    ctx.reset()
                    ctx.fillStyle = win.p.panel
                    ctx.fillRect(0, 0, width, height)
                    var pad = 8 * win.s, left = 48 * win.s
                    function y(v) { return pad + (100 - v) / 100 * (height - 2 * pad) }
                    ctx.fillStyle = win.p.muted
                    ctx.font = Math.round(14 * win.s) + "px sans-serif"
                    ctx.textBaseline = "middle"
                    ;[0, 50, 100].forEach(function (v) { ctx.fillText(v + " %", 0, y(v)) })
                    ctx.strokeStyle = win.p.border
                    ctx.beginPath(); ctx.moveTo(left, y(0)); ctx.lineTo(width, y(0)); ctx.moveTo(left, y(100)); ctx.lineTo(width, y(100)); ctx.stroke()
                    ctx.lineWidth = 1
                    ctx.strokeStyle = win.p.warning
                    ctx.beginPath(); ctx.moveTo(left, y(st.batteryWarning)); ctx.lineTo(width, y(st.batteryWarning)); ctx.stroke()
                    ctx.strokeStyle = win.p.fault
                    ctx.beginPath(); ctx.moveTo(left, y(st.batteryCritical)); ctx.lineTo(width, y(st.batteryCritical)); ctx.stroke()
                    if (values.length < 2) return
                    ctx.strokeStyle = win.p.accent
                    ctx.lineWidth = 3
                    ctx.beginPath()
                    for (var i = 0; i < values.length; i++) {
                        var x = left + i / (values.length - 1) * (width - left)
                        if (i === 0) ctx.moveTo(x, y(values[i])); else ctx.lineTo(x, y(values[i]))
                    }
                    ctx.stroke()
                }
            }
            Label { text: qsTr("Display only: the console does not control real power hardware."); color: win.p.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap }
        }
    }
}
