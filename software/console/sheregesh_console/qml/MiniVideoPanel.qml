// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Раздел «Видео»: источники блока (выбор с подтверждением), состояние, режим и FPS.
ColumnLayout {
    id: panel
    spacing: 8 * win.k
    readonly property var vs: backend.videoSource
    readonly property var rx: backend.videoReceiver
    readonly property var sources: backend.videoSources
    readonly property var sel: backend.videoSelection
    readonly property bool switching: sel.state === "switching"

    Label { text: qsTr("Video source"); color: win.p.muted; font.pixelSize: 11 * win.k; font.bold: true }
    Repeater {
        model: panel.sources
        delegate: AbstractButton {
            id: chip
            required property var modelData
            required property int index
            objectName: "miniSource" + index
            readonly property bool selectable: modelData.available && !modelData.active && panel.sel.blocker === ""
                                               && !panel.switching
            enabled: selectable
            Layout.fillWidth: true
            Layout.preferredHeight: 44 * win.k
            onClicked: {                     // REQ-SYS-009: переключение — только в отдельном диалоге подтверждения
                sourceDialog.sourceId = modelData.id
                sourceDialog.sourceName = modelData.name
                sourceDialog.sourceInput = win.videoInputText(modelData.input)
                sourceDialog.open()
            }
            background: Rectangle {
                radius: 5 * win.k
                color: chip.modelData.active ? Qt.darker(win.p.ok, 2.4) : win.p.bg
                border.color: chip.modelData.active ? win.p.ok : sourceDialog.opened && sourceDialog.sourceId === chip.modelData.id ? win.p.accent : win.p.border
                opacity: chip.modelData.available ? 1 : 0.5
            }
            contentItem: RowLayout {
                spacing: 8 * win.k
                Label {
                    text: chip.modelData.name
                    font.pixelSize: 14 * win.k
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                    leftPadding: 10 * win.k
                }
                Label {
                    text: win.videoInputText(chip.modelData.input) + " · "
                          + (chip.modelData.active ? qsTr("active") : panel.switching && panel.sel.id === chip.modelData.id
                             ? qsTr("switching…") : chip.modelData.available ? qsTr("available") : qsTr("unavailable"))
                    color: chip.modelData.active ? win.p.ok : win.p.muted
                    font.pixelSize: 12 * win.k
                    rightPadding: 10 * win.k
                }
            }
        }
    }
    Dialog {
        id: sourceDialog
        objectName: "miniSourceDialog"
        property string sourceId
        property string sourceName
        property string sourceInput
        parent: Overlay.overlay
        anchors.centerIn: Overlay.overlay
        modal: true
        width: Math.min(640 * win.k, win.width - 32 * win.k)
        height: 260 * win.k                     // явная высота: иначе цикл привязок implicitHeight в Fusion Dialog
        standardButtons: Dialog.Ok | Dialog.Cancel
        title: qsTr("Switch video source?")
        onOpened: standardButton(Dialog.Ok).forceActiveFocus()   // без сенсора: Enter — да, Esc — нет
        Shortcut {
            sequences: ["Return", "Enter"]
            enabled: sourceDialog.opened
            onActivated: sourceDialog.accept()
        }
        Label {
            objectName: "miniSourceQuestion"
            text: qsTr("The remote unit will switch to «%1» (%2). The current picture is interrupted until the new source starts.")
                  .arg(sourceDialog.sourceName).arg(sourceDialog.sourceInput)
            wrapMode: Text.WordWrap
            width: sourceDialog.availableWidth
        }
        onAccepted: backend.selectVideoSource(sourceId, true)
    }
    Label {
        visible: panel.sources.length === 0
        text: qsTr("The remote unit does not report a list of sources.")
        color: win.p.muted
        font.pixelSize: 12 * win.k
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
    }
    Label {
        visible: panel.sel.state === "error"
        text: qsTr("Switching to «%1» failed.").arg(panel.sel.name) + (panel.sel.detail !== "" ? " " + panel.sel.detail : "")
        color: win.p.fault
        font.pixelSize: 12 * win.k
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
    }
    Item { Layout.preferredHeight: 4 * win.k }
    MiniRow {
        objectName: "miniVideoState"
        label: qsTr("State")
        value: panel.vs.live ? qsTr("Playing") : messages.title()
        valueColor: panel.vs.live ? win.p.ok : win.p.warning
    }
    MiniRow {
        label: qsTr("Source mode")
        value: panel.vs.width > 0 ? "%1×%2 · %3 fps".arg(panel.vs.width).arg(panel.vs.height).arg(win.num(panel.vs.fps, 0)) : "—"
    }
    MiniRow {
        label: qsTr("Shown")
        value: qsTr("%1 fps · %2 · decoder restarts %3").arg(win.num(panel.rx.fps, 0))
               .arg(panel.rx.codec !== "" ? panel.rx.codec.toUpperCase() : "—").arg(panel.rx.restarts)
    }
    MiniRow {
        visible: panel.vs.input === "synthetic"
        label: qsTr("Signal")
        value: qsTr("SIM · synthetic test pattern")
        valueColor: win.p.warning
    }
    MiniRow {
        visible: panel.vs.error !== ""
        label: qsTr("Error")
        value: panel.vs.error
        valueColor: win.p.fault
    }
    MiniVideoText { id: messages }
    MiniDetails { pages: [{page: "VideoPage", label: qsTr("Video page")}] }
}
