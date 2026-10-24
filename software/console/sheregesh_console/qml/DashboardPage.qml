// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: page
    readonly property var st: win.st
    readonly property real big: 34 * win.s

    function videoState(s) {         // краткая причина для плитки связи (полный текст — на экране «Видео»)
        return s === "no-video" ? qsTr("stream interrupted") : s === "restarting" ? qsTr("decoder restarting")
             : s === "unavailable" ? qsTr("GStreamer missing") : s
    }

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
                text: st.powerAvailable ? qsTr("%1\u00a0%").arg(win.num(st.powerPercent, 0)) : qsTr("No data")
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
            // peerLink: CONNECTED | RECONNECTING | DISCONNECTED | AUTH_FAILED | INCOMPATIBLE (S3.1)
            accent: st.peerLink === "CONNECTED" ? win.p.ok
                  : st.peerLink === "AUTH_FAILED" || st.peerLink === "INCOMPATIBLE" ? win.p.fault
                  : st.peerState === "connecting" ? win.p.info : win.p.warning
            Layout.fillWidth: true
            Layout.preferredWidth: 1
            Layout.fillHeight: true
            KeyNavigation.left: tilePower
            KeyNavigation.down: tileEvents
            Label {
                objectName: "peerValue"
                text: st.peerLink === "CONNECTED" ? qsTr("Connected") : st.peerLink === "RECONNECTING" ? qsTr("Reconnecting…")
                    : st.peerLink === "AUTH_FAILED" ? qsTr("Authentication failed") : st.peerLink === "INCOMPATIBLE" ? qsTr("Incompatible")
                    : st.peerState === "connecting" ? qsTr("Connecting…") : qsTr("No link")
                font.pixelSize: page.big
                font.bold: true
            }
            Label {
                objectName: "peerDetail"
                Layout.fillWidth: true; wrapMode: Text.WordWrap
                visible: st.peerState !== "connected"
                text: st.peerDetail === "refused" ? qsTr("Remote unit process is not running (connection refused)")
                    : st.peerDetail === "heartbeat" ? qsTr("Remote unit stopped sending heartbeat")
                    : st.peerDetail === "closed" ? qsTr("Connection closed by the remote unit")
                    : st.peerDetail === "bad-messages" ? qsTr("Too many corrupted messages; reconnecting")
                    : st.peerDetail === "timeout" || st.peerDetail === "handshake-timeout" ? qsTr("No response from the remote unit")
                    : st.peerState === "incompatible" ? qsTr("Version check failed: %1").arg(st.peerDetail)
                    : st.peerDetail.indexOf("auth") === 0 ? qsTr("Certificate or device identity check failed: %1").arg(st.peerDetail.substring(5).trim())
                    : qsTr("Establishing connection")
                color: st.peerState === "incompatible" || st.peerLink === "AUTH_FAILED" ? win.p.fault : win.p.warning
            }
            Label {
                objectName: "peerIdentity"
                Layout.fillWidth: true; wrapMode: Text.WordWrap
                text: qsTr("Remote unit: %1").arg(st.peerDevice !== "" ? st.peerDevice : "—")
                      + (st.peerRole !== "" ? qsTr(" · role %1").arg(st.peerRole) : "")
                      + (st.peerSoftware !== "" ? qsTr(" · software %1").arg(st.peerSoftware) : "")
                color: win.p.muted
            }
            Label {
                objectName: "remoteTelemetry"
                Layout.fillWidth: true; wrapMode: Text.WordWrap
                visible: st.remoteAvailable && st.peerState === "connected"
                text: qsTr("SoC %1\u00a0°C · power %2\u00a0% · uptime %3\u00a0s").arg(win.num(st.remoteTempSoc, 1)).arg(win.num(st.remotePowerPercent, 0)).arg(win.num(st.remoteUptime, 0))
                color: win.p.muted
            }
            Label { Layout.fillWidth: true; wrapMode: Text.WordWrap; text: qsTr("Protocol: remote %1 / console %2").arg(st.peerProtocol).arg(st.localProtocol); color: win.p.muted }
            Label {
                objectName: "linkSecurity"
                Layout.fillWidth: true; wrapMode: Text.WordWrap
                visible: st.linkSecure === true
                text: qsTr("Mutual TLS %1 · %2 · round trip %3 ms").arg(st.linkTls !== "" ? st.linkTls : "—")
                      .arg(st.linkHost !== "" ? st.linkHost : "—").arg(st.linkRttMs >= 0 ? win.num(st.linkRttMs, 1) : "—")
                color: win.p.muted
            }
            Label {
                objectName: "linkPlain"
                Layout.fillWidth: true; wrapMode: Text.WordWrap
                visible: st.linkSecure !== true && st.peerMode !== "emulated"
                text: qsTr("No TLS (local test channel) · round trip %1 ms").arg(st.linkRttMs >= 0 ? win.num(st.linkRttMs, 1) : "—")
                color: win.p.muted
            }
            Label {
                objectName: "linkAuthFailures"
                Layout.fillWidth: true; wrapMode: Text.WordWrap
                visible: st.linkAuthFailures > 0
                text: qsTr("Authentication failures: %1").arg(st.linkAuthFailures)
                color: win.p.fault
            }
            Label {
                objectName: "linkVideo"
                Layout.fillWidth: true; wrapMode: Text.WordWrap
                readonly property var rx: backend.videoReceiver
                text: rx.state === "playing" ? qsTr("Video: playing · %1 fps").arg(win.num(rx.fps, 0))
                    : rx.state === "waiting" ? qsTr("Video: waiting for stream")
                    : rx.state === "off" ? qsTr("Video: no stream")
                    : qsTr("Video: no frames (%1)").arg(page.videoState(rx.state))
                color: rx.state === "playing" || rx.state === "off" ? win.p.muted : win.p.warning
            }
            Label { Layout.fillWidth: true; wrapMode: Text.WordWrap;
                text: st.peerHeartbeatAge >= 0 ? qsTr("Last heartbeat %1\u00a0s ago").arg(win.num(st.peerHeartbeatAge, 1)) : qsTr("No heartbeat")
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
                text: st.tempAvailable ? qsTr("SoC %1\u00a0°C").arg(win.num(st.tempSoc, 1)) : qsTr("No data")
                font.pixelSize: page.big
                font.bold: true
            }
            Label { Layout.fillWidth: true; wrapMode: Text.WordWrap; visible: st.tempAvailable; text: qsTr("Board %1\u00a0°C").arg(win.num(st.tempBoard, 1)); color: win.p.muted }
            Label { Layout.fillWidth: true; wrapMode: Text.WordWrap;
                objectName: "fanValue"
                text: st.fanRpm >= 0 ? qsTr("Fan %1\u00a0% · %2 rpm").arg(st.fanDuty).arg(st.fanRpm) : qsTr("Fan %1\u00a0% · no speed signal").arg(st.fanDuty)
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
                    Label { objectName: "activeCode"; text: modelData.code; font.bold: true; Layout.preferredWidth: 170 * win.s; Layout.alignment: Qt.AlignTop }
                    Label { text: modelData.text; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                }
            }
        }
    }
}
