// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Внешний модуль удалённого блока (ADR-0012, раздел 10): только показ status.module и команды module-cmd через
// backend.module (ModuleView). Все команды — после подтверждения в диалоге. С S8 (ADR-0014, 3.3): строка выхода
// (нет / нейтраль / команды), блок CRSF и выбор канала к модулю (лабораторный эмулятор / эмулятор CRSF / UART).
Item {
    id: page
    readonly property var mv: backend.module
    readonly property bool ok: mv.support === "ok"
    readonly property bool busy: mv.command.state === "sending"
    readonly property bool canCommand: ok && !busy
    readonly property string shownState: ok ? mv.state : mv.lastState      // при «нет связи» — последнее известное
    readonly property bool moduleEnabled: mv.settings.enabled !== false && mv.state !== "DISABLED"
    readonly property bool hasData: page.ok || mv.stale                     // есть что показать (в том числе устаревшее)
    readonly property var cr: mv.crsf

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
    function backendName(b) {
        switch (b) {
        case "lab-emulator": return qsTr("Laboratory emulator")
        case "crsf-pty-lab": return qsTr("CRSF module emulator")
        case "crsf-uart": return qsTr("CRSF module on UART")
        default: return b !== "" ? b : "—"
        }
    }
    function backendDescription(b) {
        switch (b) {
        case "lab-emulator": return qsTr("Module state model from S6. No output to the module.")
        case "crsf-pty-lab": return qsTr("ELRS TX emulator on a pseudo-terminal of the remote unit: CRSF channel frames without a radio.")
        case "crsf-uart": return qsTr("Physical JR module on UART3 of the remote unit (CRSF, 400000 baud). Requires an image with UART3.")
        default: return ""
        }
    }
    function emulatorLine() {
        if (mv.backendKind === "" && mv.backendSetting === "")
            return qsTr("Emulator: no data")
        if (mv.backendSetting === "crsf-pty-lab")
            return qsTr("CRSF emulator: %1 · PID %2").arg(page.emulatorStateText(mv.emulatorState))
                   .arg(mv.emulatorPid > 0 ? mv.emulatorPid : "—")
        if (mv.backendSetting === "crsf-uart")
            return mv.uartMissing ? qsTr("CRSF on UART: UART not found")
                                  : qsTr("CRSF on UART: %1").arg(page.cr.port !== undefined && page.cr.port !== "" ? page.cr.port : "—")
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
    function outputText(o) {          // как ModuleView.output_text (локальный канал и ui-state)
        switch (o) {
        case "none": return qsTr("Output: none")
        case "neutral": return qsTr("Output: neutral")
        case "live": return qsTr("Output: commands")
        case "unknown": return qsTr("Output: unknown value from the unit")
        default: return ""
        }
    }
    function outputColor(o) {
        return o === "live" ? win.p.ok : o === "neutral" ? win.p.info : o === "unknown" ? win.p.warning : win.p.muted
    }
    function has(v) { return v !== undefined && v !== null }
    function signed(v) { return v < 0 ? "\u2212" + (-v) : "" + v }    // минус, а не дефис
    function crsfDeviceText() {
        var d = page.cr.device
        if (!d || !d.name)
            return qsTr("Device: no answer")
        return d.sw ? qsTr("Device: %1 · firmware %2").arg(d.name).arg(d.sw) : qsTr("Device: %1").arg(d.name)
    }
    function crsfPeriodText() {
        return page.cr.periodUs ? qsTr("Period: %1\u00a0ms").arg(win.num(page.cr.periodUs / 1000, 1)) : qsTr("Period: —")
    }
    function crsfSyncText() {
        return page.cr.synced ? qsTr("Synchronization: yes") : qsTr("Synchronization: no")
    }
    function crsfRateText() {
        return page.has(page.cr.rateHz) ? qsTr("Frames: %1\u00a0Hz").arg(win.num(page.cr.rateHz, 1)) : qsTr("Frames: —")
    }
    function crsfJitterText() {
        return page.has(page.cr.jitterP99Ms) && page.cr.framesSent > 0 ? qsTr("Jitter p99: %1\u00a0ms").arg(win.num(page.cr.jitterP99Ms, 2))
                                             : qsTr("Jitter p99: —")
    }
    function crsfLinkText() {
        var l = page.cr.link
        if (!l || !page.has(l.rssi))
            return qsTr("RSSI / LQ: —")
        return qsTr("RSSI %1\u00a0dBm · LQ %2\u00a0%").arg(page.signed(l.rssi)).arg(page.has(l.lq) ? l.lq : "—")
    }
    function crsfErrorsText() {
        return qsTr("CRC errors: %1 · collisions %2").arg(page.cr.crcErrors !== undefined ? page.cr.crcErrors : 0)
               .arg(page.cr.collisions !== undefined ? page.cr.collisions : 0)
    }
    function crsfModeText(m) {
        switch (m) {
        case "none": return qsTr("no channel frames")
        case "neutral": return qsTr("neutral")
        case "live": return qsTr("commands")
        default: return m !== "" ? m : "—"
        }
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
        case "silent": return qsTr("Module silent")
        case "garbage": return qsTr("Garbage on the line")
        case "crc": return qsTr("Corrupted CRC")
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
        case "silent": return qsTr("the module emulator stops answering: no synchronization and no link statistics")
        case "garbage": return qsTr("random bytes are mixed into the module answers")
        case "crc": return qsTr("the module answers carry a wrong CRC")
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
        if (mv.isCrsf && page.cr.mode !== undefined) {
            var c = page.cr, dv = c.device || {}, l = c.link || {}, e = c.emu || {}
            rows.push({k: qsTr("CRSF frames"), v: qsTr("%1 · sent %2").arg(page.crsfModeText(c.mode)).arg(c.framesSent)})
            rows.push({k: qsTr("Synchronization"), v: c.synced && page.has(c.syncAgeMs)
                       ? qsTr("%1\u00a0ms ago").arg(Math.round(c.syncAgeMs)) : qsTr("none")})
            rows.push({k: qsTr("Last module answer"), v: page.has(c.lastRxMs) ? qsTr("%1\u00a0ms ago").arg(Math.round(c.lastRxMs)) : "—"})
            rows.push({k: qsTr("Module serial number"), v: dv.serial ? dv.serial : "—"})
            rows.push({k: qsTr("Radio link"), v: page.has(l.snr) || page.has(l.rfMode) || page.has(l.txPower)
                       ? qsTr("SNR %1\u00a0dB · RF mode %2 · power %3\u00a0mW").arg(page.has(l.snr) ? page.signed(l.snr) : "—")
                         .arg(page.has(l.rfMode) ? l.rfMode : "—").arg(page.has(l.txPower) ? l.txPower : "—") : "—"})
            if (e.state !== undefined)
                rows.push({k: qsTr("Module emulator"), v: [e.state !== "" ? e.state : "—",
                           e.armed === true ? qsTr("armed") : e.armed === false ? qsTr("not armed") : "",
                           page.has(e.rateHz) ? qsTr("%1\u00a0Hz").arg(win.num(e.rateHz, 1)) : ""]
                           .filter(function (x) { return x !== "" }).join(" · ")})
        }
        if (mv.backendSetting === "crsf-uart" && se.uart !== undefined)
            rows.push({k: qsTr("UART line"), v: qsTr("%1 · %2 baud").arg(se.uart.device !== "" ? se.uart.device : "—")
                       .arg(se.uart.baud) + (se.uart.half_duplex ? " · " + qsTr("half duplex") : "")})
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

        // Обязательная плашка лабораторного канала (лабораторный эмулятор S6 или эмулятор CRSF); видна и без данных
        Rectangle {
            objectName: "moduleLabBanner"
            readonly property bool shown: mv.isLab || !page.ok      // для ui-state: видимость без учёта вкладки
            visible: shown
            color: win.p.warning
            radius: 6 * win.s
            Layout.fillWidth: true
            implicitHeight: bannerText.implicitHeight + 12 * win.s
            Label {
                id: bannerText
                objectName: "moduleLabBannerText"
                anchors.fill: parent
                anchors.leftMargin: 16 * win.s
                anchors.rightMargin: 16 * win.s
                verticalAlignment: Text.AlignVCenter
                text: mv.backendSetting === "crsf-pty-lab" ? qsTr("CRSF module emulator, not a physical module: no radio transmission")
                                                           : qsTr("Laboratory emulator, not a physical JR module")
                color: "#ffffff"
                font.bold: true
                elide: Text.ElideRight
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
                objectName: "moduleStateDetail"         // для FAULT — что именно (Ошибка интерфейса и т. п.); нет UART
                readonly property bool shown: page.ok && (mv.uartMissing || mv.state === "FAULT" && mv.lastErrorCode !== "")
                visible: shown
                text: mv.uartMissing ? qsTr("UART not found") : page.errorName(mv.lastErrorCode)
                color: win.p.fault
                font.pixelSize: 24 * win.s
                font.bold: true
                wrapMode: Text.WordWrap                 // рядом строка выхода: длинное описание — в две строки
                maximumLineCount: 2
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
            Item { Layout.fillWidth: true; visible: page.ok && !(mv.uartMissing || mv.state === "FAULT" && mv.lastErrorCode !== "") }
            Rectangle {                                 // выход на модуль (ADR-0014, 2): нет / нейтраль / команды
                objectName: "moduleOutputBadge"
                readonly property bool shown: page.ok && mv.output !== ""      // без связи выход неизвестен
                visible: shown
                radius: 6 * win.s
                color: page.outputColor(mv.output)
                opacity: mv.stale ? 0.6 : 1.0
                implicitWidth: outputLabel.implicitWidth + 28 * win.s
                implicitHeight: outputLabel.implicitHeight + 12 * win.s
                Layout.maximumWidth: page.width * 0.4
                Label {
                    id: outputLabel
                    objectName: "moduleOutputText"
                    readonly property bool shown: parent.shown
                    anchors.centerIn: parent
                    width: Math.min(implicitWidth, parent.width - 28 * win.s)
                    text: page.outputText(mv.output)
                    color: "#ffffff"
                    font.pixelSize: 22 * win.s
                    font.bold: true
                    elide: Text.ElideRight
                }
            }
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
                RowLayout {
                    spacing: 10 * win.s
                    Layout.fillWidth: true
                    Label {
                        objectName: "moduleEmulatorText"
                        text: page.emulatorLine()
                        color: mv.uartMissing && page.ok ? win.p.fault : win.p.text
                        opacity: mv.stale ? 0.55 : 1.0
                        wrapMode: Text.WordWrap
                        maximumLineCount: 2
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                    Button {
                        objectName: "moduleBackendButton"
                        visible: page.ok || mv.support === "no-link"
                        text: qsTr("Change channel…")
                        enabled: page.canCommand
                        font.pixelSize: 17 * win.s
                        opacity: enabled ? 1.0 : 0.45
                        Layout.preferredHeight: 44 * win.s
                        Layout.preferredWidth: implicitWidth + 24 * win.s
                        onClicked: backendDialog.ask()
                    }
                }
                GridLayout {                                 // выход CRSF (status.module.crsf, ADR-0014, 3.1)
                    objectName: "moduleCrsfBlock"
                    readonly property bool shown: mv.isCrsf && page.hasData
                    visible: shown
                    columns: 2
                    rowSpacing: 2 * win.s
                    columnSpacing: 16 * win.s
                    opacity: mv.stale ? 0.55 : 1.0
                    Layout.fillWidth: true
                    Label {
                        objectName: "moduleCrsfDeviceText"
                        text: page.crsfDeviceText()
                        font.pixelSize: 17 * win.s
                        font.bold: page.cr.device !== undefined && !!page.cr.device.name
                        elide: Text.ElideRight
                        Layout.columnSpan: 2
                        Layout.fillWidth: true
                    }
                    Label {
                        objectName: "moduleCrsfPeriodText"
                        text: page.crsfPeriodText()
                        font.pixelSize: 17 * win.s
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                    }
                    Label {
                        objectName: "moduleCrsfSyncText"
                        text: page.crsfSyncText()
                        color: page.cr.synced ? win.p.text : win.p.warning
                        font.pixelSize: 17 * win.s
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                    }
                    Label {
                        objectName: "moduleCrsfRateText"
                        text: page.crsfRateText()
                        font.pixelSize: 17 * win.s
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                    }
                    Label {
                        objectName: "moduleCrsfJitterText"
                        text: page.crsfJitterText()
                        font.pixelSize: 17 * win.s
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                    }
                    Label {
                        objectName: "moduleCrsfLinkText"
                        text: page.crsfLinkText()
                        font.pixelSize: 17 * win.s
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                    }
                    Label {
                        objectName: "moduleCrsfErrorsText"
                        text: page.crsfErrorsText()
                        color: page.cr.crcErrors > 0 || page.cr.collisions > 0 ? win.p.warning : win.p.text
                        font.pixelSize: 17 * win.s
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                    }
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
                    text: mv.isCrsf ? qsTr("The state comes from the remote unit; the console only shows it and sends commands after confirmation. The remote unit sends neutral channels to the module while transmission is off or in FAILSAFE, and console commands while transmission is on.")
                                    : qsTr("The state comes from the remote unit; the console only shows it and sends commands after confirmation. Control output depends on the channel and the module state (see «Output»).")
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
                : confirmDialog.action === "disable" && mv.isCrsf ? qsTr("The remote unit stops the channel to the module: no CRSF frames are sent. The setting is saved and kept after a restart.")
                : confirmDialog.action === "enable" && mv.isCrsf ? qsTr("The remote unit starts the channel to the module and initializes the module. The setting is saved.")
                : confirmDialog.action === "disable" ? qsTr("The module emulator on the remote unit is stopped. The setting is saved and kept after a restart.")
                : confirmDialog.action === "enable" ? qsTr("The remote unit starts the module emulator and initializes the module. The setting is saved.")
                : mv.backendSetting === "crsf-pty-lab"
                  ? qsTr("CRSF module emulator on the remote unit: %1. Only the emulator is affected: there is no physical module and no radio transmission.")
                    .arg(page.labEffect(confirmDialog.scenario))
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

    // Выбор канала к модулю (configure backend): выбор + «ОК» — подтверждение; без выбора другого канала «ОК» неактивна
    Dialog {
        id: backendDialog
        objectName: "moduleBackendDialog"
        property string choice
        readonly property bool changed: choice !== "" && choice !== mv.backendSetting
        function ask() {
            choice = mv.backendSetting
            open()
        }
        function move(step) {
            var list = mv.backendChoices, i = list.indexOf(choice)
            choice = list[Math.max(0, Math.min(list.length - 1, (i < 0 ? 0 : i + step)))]
        }
        function okButton() { return standardButton(Dialog.Ok) }
        onChangedChanged: if (okButton()) okButton().enabled = changed
        onOpened: {
            okButton().enabled = changed
            okButton().forceActiveFocus()
        }
        Shortcut {
            sequences: ["Return", "Enter"]
            enabled: backendDialog.opened
            onActivated: if (backendDialog.changed) backendDialog.accept()
        }
        Shortcut { sequence: "Up"; enabled: backendDialog.opened; onActivated: backendDialog.move(-1) }
        Shortcut { sequence: "Down"; enabled: backendDialog.opened; onActivated: backendDialog.move(1) }
        // явная высота (по содержимому, не по implicitHeight диалога): иначе цикл привязок в Fusion Dialog
        height: Math.min(backendCol.implicitHeight + 124 * win.s, page.height)
        anchors.centerIn: Overlay.overlay
        modal: true
        width: Math.min(820 * win.s, page.width)
        standardButtons: Dialog.Ok | Dialog.Cancel
        title: qsTr("Change the module channel?")
        ColumnLayout {
            id: backendCol
            width: backendDialog.availableWidth
            spacing: 8 * win.s
            Repeater {
                model: mv.backendChoices
                delegate: Rectangle {
                    id: option
                    required property string modelData
                    readonly property bool selected: backendDialog.choice === modelData
                    readonly property bool current: mv.backendSetting === modelData
                    objectName: "moduleBackendOption_" + modelData
                    Layout.fillWidth: true
                    implicitHeight: optionCol.implicitHeight + 14 * win.s
                    radius: 6 * win.s
                    color: win.p.bg
                    border.color: selected ? win.p.accent : win.p.border
                    border.width: selected ? 3 : 1
                    TapHandler { onTapped: backendDialog.choice = option.modelData }
                    ColumnLayout {
                        id: optionCol
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.leftMargin: 10 * win.s
                        anchors.rightMargin: 10 * win.s
                        spacing: 0
                        RadioButton {
                            text: page.backendName(option.modelData) + (option.current ? " · " + qsTr("current") : "")
                            checked: option.selected
                            font.bold: true
                            focusPolicy: Qt.NoFocus
                            onClicked: backendDialog.choice = option.modelData
                            Layout.fillWidth: true
                        }
                        Label {
                            text: page.backendDescription(option.modelData)
                            color: win.p.muted
                            font.pixelSize: 16 * win.s
                            wrapMode: Text.WordWrap
                            leftPadding: 32 * win.s
                            Layout.fillWidth: true
                        }
                    }
                }
            }
            Label {
                objectName: "moduleBackendQuestion"
                text: backendDialog.changed
                      ? qsTr("The remote unit saves the setting and restarts the module on «%1». Until the module is ready there is no output to it.")
                        .arg(page.backendName(backendDialog.choice))
                      : qsTr("Select another channel and press «OK».")
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
                Layout.topMargin: 4 * win.s
            }
        }
        onAccepted: if (changed) mv.setBackend(choice, true)
    }
}
