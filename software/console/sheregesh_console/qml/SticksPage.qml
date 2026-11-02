// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Стики и кнопки аппаратуры → удалённый блок (ADR-0013, 4.2): backend.sticks (SticksView). Передача после старта
// пульта выключена; включение — только после подтверждения в диалоге, выключение — сразу. Состояние команд на блоке
// (IDLE / ACTIVE / FAILSAFE) и эхо каналов — из rc-state блока. С S8 (ADR-0014, 3.3) плашка — по фактическому выходу
// на модуль (status.module.output: none / neutral / live, backend.module), третья полоса эха — каналы, полученные
// эмулятором модуля (status.module.crsf.emu), если блок их сообщает.
Item {
    id: page
    readonly property var sv: backend.sticks
    readonly property var mv: backend.module
    readonly property bool emu: mv.support === "ok" && mv.emuChannels.length === 8     // каналы эмулятора модуля
    readonly property bool emuMark: mv.support === "ok" && mv.backendSetting === "crsf-pty-lab"
    readonly property color emuColor: backend.theme === "light" ? "#8250df" : "#bc8cff"   // третья полоса: не как пульт
    readonly property bool linkOk: sv.support === "ok" || sv.support === "no-device"
    readonly property bool hasRemote: sv.remoteState !== ""
    readonly property bool echo: hasRemote && sv.remoteState !== "IDLE"       // в IDLE блок команд не применяет
    readonly property color echoColor: sv.remoteState === "FAILSAFE" ? win.p.fault : win.p.ok   // FAILSAFE: нули

    function stateText() {            // как SticksView.state_text (локальный канал и ui-state)
        switch (sv.support) {
        case "no-link": return qsTr("No link to the remote unit")
        case "old-remote": return qsTr("Not supported by the remote unit")
        }
        switch (sv.remoteState) {
        case "IDLE": return qsTr("No commands")
        case "ACTIVE": return qsTr("Commands are arriving")
        case "FAILSAFE": return qsTr("FAILSAFE: no fresh commands")
        default: return qsTr("No data from the remote unit")
        }
    }
    function stateColor() {
        if (sv.support === "no-link")
            return win.p.warning
        if (sv.support === "old-remote" || !page.hasRemote)
            return win.p.muted
        return sv.remoteState === "ACTIVE" ? win.p.ok : sv.remoteState === "FAILSAFE" ? win.p.fault : win.p.info
    }
    function reasonText() {
        switch (sv.support) {
        case "no-link": return qsTr("Transmission is unavailable until the link is restored.")
        case "old-remote": return qsTr("The remote unit does not accept stick commands (protocol %1).").arg(sv.session !== "" ? sv.session : "—")
        }
        if (!page.hasRemote)
            return qsTr("Waiting for the command state from the remote unit…")
        switch (sv.remoteReason) {
        case "start": return qsTr("No commands since the remote unit started")
        case "fresh": return qsTr("Fresh commands from the console")
        case "disabled": return qsTr("Transmission disabled by the operator")
        case "timeout": return qsTr("No commands for longer than %1\u00a0ms: channels centered, buttons released").arg(sv.timeoutMs)
        case "link-lost": return qsTr("Link session lost: channels centered, buttons released")
        case "device-lost": return qsTr("Control device disconnected: channels centered, buttons released")
        default: return sv.remoteReason
        }
    }
    function refusalText(code) {
        switch (code) {
        case "no-device": return qsTr("no control device")
        case "no-link": return qsTr("no link to the remote unit")
        case "old-remote": return qsTr("not supported by the remote unit")
        default: return code
        }
    }
    function noticeText() {
        if (sv.notice === "device-lost" && !sv.enabled)
            return qsTr("The control device was disconnected: transmission is off. Enable it again after reconnecting.")
        if (sv.result !== "" && sv.result !== "ok" && !sv.enabled)
            return qsTr("Transmission was not enabled: %1").arg(page.refusalText(sv.result))
        return ""
    }
    function outputText() {           // фактический выход блока на модуль (ADR-0014, 2)
        if (mv.support === "no-link")
            return qsTr("Radio output: no data, no link to the remote unit")
        var mark = page.emuMark ? " · " + qsTr("module emulator") : ""
        switch (mv.support === "ok" ? mv.output : "none") {
        case "neutral": return qsTr("CRSF output: neutral") + mark
        case "live": return qsTr("CRSF output: commands go to the module") + mark
        case "unknown": return qsTr("Radio output: unknown value from the remote unit")
        default: return qsTr("No radio output: commands reach the remote unit but are not passed to the module")
        }
    }
    function outputColor() {
        if (mv.support !== "ok")
            return mv.support === "no-link" ? win.p.muted : win.p.warning
        return mv.output === "live" ? win.p.ok : mv.output === "neutral" ? win.p.info : win.p.warning
    }
    function outputNote() {           // для диалога включения
        if (mv.support === "ok" && (mv.output === "neutral" || mv.output === "live"))
            return page.emuMark ? qsTr("The remote unit passes channels 1–8 to the CRSF module emulator.")
                                : qsTr("The remote unit passes channels 1–8 to the module over CRSF.")
        return qsTr("There is no radio output.")
    }
    function value(v) {               // −1000…1000 со знаком «минус», а не дефисом
        return v < 0 ? "\u2212" + (-v) : "" + v
    }

    // Полоса канала −1000…1000: заполнение от центра
    component AxisBar: Rectangle {
        property int level: 0
        property color fill: win.p.accent
        property bool active: true
        implicitHeight: (page.emu ? 8 : 12) * win.s
        radius: 2 * win.s
        color: win.p.bg
        border.color: win.p.border
        Rectangle {
            readonly property real half: (parent.width - 2) / 2
            x: 1 + half + (parent.level < 0 ? half * parent.level / 1000 : 0)
            y: 1
            width: half * Math.abs(parent.level) / 1000
            height: parent.height - 2
            color: parent.fill
            opacity: parent.active ? 1.0 : 0.35
        }
        Rectangle { x: parent.width / 2 - 1; width: 2; height: parent.height; color: win.p.muted }
    }

    TextMetrics { id: numMetrics; font.pixelSize: 17 * win.s; text: "\u22121000" }

    ColumnLayout {
        anchors.fill: parent
        spacing: 10 * win.s

        // Обязательная плашка: фактический выход на модуль — нет / нейтраль / команды (S7 — только «нет»)
        Rectangle {
            objectName: "sticksOutputBanner"
            readonly property bool shown: true
            color: page.outputColor()
            radius: 6 * win.s
            Layout.fillWidth: true
            implicitHeight: bannerText.implicitHeight + 12 * win.s
            Label {
                id: bannerText
                objectName: "sticksOutputText"
                anchors.fill: parent
                anchors.leftMargin: 16 * win.s
                anchors.rightMargin: 16 * win.s
                verticalAlignment: Text.AlignVCenter
                text: page.outputText()
                color: "#ffffff"
                font.bold: true
                elide: Text.ElideRight
            }
        }

        RowLayout {
            spacing: 16 * win.s
            Layout.fillWidth: true
            Rectangle {
                objectName: "sticksRemoteStateBadge"
                radius: 6 * win.s
                color: page.stateColor()
                implicitWidth: stateLabel.implicitWidth + 32 * win.s
                implicitHeight: stateLabel.implicitHeight + 12 * win.s
                Layout.maximumWidth: page.width * 0.45
                Label {
                    id: stateLabel
                    objectName: "sticksRemoteStateText"
                    anchors.centerIn: parent
                    width: Math.min(implicitWidth, parent.width - 32 * win.s)
                    text: page.stateText()
                    color: "#ffffff"
                    font.pixelSize: 26 * win.s
                    font.bold: true
                    elide: Text.ElideRight
                }
            }
            Label {
                objectName: "sticksRemoteReasonText"
                text: page.reasonText()
                color: sv.remoteState === "FAILSAFE" && page.linkOk ? win.p.fault : win.p.text
                wrapMode: Text.WordWrap
                maximumLineCount: 2
                elide: Text.ElideRight
                Layout.fillWidth: true
                Layout.minimumWidth: 0
            }
            Switch {
                id: enableSwitch
                objectName: "sticksEnableSwitch"
                text: qsTr("Transmission")
                checkable: false                    // положение — только из sv.enabled (включение — через диалог)
                checked: sv.enabled
                enabled: sv.enabled || sv.support === "ok"
                opacity: enabled ? 1.0 : 0.45
                font.pixelSize: 20 * win.s
                font.bold: true
                Layout.preferredHeight: 52 * win.s
                onClicked: {
                    if (sv.enabled)
                        sv.setEnabled(false, false)     // выключение — сразу, без подтверждения
                    else
                        confirmDialog.open()
                }
            }
        }

        Label {
            objectName: "sticksNoticeText"
            readonly property bool shown: text !== ""
            visible: shown
            text: page.noticeText()
            color: win.p.fault
            font.bold: true
            elide: Text.ElideRight
            Layout.fillWidth: true
        }

        RowLayout {
            spacing: 16 * win.s
            Layout.fillWidth: true
            Layout.fillHeight: true

            Tile {
                id: channelsTile
                objectName: "sticksChannelsTile"
                title: page.emu ? qsTr("Channels: console → remote unit → module") : qsTr("Channels: console → remote unit")
                accent: sv.enabled ? win.p.ok : win.p.border
                Layout.fillWidth: true
                Layout.preferredWidth: 3
                Layout.fillHeight: true
                focus: true
                RowLayout {
                    spacing: 16 * win.s
                    Layout.fillWidth: true
                    Rectangle { width: 22 * win.s; height: 11 * win.s; radius: 2 * win.s; color: win.p.accent }
                    Label { text: qsTr("console"); font.pixelSize: 16 * win.s; color: win.p.muted }
                    Rectangle { width: 22 * win.s; height: 11 * win.s; radius: 2 * win.s; color: win.p.ok }
                    Label { text: qsTr("remote unit (echo)"); font.pixelSize: 16 * win.s; color: win.p.muted; elide: Text.ElideRight; Layout.fillWidth: !page.emu }
                    Rectangle { visible: page.emu; width: 22 * win.s; height: 11 * win.s; radius: 2 * win.s; color: page.emuColor }
                    Label {
                        objectName: "sticksEmuLegend"
                        visible: page.emu
                        text: qsTr("module emulator")
                        font.pixelSize: 16 * win.s
                        color: win.p.muted
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                }
                Repeater {
                    model: 8
                    delegate: RowLayout {
                        required property int index
                        objectName: "sticksChannel_" + index
                        spacing: 10 * win.s
                        Layout.fillWidth: true
                        Label {
                            text: qsTr("CH%1").arg(index + 1)
                            font.pixelSize: 17 * win.s
                            font.bold: true
                            color: page.sv.present && index < page.sv.axisCount ? win.p.text : win.p.muted
                            Layout.preferredWidth: 52 * win.s
                        }
                        ColumnLayout {
                            spacing: 3 * win.s
                            Layout.fillWidth: true
                            AxisBar { Layout.fillWidth: true; level: page.sv.axes[index]; fill: win.p.accent; active: page.sv.present }
                            AxisBar { Layout.fillWidth: true; level: page.sv.remoteAxes[index]; fill: page.echoColor; active: page.echo }
                            AxisBar { Layout.fillWidth: true; visible: page.emu; level: page.emu ? page.mv.emuChannels[index] : 0; fill: page.emuColor }
                        }
                        Label {
                            objectName: "sticksAxisValue_" + index
                            text: page.sv.present ? page.value(page.sv.axes[index]) : "—"
                            font.pixelSize: 17 * win.s
                            horizontalAlignment: Text.AlignRight
                            Layout.preferredWidth: numMetrics.advanceWidth + 4 * win.s
                        }
                        Label {
                            objectName: "sticksRemoteAxisValue_" + index
                            text: page.hasRemote ? page.value(page.sv.remoteAxes[index]) : "—"
                            font.pixelSize: 17 * win.s
                            color: page.echo ? page.echoColor : win.p.muted
                            horizontalAlignment: Text.AlignRight
                            Layout.preferredWidth: numMetrics.advanceWidth + 4 * win.s
                        }
                        Label {
                            objectName: "sticksEmuAxisValue_" + index
                            visible: page.emu
                            text: page.emu ? page.value(page.mv.emuChannels[index]) : "—"
                            font.pixelSize: 17 * win.s
                            color: page.emuColor
                            horizontalAlignment: Text.AlignRight
                            Layout.preferredWidth: numMetrics.advanceWidth + 4 * win.s
                        }
                    }
                }
                Rectangle { color: win.p.border; implicitHeight: 1; Layout.fillWidth: true; Layout.topMargin: 4 * win.s }
                GridLayout {                            // приём команд блоком (rc-state)
                    objectName: "sticksLinkStats"
                    columns: 2
                    rowSpacing: 2 * win.s
                    columnSpacing: 16 * win.s
                    Layout.fillWidth: true
                    Label {
                        objectName: "sticksRateText"
                        text: page.hasRemote ? qsTr("Rate: %1\u00a0Hz").arg(win.num(sv.rateHz, 1)) : qsTr("Rate: —")
                        font.pixelSize: 17 * win.s
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                    }
                    Label {
                        objectName: "sticksLatencyText"
                        text: sv.latencyMs >= 0 ? qsTr("Latency: %1\u00a0ms").arg(win.num(sv.latencyMs, 0)) : qsTr("Latency: —")
                        font.pixelSize: 17 * win.s
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                    }
                    Label {
                        objectName: "sticksGapsText"
                        text: page.hasRemote ? qsTr("Gaps: %1").arg(sv.gaps) : qsTr("Gaps: —")
                        color: page.hasRemote && sv.gaps > 0 ? win.p.warning : win.p.text
                        font.pixelSize: 17 * win.s
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                    }
                    Label {
                        objectName: "sticksFailsafesText"
                        text: page.hasRemote ? qsTr("Failsafes: %1").arg(sv.failsafes) : qsTr("Failsafes: —")
                        color: page.hasRemote && sv.failsafes > 0 ? win.p.warning : win.p.text
                        font.pixelSize: 17 * win.s
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                    }
                }
            }

            ColumnLayout {
                spacing: 16 * win.s
                Layout.fillWidth: true
                Layout.preferredWidth: 2
                Layout.fillHeight: true

                Tile {
                    objectName: "sticksDeviceTile"
                    title: qsTr("Control device")
                    accent: sv.present ? win.p.ok : win.p.warning
                    Layout.fillWidth: true
                    Label {
                        objectName: "sticksDeviceText"
                        text: sv.present ? sv.deviceName : qsTr("No control device")
                        color: sv.present ? win.p.text : win.p.warning
                        font.pixelSize: 20 * win.s
                        font.bold: true
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                    Label {
                        objectName: "sticksDeviceDetail"
                        text: sv.present ? qsTr("Axes: %1 · buttons: %2").arg(sv.axisCount).arg(sv.buttonCount)
                                         : qsTr("Connect the radio via USB in USB Joystick mode")
                        color: win.p.muted
                        font.pixelSize: 17 * win.s
                        wrapMode: Text.WordWrap
                        maximumLineCount: 2
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                }

                Tile {
                    id: buttonsTile
                    objectName: "sticksButtonsTile"
                    title: qsTr("Buttons")
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    GridLayout {
                        columns: 8
                        rowSpacing: 4 * win.s
                        columnSpacing: 4 * win.s
                        Layout.fillWidth: true
                        Repeater {
                            model: 24
                            delegate: Rectangle {
                                required property int index
                                readonly property bool pressed: page.sv.present && ((page.sv.buttons >> index) & 1) === 1
                                readonly property bool remote: page.echo && ((page.sv.remoteButtons >> index) & 1) === 1
                                readonly property bool exists: page.sv.present && index < page.sv.buttonCount
                                objectName: "sticksButton_" + index
                                Layout.fillWidth: true
                                Layout.preferredWidth: 1
                                implicitHeight: 36 * win.s
                                radius: 4 * win.s
                                color: pressed ? win.p.accent : win.p.bg
                                border.color: remote ? page.echoColor : win.p.border
                                border.width: remote ? 3 : 1
                                opacity: exists || !page.sv.present ? 1.0 : 0.4
                                Label {
                                    anchors.centerIn: parent
                                    text: index + 1
                                    font.pixelSize: 15 * win.s
                                    color: parent.pressed ? "#ffffff" : win.p.muted
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    Dialog {
        id: confirmDialog
        objectName: "sticksConfirmDialog"
        height: 320 * win.s          // явная высота: иначе цикл привязок implicitHeight в Fusion Dialog
        // Без сенсора (REQ-P-003): фокус на «Отмене»; Tab — к подтверждению, Enter — кнопка в фокусе, Esc — отмена
        onOpened: { confirmDialog.standardButton(Dialog.Ok).text = qsTr("Enable transmission"); confirmDialog.standardButton(Dialog.Cancel).forceActiveFocus() }   // действие с последствиями: по умолчанию — отказ
        Shortcut {
            sequences: ["Return", "Enter"]
            enabled: confirmDialog.opened
            onActivated: confirmDialog.standardButton(Dialog.Ok).activeFocus ? confirmDialog.accept() : confirmDialog.reject()   // Enter — кнопка в фокусе
        }
        anchors.centerIn: Overlay.overlay
        modal: true
        width: Math.min(760 * win.s, page.width)
        standardButtons: Dialog.Ok | Dialog.Cancel
        title: qsTr("Enable command transmission?")
        Label {
            objectName: "sticksConfirmQuestion"
            text: qsTr("Axes and buttons of «%1» are sent to the remote unit 50 times per second. If commands stop for longer than %2\u00a0ms, the remote unit switches to FAILSAFE.")
                  .arg(sv.deviceName !== "" ? sv.deviceName : qsTr("control device")).arg(sv.timeoutMs) + " " + page.outputNote()
            wrapMode: Text.WordWrap
            width: confirmDialog.availableWidth
        }
        onAccepted: sv.setEnabled(true, true)
    }
}
