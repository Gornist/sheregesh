// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Внешний модуль удалённого блока (ADR-0012, раздел 10): только показ status.module и команды module-cmd через
// backend.module (ModuleView). Все команды — после подтверждения в диалоге. Модуль в S6 — лабораторный эмулятор.
Item {
    id: page
    readonly property var mv: backend.module
    readonly property bool ok: mv.support === "ok"
    readonly property bool busy: mv.command.state === "sending"
    readonly property bool canCommand: ok && !busy
    readonly property string shownState: ok ? mv.state : mv.lastState      // при «нет связи» — последнее известное
    readonly property bool moduleEnabled: mv.settings.enabled !== false && mv.state !== "DISABLED"

    function stateText(s) {
        switch (s) {
        case "DISCONNECTED": return qsTr("Module absent")
        case "DETECTED": return qsTr("Module detected")
        case "INITIALIZING": return qsTr("Initializing")
        case "READY": return qsTr("Ready")
        case "FAULT": return qsTr("Fault")
        case "RECOVERING": return qsTr("Recovering")
        case "DISABLED": return qsTr("Disabled")
        default: return s !== "" ? s : "—"
        }
    }
    function badgeText() {            // как ModuleView.state_text (локальный канал и ui-state)
        if (page.ok)
            return page.stateText(mv.state)
        switch (mv.support) {
        case "old-remote": return qsTr("Not supported by the remote unit")
        case "absent": return qsTr("No module data")
        case "local": return qsTr("No remote unit")
        default: return qsTr("No link to the remote unit")
        }
    }
    function supportText() {
        switch (mv.support) {
        case "no-link": return qsTr("Commands are unavailable until the link is restored.")
        case "old-remote": return qsTr("The remote unit does not support the external module (protocol %1)").arg(mv.protocolNote)
        case "absent": return qsTr("The remote unit does not report the module state")
        case "local": return qsTr("The external module is available only with a remote unit")
        default: return ""
        }
    }
    function stateColor(s) {
        if (!page.ok)
            return mv.support === "no-link" ? win.p.warning : win.p.muted
        switch (s) {
        case "READY": return win.p.ok
        case "FAULT": return win.p.fault
        case "DISABLED": return win.p.muted
        case "DISCONNECTED": return win.p.warning
        default: return win.p.info
        }
    }
    function errorName(code) {
        switch (code) {
        case "MOD-IFACE-ERROR": return qsTr("Interface error")
        case "MOD-NO-RESPONSE": return qsTr("Module does not respond")
        case "MOD-INIT-TIMEOUT": return qsTr("Initialization did not finish in time")
        case "MOD-INIT-FAILED": return qsTr("Initialization error")
        case "MOD-PROFILE-MISMATCH": return qsTr("Module does not match the profile")
        case "MOD-RECOVER-TIMEOUT": return qsTr("Recovery did not finish")
        case "EMU-START-FAILED": return qsTr("Emulator did not start")
        case "EMU-EXITED": return qsTr("Emulator exited abnormally")
        case "EMU-NO-KEEPALIVE": return qsTr("Emulator does not respond")
        case "EMU-UNAVAILABLE": return qsTr("Emulator unavailable")
        default: return code
        }
    }
    function errorLine() {
        if (mv.lastErrorCode === "")
            return qsTr("Last error: none")
        return (mv.errorActive ? qsTr("Last error: %1 at %2 (active)") : qsTr("Last error: %1 at %2"))
               .arg(page.errorName(mv.lastErrorCode)).arg(mv.lastErrorAt)
    }
    function emulatorStateText(s) {
        switch (s) {
        case "running": return qsTr("running")
        case "starting": return qsTr("starting")
        case "restarting": return qsTr("restarting")
        case "stopped": return qsTr("stopped")
        case "unavailable": return qsTr("unavailable")
        default: return s !== "" ? s : "—"
        }
    }
    function emulatorLine() {
        if (mv.backendKind === "")
            return qsTr("Emulator: no data")
        if (mv.backendKind !== "lab-emulator")
            return qsTr("Module backend: %1").arg(mv.backendKind)
        return qsTr("Emulator: %1 · restarts %2 of %3 · PID %4").arg(page.emulatorStateText(mv.emulatorState))
               .arg(mv.emulatorRestarts).arg(mv.emulatorRestartLimit).arg(mv.emulatorPid > 0 ? mv.emulatorPid : "—")
    }
    function changedLine() {
        if (mv.changedAt === "")
            return ""
        return mv.changedAgoS >= 120 ? qsTr("Changed: %1 (%2 min ago)").arg(mv.changedAt).arg(Math.floor(mv.changedAgoS / 60))
                                     : qsTr("Changed: %1 (%2 s ago)").arg(mv.changedAt).arg(mv.changedAgoS)
    }
    function opText(op) {
        switch (op) {
        case "reinit": return qsTr("reinitialization")
        case "reset": return qsTr("error reset")
        case "configure": return qsTr("module settings")
        case "lab": return qsTr("laboratory scenario")
        default: return op
        }
    }
    function resultText(r) {
        switch (r) {
        case "no-link": case "not-connected": return qsTr("no link to the remote unit")
        case "unsupported": return qsTr("not supported by the remote unit")
        case "timeout": return qsTr("no result from the remote unit in time")
        case "busy": return qsTr("another command is in progress")
        case "disabled": return qsTr("the module is disabled")
        case "forbidden": return qsTr("allowed only with the laboratory emulator")
        case "not-running": return qsTr("the emulator is not running")
        case "invalid": return qsTr("value rejected by the remote unit")
        case "not-saved": return qsTr("the setting could not be saved on the remote unit")
        case "bad-request": return qsTr("invalid request")
        default: return r
        }
    }
    function commandLine() {
        var c = mv.command
        if (c.state === "sending")
            return qsTr("Command «%1» is being sent to the remote unit…").arg(page.opText(c.op))
        if (c.state === "done")
            return qsTr("Command «%1»: done").arg(page.opText(c.op))
        if (c.state === "error")
            return qsTr("Command «%1» failed: %2").arg(page.opText(c.op)).arg(page.resultText(c.result))
                   + (c.detail !== "" ? " (" + c.detail + ")" : "")
        return ""
    }
    function labText(name) {
        switch (name) {
        case "attach": return qsTr("Insert module")
        case "detach": return qsTr("Remove module")
        case "iface-error": return qsTr("Interface error")
        case "hang": return qsTr("Module hang")
        case "heal": return qsTr("Clear faults")
        case "slow-init": return qsTr("Slow initialization")
        case "crash": return qsTr("Emulator crash")
        case "emu-unavailable": return qsTr("Emulator unavailable")
        case "emu-available": return qsTr("Emulator available")
        default: return name
        }
    }
    function labEffect(name) {
        switch (name) {
        case "attach": return qsTr("the module is inserted")
        case "detach": return qsTr("the module is removed and stops answering")
        case "iface-error": return qsTr("the module reports an interface error")
        case "hang": return qsTr("the module stays present but stops answering")
        case "heal": return qsTr("all simulated module faults are cleared")
        case "slow-init": return qsTr("the next initialization takes longer than allowed")
        case "crash": return qsTr("the emulator process exits abnormally and is restarted")
        case "emu-unavailable": return qsTr("the emulator process is stopped and new starts are refused")
        case "emu-available": return qsTr("emulator starts are allowed again (then use «Reset error»)")
        default: return name
        }
    }
    function sourceText(s) {
        switch (s) {
        case "store": return qsTr("saved")
        case "factory": return qsTr("factory defaults")
        case "recovered": return qsTr("recovered")
        case "reset": return qsTr("reset to factory defaults")
        case "memory": return qsTr("in memory only (not saved)")
        default: return s !== "" ? s : "—"
        }
    }
    function onOff(v) { return v ? qsTr("on") : qsTr("off") }
    function diagRows() {
        var d = mv.diagnostics, id = mv.identity, se = mv.settings, r = mv.recovery, rows = []
        rows.push({k: qsTr("State code"), v: (page.shownState !== "" ? page.shownState : "—")
                   + (mv.reason !== "" ? " · " + mv.reason : "")})
        rows.push({k: qsTr("Profile"), v: mv.profileId !== "" ? [mv.profileId, mv.profileFamily, mv.profileInterface]
                   .filter(function (x) { return x !== "" }).join(" · ") : "—"})
        rows.push({k: qsTr("Identity"), v: id.vendor !== undefined ? qsTr("%1 %2 · firmware %3 · S/N %4")
                   .arg(id.vendor).arg(id.model).arg(id.firmware).arg(id.serial) : "—"})
        rows.push({k: qsTr("Initialization"), v: d.init_ms !== undefined && d.init_ms > 0 ? qsTr("%1 ms").arg(d.init_ms) : "—"})
        rows.push({k: qsTr("Last answer"), v: d.pong_age_s !== undefined && d.pong_age_s !== null
                   ? qsTr("%1 s ago").arg(win.num(d.pong_age_s, 1)) : "—"})
        rows.push({k: qsTr("Counters"), v: d.faults !== undefined
                   ? qsTr("faults %1 · recoveries %2 · transitions %3").arg(d.faults).arg(d.recoveries).arg(d.transitions) : "—"})
        rows.push({k: qsTr("Automatic recovery"), v: r.auto === undefined ? "—"
                   : !r.auto ? page.onOff(false) : r.exhausted ? qsTr("stopped: restart limit reached")
                   : qsTr("on · attempt %1").arg(r.attempt) + (r.next_s !== null && r.next_s !== undefined
                   ? " · " + qsTr("next in %1 s").arg(win.num(r.next_s, 0)) : "")})
        rows.push({k: qsTr("Settings"), v: se.revision !== undefined
                   ? qsTr("revision %1 · %2").arg(se.revision !== "" ? se.revision : "—").arg(page.sourceText(se.source)) : "—"})
        rows.push({k: qsTr("Module settings"), v: se.enabled !== undefined
                   ? qsTr("module %1 · auto init %2 · auto recovery %3").arg(page.onOff(se.enabled))
                     .arg(page.onOff(se.auto_init)).arg(page.onOff(se.auto_recover)) : "—"})
        if (se.notice !== undefined && se.notice !== "")
            rows.push({k: qsTr("Settings notice"), v: se.notice})
        if (mv.lastErrorText !== "")
            rows.push({k: qsTr("Error text from the unit"), v: mv.lastErrorCode + ": " + mv.lastErrorText})
        rows.push({k: qsTr("Protocol session"), v: mv.protocolNote !== "" ? mv.protocolNote : "—"})
        return rows
    }
    function historyText(h) {
        return page.stateText(h.from) + " → " + page.stateText(h.state)
               + (h.reason !== "" ? " · " + h.reason : "") + (h.code !== "" ? " · " + h.code : "")
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 10 * win.s

        // Обязательная плашка: в S6 модуль только лабораторный эмулятор (видна и без данных от блока)
        Rectangle {
            objectName: "moduleLabBanner"
            readonly property bool shown: mv.isLab || !page.ok      // для ui-state: видимость без учёта вкладки
            visible: shown
            color: win.p.warning
            radius: 6 * win.s
            Layout.fillWidth: true
            implicitHeight: bannerRow.implicitHeight + 12 * win.s
            RowLayout {
                id: bannerRow
                anchors.fill: parent
                anchors.leftMargin: 16 * win.s
                anchors.rightMargin: 16 * win.s
                spacing: 16 * win.s
                Label {
                    objectName: "moduleLabBannerText"
                    text: qsTr("Laboratory emulator, not a physical JR module")
                    color: "#ffffff"
                    font.bold: true
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                Label {
                    objectName: "moduleOutputText"
                    text: mv.output === "unknown" ? qsTr("Control output: unknown value from the unit")
                                                  : qsTr("Control output: none (S6)")
                    color: "#ffffff"
                    font.bold: mv.output === "unknown"
                    elide: Text.ElideRight
                    Layout.maximumWidth: implicitWidth
                    Layout.minimumWidth: 0
                }
            }
        }

        RowLayout {
            spacing: 16 * win.s
            Layout.fillWidth: true
            Rectangle {
                objectName: "moduleStateBadge"
                radius: 6 * win.s
                color: page.stateColor(mv.state)
                implicitWidth: stateTextLabel.implicitWidth + 32 * win.s
                implicitHeight: stateTextLabel.implicitHeight + 12 * win.s
                Layout.maximumWidth: page.width * 0.6
                Label {
                    id: stateTextLabel
                    objectName: "moduleStateText"
                    anchors.centerIn: parent
                    width: Math.min(implicitWidth, parent.width - 32 * win.s)
                    text: page.badgeText()
                    color: "#ffffff"
                    font.pixelSize: 26 * win.s
                    font.bold: true
                    elide: Text.ElideRight
                }
            }
            Label {
                objectName: "moduleStateDetail"         // для FAULT — что именно (Ошибка интерфейса и т. п.)
                readonly property bool shown: page.ok && mv.state === "FAULT" && mv.lastErrorCode !== ""
                visible: shown
                text: page.errorName(mv.lastErrorCode)
                color: win.p.fault
                font.pixelSize: 24 * win.s
                font.bold: true
                elide: Text.ElideRight
                Layout.fillWidth: true
                Layout.minimumWidth: 0
            }
            Label {
                objectName: "moduleSupportText"
                readonly property bool shown: !page.ok
                visible: shown
                text: page.supportText()
                wrapMode: Text.WordWrap
                maximumLineCount: 2
                elide: Text.ElideRight
                Layout.fillWidth: true
            }
            Item { Layout.fillWidth: true; visible: page.ok && mv.state !== "FAULT" }
            Label {
                objectName: "moduleChangedText"
                readonly property bool shown: text !== ""
                visible: shown
                text: page.changedLine()
                color: win.p.muted
                opacity: mv.stale ? 0.6 : 1.0
            }
        }

        RowLayout {
            spacing: 16 * win.s
            Layout.fillWidth: true
            Layout.fillHeight: true

            Tile {
                id: moduleTile
                objectName: "moduleTile"
                title: qsTr("External module")
                accent: page.stateColor(mv.state)
                Layout.fillWidth: true
                Layout.preferredWidth: 3
                Layout.fillHeight: true
                focus: true
                optional: moduleNote
                Label {
                    objectName: "moduleProfileText"
                    text: mv.profileName !== "" ? mv.profileName + " (" + mv.profileId + ")" : qsTr("Profile: not reported")
                    font.pixelSize: 24 * win.s
                    font.bold: true
                    opacity: mv.stale ? 0.55 : 1.0
                    wrapMode: Text.WordWrap
                    maximumLineCount: 2
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                Label {
                    objectName: "moduleEmulatorText"
                    text: page.emulatorLine()
                    opacity: mv.stale ? 0.55 : 1.0
                    wrapMode: Text.WordWrap
                    maximumLineCount: 2
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                Label {
                    objectName: "moduleErrorText"
                    text: page.errorLine()
                    color: mv.errorActive && page.ok ? win.p.fault : mv.lastErrorCode !== "" ? win.p.text : win.p.muted
                    opacity: mv.stale ? 0.55 : 1.0
                    wrapMode: Text.WordWrap
                    maximumLineCount: 2
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                Label {
                    objectName: "moduleStaleText"
                    visible: mv.stale
                    text: qsTr("Data as of %1").arg(mv.staleAt) + " · " + qsTr("last known state: %1").arg(page.stateText(mv.lastState))
                    color: win.p.muted
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                Label {
                    objectName: "moduleCommandText"
                    visible: text !== ""
                    text: page.commandLine()
                    color: mv.command.state === "error" ? win.p.fault : mv.command.state === "done" ? win.p.ok : win.p.info
                    wrapMode: Text.WordWrap
                    maximumLineCount: 2
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                RowLayout {
                    objectName: "moduleCommandRow"
                    visible: page.ok || mv.support === "no-link"        // старый блок, нет блока: команд нет вовсе
                    spacing: 10 * win.s
                    Layout.fillWidth: true
                    Layout.topMargin: 4 * win.s
                    Button {
                        objectName: "moduleReinitButton"
                        text: qsTr("Reinitialize")
                        enabled: page.canCommand && page.moduleEnabled
                        font.pixelSize: 18 * win.s
                        opacity: enabled ? 1.0 : 0.45
                        Layout.preferredHeight: 52 * win.s
                        Layout.fillWidth: true
                        Layout.preferredWidth: implicitWidth
                        onClicked: confirmDialog.ask("reinit", "")
                    }
                    Button {
                        objectName: "moduleResetButton"
                        text: qsTr("Reset error")
                        enabled: page.canCommand && page.moduleEnabled
                        font.pixelSize: 18 * win.s
                        opacity: enabled ? 1.0 : 0.45
                        Layout.preferredHeight: 52 * win.s
                        Layout.fillWidth: true
                        Layout.preferredWidth: implicitWidth
                        onClicked: confirmDialog.ask("reset", "")
                    }
                    Button {
                        objectName: "moduleEnableButton"
                        text: page.moduleEnabled ? qsTr("Disable module") : qsTr("Enable module")
                        enabled: page.canCommand
                        font.pixelSize: 18 * win.s
                        opacity: enabled ? 1.0 : 0.45
                        Layout.preferredHeight: 52 * win.s
                        Layout.fillWidth: true
                        Layout.preferredWidth: implicitWidth
                        onClicked: confirmDialog.ask(page.moduleEnabled ? "disable" : "enable", "")
                    }
                }
                Label {                                      // второстепенная: только если помещается в плитку
                    id: moduleNote
                    objectName: "moduleNote"
                    visible: moduleTile.optionalFits
                    text: qsTr("The state comes from the remote unit; the console only shows it and sends commands after confirmation. There is no control output in any state.")
                    color: win.p.muted
                    font.pixelSize: 16 * win.s
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                    Layout.topMargin: 6 * win.s
                }
            }

            Rectangle {                                     // диагностика и история переходов — вкладками
                objectName: "moduleDiagTile"
                color: win.p.panel
                radius: 8 * win.s
                border.color: win.p.border
                Layout.fillWidth: true
                Layout.preferredWidth: 2
                Layout.fillHeight: true
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 12 * win.s
                    spacing: 8 * win.s
                    TabBar {
                        id: infoTabs
                        objectName: "moduleInfoTabs"
                        Layout.fillWidth: true
                        TabButton { objectName: "moduleDiagTab"; text: qsTr("Diagnostics"); font.pixelSize: 18 * win.s }
                        TabButton { objectName: "moduleHistoryTab"; text: qsTr("Transitions"); font.pixelSize: 18 * win.s }
                    }
                    StackLayout {
                        currentIndex: infoTabs.currentIndex
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        ListView {
                            id: diagList
                            objectName: "moduleDiagList"
                            clip: true
                            spacing: 4 * win.s
                            boundsBehavior: Flickable.StopAtBounds
                            model: page.diagRows()
                            opacity: mv.stale ? 0.55 : 1.0
                            ScrollBar.vertical: ScrollBar { policy: diagList.contentHeight > diagList.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff }
                            delegate: RowLayout {
                                required property var modelData
                                width: ListView.view.width - 12 * win.s
                                spacing: 10 * win.s
                                Label {
                                    text: modelData.k
                                    color: win.p.muted
                                    font.pixelSize: 16 * win.s
                                    wrapMode: Text.WordWrap
                                    Layout.preferredWidth: parent.width * 0.4
                                    Layout.alignment: Qt.AlignTop
                                }
                                Label {
                                    text: modelData.v
                                    font.pixelSize: 16 * win.s
                                    wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                                    Layout.fillWidth: true
                                    Layout.alignment: Qt.AlignTop
                                }
                            }
                        }
                        ListView {
                            id: historyList
                            objectName: "moduleHistoryList"
                            clip: true
                            spacing: 4 * win.s
                            boundsBehavior: Flickable.StopAtBounds
                            model: mv.history
                            opacity: mv.stale ? 0.55 : 1.0
                            ScrollBar.vertical: ScrollBar { policy: historyList.contentHeight > historyList.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff }
                            Label {
                                anchors.centerIn: parent
                                visible: historyList.count === 0
                                text: qsTr("No transitions")
                                color: win.p.muted
                            }
                            delegate: RowLayout {
                                required property var modelData
                                width: ListView.view.width - 12 * win.s
                                spacing: 10 * win.s
                                Label { text: modelData.at !== "" ? modelData.at : "—"; color: win.p.muted; font.pixelSize: 16 * win.s; Layout.alignment: Qt.AlignTop }
                                Label {
                                    text: page.historyText(modelData)
                                    color: modelData.state === "FAULT" ? win.p.fault : modelData.state === "READY" ? win.p.ok : win.p.text
                                    font.pixelSize: 16 * win.s
                                    wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                                    Layout.fillWidth: true
                                }
                            }
                        }
                    }
                }
            }
        }

        Tile {
            id: labPanel
            objectName: "moduleLabPanel"
            visible: mv.isLab
            title: qsTr("Laboratory scenarios (emulator only)")
            accent: win.p.warning
            Layout.fillWidth: true
            GridLayout {
                id: labGrid
                Layout.fillWidth: true
                columns: Math.max(2, Math.min(5, Math.floor(labPanel.width / (240 * win.s))))
                rowSpacing: 8 * win.s
                columnSpacing: 8 * win.s
                Repeater {
                    model: mv.labScenarios
                    delegate: Button {
                        required property string modelData
                        objectName: "moduleLabButton_" + modelData
                        text: page.labText(modelData)
                        enabled: page.canCommand
                        font.pixelSize: 17 * win.s
                        opacity: enabled ? 1.0 : 0.45
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                        Layout.preferredHeight: 48 * win.s
                        onClicked: confirmDialog.ask("lab", modelData)
                    }
                }
            }
        }
    }

    Dialog {
        id: confirmDialog
        objectName: "moduleConfirmDialog"
        property string action
        property string scenario
        function ask(a, sc) {
            action = a
            scenario = sc
            open()
        }
        height: 320 * win.s          // явная высота: иначе цикл привязок implicitHeight в Fusion Dialog
        // Без сенсора: фокус на «ОК», Enter подтверждает, Esc отменяет (REQ-P-003)
        onOpened: standardButton(Dialog.Ok).forceActiveFocus()
        Shortcut {
            sequences: ["Return", "Enter"]
            enabled: confirmDialog.opened
            onActivated: confirmDialog.accept()
        }
        anchors.centerIn: Overlay.overlay
        modal: true
        width: Math.min(760 * win.s, page.width)
        standardButtons: Dialog.Ok | Dialog.Cancel
        title: action === "reinit" ? qsTr("Reinitialize the module?")
             : action === "reset" ? qsTr("Reset the module error?")
             : action === "disable" ? qsTr("Disable the external module?")
             : action === "enable" ? qsTr("Enable the external module?")
             : qsTr("Run laboratory scenario «%1»?").arg(page.labText(scenario))
        Label {
            objectName: "moduleConfirmQuestion"
            text: confirmDialog.action === "reinit" ? qsTr("The remote unit restarts module initialization. The module is not ready until it finishes.")
                : confirmDialog.action === "reset" ? qsTr("The emulator restart budget and the recovery delay are reset; after a fault the remote unit starts recovery.")
                : confirmDialog.action === "disable" ? qsTr("The module emulator on the remote unit is stopped. The setting is saved and kept after a restart.")
                : confirmDialog.action === "enable" ? qsTr("The remote unit starts the module emulator and initializes the module. The setting is saved.")
                : qsTr("Laboratory emulator on the remote unit: %1. Only the emulator is affected: there is no physical module and no control output.")
                  .arg(page.labEffect(confirmDialog.scenario))
            wrapMode: Text.WordWrap
            width: confirmDialog.availableWidth
        }
        onAccepted: {
            if (action === "reinit") mv.requestReinit(true)
            else if (action === "reset") mv.requestReset(true)
            else if (action === "disable") mv.setEnabled(false, true)
            else if (action === "enable") mv.setEnabled(true, true)
            else if (action === "lab") mv.runLabScenario(scenario, true)
        }
    }
}
