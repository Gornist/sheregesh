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
    // Источники блока и их выбор (backend.videoSources / videoSelection / selectVideoSource, S4)
    readonly property var sources: backend.videoSources
    readonly property var sel: backend.videoSelection
    readonly property bool switching: sel.state === "switching"

    function modeText() {
        return vs.width > 0 && vs.height > 0
            ? qsTr("%1×%2 at %3\u00a0fps").arg(vs.width).arg(vs.height).arg(vs.fps > 0 ? win.num(vs.fps, 0) : "—") : "—"
    }
    function sourceText() {
        if (vs.origin === "none")
            return qsTr("Source: none")
        if (vs.state === "reconnecting")
            return qsTr("Source: remote unit · state: %1").arg(win.videoSourceStateText(vs.state))
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
        case "connecting": return qsTr("Connecting to the %1 source: waiting for its first frames. Frames of the previous source are not shown.").arg(page.inputText)
        case "switching": return qsTr("Switching to «%1»…").arg(sel.name)
        case "reconnecting": return qsTr("Reconnecting to the remote unit: the link is being restored. The last frame is not shown; video returns automatically.")
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
        case "connecting": return qsTr("Connecting…")
        case "connect-error": return qsTr("Connection error")
        case "error": return qsTr("Video source error")
        case "reconnecting": return qsTr("No video")      // REQ-SW-002: «нет видео»; переподключение — в причине
        case "switching": return qsTr("Switching…")
        default: return qsTr("No video source")
        }
    }
    function selectErrorText() {
        var n = sel.name, t
        switch (sel.error) {
        case "unknown": t = qsTr("The remote unit does not know the source «%1»; the list of sources may have changed.").arg(n); break
        case "unavailable": t = qsTr("Source «%1» is unavailable on the remote unit; choose another source.").arg(n); break
        case "unsupported": t = qsTr("Source selection is not supported by the link to the remote unit."); break
        case "no-link": t = qsTr("No link to the remote unit; switching to «%1» is not confirmed.").arg(n); break
        case "busy": t = qsTr("The remote unit is busy switching sources; try again in a few seconds."); break
        case "timeout": t = qsTr("The remote unit did not confirm switching to «%1».").arg(n); break
        default: t = qsTr("Switching to «%1» failed.").arg(n)
        }
        return sel.detail !== "" ? t + " " + sel.detail : t
    }
    function selectStatusText() {
        if (page.switching)
            return qsTr("Switching to «%1»…").arg(sel.name)
        if (sel.state === "error")
            return page.selectErrorText()
        if (sel.blocker === "no-list" && vs.origin === "remote")
            return qsTr("The remote unit does not report a list of sources: only the synthetic source, selection is not available.")
        if (sel.blocker === "unsupported" && page.sources.length > 0)
            return qsTr("Source selection is not available: the link client of this console does not support it.")
        return ""
    }
    function stateLabel() {            // во время переключения — «Переключение…», пока блок не подтвердит новый источник
        return page.switching && !page.playing ? page.stateText("switching") : page.stateText(vs.display)
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
                color: page.playing ? win.p.ok : page.switching || vs.display === "waiting" || vs.display === "starting"
                       || vs.display === "connecting" || vs.display === "reconnecting" ? win.p.info
                     : vs.display === "connect-error" || vs.display === "error" ? win.p.fault : win.p.warning
                implicitWidth: videoState.implicitWidth + 28 * win.s
                implicitHeight: videoState.implicitHeight + 10 * win.s
                Label { id: videoState; objectName: "videoStateText"; anchors.centerIn: parent; text: page.stateLabel(); color: "#ffffff"; font.bold: true }
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

        ListView {
            id: sourceList
            objectName: "videoSources"
            visible: page.sources.length > 0
            Layout.fillWidth: true
            Layout.preferredHeight: 74 * win.s
            orientation: ListView.Horizontal
            spacing: 10 * win.s
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            model: page.sources
            ScrollBar.horizontal: ScrollBar { policy: ScrollBar.AsNeeded }
            delegate: AbstractButton {
                id: chip
                required property var modelData
                required property int index
                objectName: "videoSourceItem" + index
                readonly property string sourceId: modelData.id
                readonly property bool target: page.switching && page.sel.id === modelData.id
                // выбор: источник доступен, не активный, клиент умеет выбирать, другое переключение не идёт
                readonly property bool selectable: modelData.available && !modelData.active && page.sel.blocker === ""
                                                   && !page.switching
                enabled: selectable
                focusPolicy: Qt.StrongFocus
                // делят ширину строки; не уже 260 (16 источников на 1280×800 — прокрутка) и не шире 420
                width: Math.min(420 * win.s, Math.max(260 * win.s, (sourceList.width - sourceList.spacing
                       * (sourceList.count - 1)) / Math.max(sourceList.count, 1)))
                height: sourceList.height
                Keys.onReturnPressed: if (selectable) clicked()
                Keys.onEnterPressed: if (selectable) clicked()
                onClicked: {
                    selectDialog.sourceId = modelData.id
                    selectDialog.sourceName = modelData.name
                    selectDialog.sourceInput = win.videoInputText(modelData.input)
                    selectDialog.open()
                }
                background: Rectangle {
                    radius: 6 * win.s
                    color: win.p.panel
                    opacity: chip.modelData.available ? 1.0 : 0.55
                    border.width: chip.modelData.active || chip.target || chip.activeFocus ? 3 : 1
                    border.color: chip.activeFocus ? win.p.accent : chip.modelData.active ? win.p.ok
                                : chip.target ? win.p.info : win.p.border
                }
                contentItem: ColumnLayout {
                    spacing: 2 * win.s
                    opacity: chip.modelData.available ? 1.0 : 0.55
                    Label {
                        objectName: "videoSourceName"
                        text: chip.modelData.name
                        font.bold: true
                        elide: Text.ElideRight
                        maximumLineCount: 1
                        Layout.fillWidth: true
                    }
                    Label {
                        objectName: "videoSourceState"
                        text: win.videoInputText(chip.modelData.input) + " · "
                              + (chip.modelData.active ? qsTr("active") : chip.target ? qsTr("switching…")
                                 : chip.modelData.available ? qsTr("available") : qsTr("unavailable"))
                        color: chip.modelData.active ? win.p.ok : chip.target ? win.p.info : win.p.muted
                        font.pixelSize: 16 * win.s
                        elide: Text.ElideRight
                        maximumLineCount: 1
                        Layout.fillWidth: true
                    }
                }
                leftPadding: 14 * win.s
                rightPadding: 14 * win.s
                topPadding: 8 * win.s
                bottomPadding: 8 * win.s
            }
        }
        Label {
            objectName: "videoSelectStatus"
            text: page.selectStatusText()
            visible: text !== ""
            color: page.switching ? win.p.info : sel.state === "error" ? win.p.fault : win.p.muted
            wrapMode: Text.WordWrap
            maximumLineCount: 2
            elide: Text.ElideRight
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
                    text: page.stateLabel()
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

    Dialog {
        id: selectDialog
        objectName: "videoSelectDialog"
        property string sourceId
        property string sourceName
        property string sourceInput
        height: 300 * win.s          // явная высота: иначе цикл привязок implicitHeight в Fusion Dialog
        // Без сенсора: фокус на «ОК», Enter подтверждает, Esc отменяет (REQ-P-003)
        onOpened: standardButton(Dialog.Ok).forceActiveFocus()
        Shortcut {
            sequences: ["Return", "Enter"]
            enabled: selectDialog.opened
            onActivated: selectDialog.accept()
        }
        anchors.centerIn: Overlay.overlay
        modal: true
        title: qsTr("Switch video source?")
        width: Math.min(720 * win.s, page.width)
        standardButtons: Dialog.Ok | Dialog.Cancel
        Label {
            objectName: "videoSelectQuestion"
            text: qsTr("The remote unit will switch to «%1» (%2). The current picture is interrupted until the new source starts.")
                  .arg(selectDialog.sourceName).arg(selectDialog.sourceInput)
            wrapMode: Text.WordWrap
            width: selectDialog.availableWidth
        }
        onAccepted: backend.selectVideoSource(sourceId, true)
    }
}
