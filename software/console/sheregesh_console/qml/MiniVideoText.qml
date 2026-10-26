// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick

// Короткие тексты состояния видео для Minimal (как VideoPage.stateText/reasonText, без технических подробностей).
QtObject {
    readonly property var vs: backend.videoSource
    readonly property var sel: backend.videoSelection

    function title() {
        switch (vs.display) {
        case "no-signal": return qsTr("No signal")
        case "no-device": return qsTr("No video device")
        case "connect-error": return qsTr("Connection error")
        case "error": return qsTr("Video source error")
        case "starting": return qsTr("Source starting")
        case "connecting": return qsTr("Connecting…")
        case "switching": return qsTr("Switching…")
        case "unsupported": return qsTr("Video mode not supported")
        case "unavailable": return qsTr("Video unavailable")
        case "waiting": return qsTr("Waiting for stream")
        default: return qsTr("No video")
        }
    }
    function reason() {
        var input = win.videoInputText(vs.input)
        switch (vs.display) {
        case "no-signal": return qsTr("No signal on the %1 input. The last frame is not shown; video returns automatically when the signal is back.").arg(input)
        case "no-device": return qsTr("No capture device for the %1 input.").arg(input)
        case "connect-error": return qsTr("Cannot connect to the %1 stream; retrying automatically.").arg(input)
        case "error": return qsTr("The %1 video source reports an error.").arg(input)
        case "starting": return qsTr("The %1 video source is starting.").arg(input)
        case "connecting": return qsTr("Connecting to the %1 source: waiting for its first frames. Frames of the previous source are not shown.").arg(input)
        case "switching": return qsTr("Switching to «%1»…").arg(sel.name)
        case "reconnecting": return qsTr("Reconnecting to the remote unit: the link is being restored. The last frame is not shown; video returns automatically.")
        case "unavailable": return qsTr("GStreamer is not installed; the rest of the interface works normally.")
        case "off": return qsTr("The remote unit does not report a video stream.")
        default: return qsTr("The interface keeps working; the stream is restored automatically when frames arrive.")
        }
    }
}
