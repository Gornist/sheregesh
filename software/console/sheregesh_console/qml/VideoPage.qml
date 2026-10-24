// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Sheregesh 1.0

Item {
    id: page
    readonly property var rx: backend.videoReceiver
    readonly property bool playing: rx.state === "playing"
    readonly property var src: rx.source
    readonly property bool localInput: src.kind !== undefined

    function stateText(s) {
        switch (s) {
        case "playing": return qsTr("Playing")
        case "waiting": return qsTr("Waiting for stream")
        case "no-video": return qsTr("No video")
        case "restarting": return qsTr("Restarting decoder")
        case "unavailable": return qsTr("Video unavailable")
        case "no-device": return qsTr("No video device")
        case "unsupported": return qsTr("Video mode not supported")
        default: return qsTr("No video source")
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 12 * win.s

        RowLayout {
            spacing: 16 * win.s
            Layout.fillWidth: true
            Rectangle {
                objectName: "videoStateBadge"
                radius: 6 * win.s
                color: page.playing ? win.p.ok : rx.state === "waiting" ? win.p.info : win.p.warning
                implicitWidth: videoState.implicitWidth + 28 * win.s
                implicitHeight: videoState.implicitHeight + 10 * win.s
                Label { id: videoState; objectName: "videoStateText"; anchors.centerIn: parent; text: page.stateText(rx.state); color: "#ffffff"; font.bold: true }
            }
            Label {
                objectName: "videoInfo"
                text: page.localInput
                      ? qsTr("%1 · %2 %3 at %4\u00a0fps · shown %5\u00a0fps · frames %6 · capture restarts %7")
                        .arg(page.src.name || "UVC").arg((page.src.format || "—").toUpperCase())
                        .arg(page.src.resolution ? page.src.resolution.replace("x", "×") : "—").arg(page.src.fps || "—")
                        .arg(win.num(rx.fps, 0)).arg(rx.frames).arg(rx.restarts)
                      : qsTr("Synthetic test pattern · %1 · port %2 · %3\u00a0fps · frames %4 · decoder restarts %5")
                        .arg(rx.codec !== "" ? rx.codec.toUpperCase() : "—").arg(rx.port > 0 ? rx.port : "—")
                        .arg(win.num(rx.fps, 0)).arg(rx.frames).arg(rx.restarts)
                color: win.p.muted
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
        }

        Rectangle {
            id: frame
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: "#000000"
            radius: 8 * win.s
            border.color: win.p.border
            clip: true
            activeFocusOnTab: true
            VideoView {
                id: view
                objectName: "videoView"
                anchors.fill: parent
                anchors.margins: 2
                receiver: page.rx
            }
            ColumnLayout {
                anchors.centerIn: parent
                visible: !page.playing
                spacing: 8 * win.s
                Label {
                    objectName: "videoPlaceholder"
                    text: page.stateText(rx.state)
                    color: "#e8eaed"
                    font.pixelSize: 34 * win.s
                    font.bold: true
                    Layout.alignment: Qt.AlignHCenter
                }
                Label {
                    objectName: "videoReason"
                    text: (rx.state === "no-device" || rx.state === "unsupported" || (page.localInput && rx.error !== "")) ? rx.error
                        : rx.state === "unavailable" ? qsTr("GStreamer is not installed; the rest of the interface works normally.")
                        : rx.state === "off" && backend.status.videoSource === "disabled" ? qsTr("The video source is turned off in the settings.")
                        : rx.state === "off" ? qsTr("The remote unit does not report a video stream.")
                        : qsTr("The interface keeps working; the stream is restored automatically when frames arrive.")
                    color: "#9aa0a6"
                    wrapMode: Text.WordWrap
                    horizontalAlignment: Text.AlignHCenter
                    Layout.maximumWidth: frame.width * 0.8
                    Layout.alignment: Qt.AlignHCenter
                }
            }
        }
        Label {
            text: qsTr("Laboratory bench: synthetic test image or a standard UVC lab device only. No connection to real receivers or vehicles.")
            color: win.p.muted
            font.pixelSize: 16 * win.s
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }
    }
}
