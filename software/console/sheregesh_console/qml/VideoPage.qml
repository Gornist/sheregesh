// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Sheregesh 1.0

Item {
    id: page
    readonly property var rx: backend.videoReceiver
    readonly property var vs: backend.videoSource          // источник: вход, состояние, режим, причина, display/live
    readonly property bool playing: vs.live                 // кадр актуален: приёмник показывает, источник в порядке
    readonly property var src: rx.source
    readonly property bool localInput: src.kind !== undefined
    readonly property string inputText: win.videoInputText(vs.input)

    function modeText() {
        return vs.width > 0 && vs.height > 0
            ? qsTr("%1×%2 at %3\u00a0fps").arg(vs.width).arg(vs.height).arg(vs.fps > 0 ? win.num(vs.fps, 0) : "—") : "—"
    }
    function sourceText() {
        if (vs.origin === "none")
            return qsTr("Source: none")
        var what = page.inputText + (vs.device !== "" ? " · " + vs.device : "")
                 + (vs.legacy ? qsTr(" (input type not reported by the unit)") : "")
        return qsTr("Source: %1 · %2 · state: %3").arg(vs.origin === "local" ? qsTr("console input") : qsTr("remote unit"))
               .arg(what).arg(win.videoSourceStateText(vs.state))
    }
    function reasonText() {
        var err = vs.error !== "" ? vs.error : page.localInput ? rx.error : ""
        var main
        switch (vs.display) {
        case "no-signal": main = qsTr("No signal on the %1 input. The last frame is not shown; video returns automatically when the signal is back.").arg(page.inputText); break
        case "no-device": main = qsTr("No capture device for the %1 input.").arg(page.inputText); break
        case "connect-error": main = qsTr("Cannot connect to the %1 stream; retrying automatically.").arg(page.inputText); break
        case "error": main = qsTr("The %1 video source reports an error.").arg(page.inputText); break
        case "starting": main = qsTr("The %1 video source is starting.").arg(page.inputText); break
        case "unsupported": main = qsTr("The selected video mode is not supported by the device."); break
        case "unavailable": return qsTr("GStreamer is not installed; the rest of the interface works normally.")
        case "off":
            if (backend.status.videoSource === "disabled")
                return qsTr("The video source is turned off in the settings.")
            main = qsTr("The remote unit does not report a video stream."); err = ""; break
        default:
            if (page.localInput && rx.error !== "")
                return rx.error
            return qsTr("The interface keeps working; the stream is restored automatically when frames arrive.")
        }
        return err !== "" ? main + "\n" + err : main
    }

    function stateText(s) {
        switch (s) {
        case "playing": return qsTr("Playing")
        case "waiting": return qsTr("Waiting for stream")
        case "no-video": return qsTr("No video")
        case "restarting": return qsTr("Restarting decoder")
        case "unavailable": return qsTr("Video unavailable")
        case "no-device": return qsTr("No video device")
        case "unsupported": return qsTr("Video mode not supported")
        case "no-signal": return qsTr("No signal")
        case "starting": return qsTr("Source starting")
        case "connect-error": return qsTr("Connection error")
        case "error": return qsTr("Video source error")
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
                color: page.playing ? win.p.ok : vs.display === "waiting" || vs.display === "starting" ? win.p.info
                     : vs.display === "connect-error" || vs.display === "error" ? win.p.fault : win.p.warning
                implicitWidth: videoState.implicitWidth + 28 * win.s
                implicitHeight: videoState.implicitHeight + 10 * win.s
                Label { id: videoState; objectName: "videoStateText"; anchors.centerIn: parent; text: page.stateText(vs.display); color: "#ffffff"; font.bold: true }
            }
            Label {
                objectName: "videoSourceText"
                text: page.sourceText()
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
        }
        Label {
            objectName: "videoInfo"
            text: page.localInput
                  ? qsTr("%1 · %2 %3 at %4\u00a0fps · shown %5\u00a0fps · frames %6 · capture restarts %7")
                    .arg(page.src.name || "UVC").arg((page.src.format || "—").toUpperCase())
                    .arg(page.src.resolution ? page.src.resolution.replace("x", "×") : "—").arg(page.src.fps || "—")
                    .arg(win.num(rx.fps, 0)).arg(rx.frames).arg(rx.restarts)
                  : vs.origin === "remote"
                  ? qsTr("Source mode %1 · shown %2\u00a0fps · %3 · frames %4 · decoder restarts %5")
                    .arg(page.modeText()).arg(win.num(rx.fps, 0)).arg(rx.codec !== "" ? rx.codec.toUpperCase() : "—")
                    .arg(rx.frames).arg(rx.restarts)
                  : qsTr("Synthetic test pattern · %1 · port %2 · %3\u00a0fps · frames %4 · decoder restarts %5")
                    .arg(rx.codec !== "" ? rx.codec.toUpperCase() : "—").arg(rx.port > 0 ? rx.port : "—")
                    .arg(win.num(rx.fps, 0)).arg(rx.frames).arg(rx.restarts)
            color: win.p.muted
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
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
                live: page.vs.live                  // источник без сигнала — чёрное поле, прежний кадр не показывается
            }
            ColumnLayout {
                anchors.centerIn: parent
                visible: !page.playing
                spacing: 8 * win.s
                Label {
                    objectName: "videoPlaceholder"
                    text: page.stateText(vs.display)
                    color: "#e8eaed"
                    font.pixelSize: 34 * win.s
                    font.bold: true
                    Layout.alignment: Qt.AlignHCenter
                }
                Label {
                    objectName: "videoReason"
                    text: page.reasonText()
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
