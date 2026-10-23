// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: page
    property var issues: []
    readonly property var cfg: backend.config

    function form() {
        return {
            device_name: nameField.text,
            language: ["ru", "en"][languageBox.currentIndex],
            theme: ["light", "dark"][themeBox.currentIndex],
            ui_scale: [100, 125, 150][scaleBox.currentIndex],
            screen_timeout_s: timeoutSpin.value,
            temp_warning_c: tempWarnSpin.value,
            temp_critical_c: tempCritSpin.value,
            battery_warning_pct: battWarnSpin.value,
            battery_critical_pct: battCritSpin.value,
            peer_address: peerField.text,
            port: portSpin.value,
            heartbeat_timeout_s: hbSpin.value
        }
    }
    function load() {
        nameField.text = cfg.device_name
        languageBox.currentIndex = ["ru", "en"].indexOf(cfg.language)
        themeBox.currentIndex = ["light", "dark"].indexOf(cfg.theme)
        scaleBox.currentIndex = [100, 125, 150].indexOf(cfg.ui_scale)
        timeoutSpin.value = cfg.screen_timeout_s
        tempWarnSpin.value = cfg.temp_warning_c
        tempCritSpin.value = cfg.temp_critical_c
        battWarnSpin.value = cfg.battery_warning_pct
        battCritSpin.value = cfg.battery_critical_pct
        peerField.text = cfg.peer_address
        portSpin.value = cfg.port
        hbSpin.value = cfg.heartbeat_timeout_s
        issues = []
    }
    function dirty() {
        var f = form()
        for (var k in f)
            if (String(f[k]) !== String(cfg[k])) return true
        return false
    }
    function validate() {
        issues = backend.validateForm(form())
        return issues.length === 0
    }

    Component.onCompleted: load()
    Connections { target: backend; function onConfigChanged() { page.load() } }

    ScrollView {
        anchors.fill: parent
        contentWidth: availableWidth

        ColumnLayout {
            width: parent.width
            spacing: 16 * win.s

            RowLayout {
                spacing: 16 * win.s
                Label { text: qsTr("Active revision: %1").arg(backend.revision); font.bold: true; objectName: "revisionLabel" }
                Label { text: page.dirty() ? qsTr("unsaved changes") : qsTr("no changes"); color: page.dirty() ? win.p.warning : win.p.muted; objectName: "dirtyLabel" }
                Item { Layout.fillWidth: true }
                Label { text: qsTr("Test configuration · emulation"); color: win.p.muted }
            }

            GridLayout {
                columns: 4
                columnSpacing: 20 * win.s
                rowSpacing: 12 * win.s
                Layout.fillWidth: true

                Label { text: qsTr("Device name") }
                TextField { id: nameField; objectName: "nameField"; Layout.fillWidth: true; focus: true; KeyNavigation.tab: languageBox }
                Label { text: qsTr("Language") }
                ComboBox { id: languageBox; objectName: "languageBox"; model: ["Русский", "English"]; Layout.fillWidth: true }

                Label { text: qsTr("Theme") }
                ComboBox { id: themeBox; objectName: "themeBox"; model: [qsTr("Light"), qsTr("Dark")]; Layout.fillWidth: true }
                Label { text: qsTr("Interface scale") }
                ComboBox { id: scaleBox; objectName: "scaleBox"; model: ["100 %", "125 %", "150 %"]; Layout.fillWidth: true }

                Label { text: qsTr("Screen timeout, s") }
                SpinBox { id: timeoutSpin; objectName: "timeoutSpin"; from: 0; to: 3600; stepSize: 30; editable: true; Layout.fillWidth: true }
                Label { text: qsTr("Heartbeat timeout, s") }
                SpinBox { id: hbSpin; objectName: "hbSpin"; from: 1; to: 5; editable: true; Layout.fillWidth: true }

                Label { text: qsTr("Temperature warning, °C") }
                SpinBox { id: tempWarnSpin; objectName: "tempWarnSpin"; from: 40; to: 85; editable: true; Layout.fillWidth: true }
                Label { text: qsTr("Temperature critical, °C") }
                SpinBox { id: tempCritSpin; objectName: "tempCritSpin"; from: 50; to: 90; editable: true; Layout.fillWidth: true }

                Label { text: qsTr("Battery warning, %") }
                SpinBox { id: battWarnSpin; objectName: "battWarnSpin"; from: 5; to: 50; editable: true; Layout.fillWidth: true }
                Label { text: qsTr("Battery critical, %") }
                SpinBox { id: battCritSpin; objectName: "battCritSpin"; from: 1; to: 30; editable: true; Layout.fillWidth: true }

                Label { text: qsTr("Remote unit address") }
                TextField { id: peerField; objectName: "peerField"; Layout.fillWidth: true }
                Label { text: qsTr("Port") }
                SpinBox { id: portSpin; objectName: "portSpin"; from: 1024; to: 65535; editable: true; Layout.fillWidth: true }
            }

            Repeater {
                model: page.issues
                delegate: Label {
                    required property var modelData
                    text: "⚠ " + modelData
                    color: win.p.fault
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
            }

            RowLayout {
                spacing: 12 * win.s
                Button { objectName: "validateButton"; text: qsTr("Check"); onClicked: page.validate() }
                Button {
                    objectName: "applyButton"
                    text: qsTr("Apply…")
                    highlighted: true
                    enabled: page.dirty()
                    onClicked: if (page.validate()) confirm.open()
                }
                Button { objectName: "revertButton"; text: qsTr("Discard changes"); enabled: page.dirty(); onClicked: page.load() }
                Item { Layout.fillWidth: true }
                Button { objectName: "backupButton"; text: qsTr("Back up to USB"); onClicked: backend.backup() }
                Button {
                    objectName: "restoreButton"
                    text: qsTr("Restore from USB…")
                    onClicked: { restoreDialog.files = backend.backups(); restoreDialog.open() }
                }
            }

            Label {
                objectName: "lastResult"
                text: backend.lastResult
                visible: text !== ""
                color: win.p.muted
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            Label { text: qsTr("Revision history"); font.bold: true }
            Repeater {
                model: backend.history
                delegate: Label {
                    required property var modelData
                    text: modelData.id + " · " + modelData.created + " · " + modelData.reason + (modelData.confirmed ? "" : " · " + qsTr("not confirmed"))
                    color: modelData.id === backend.revision ? win.p.text : win.p.muted
                }
            }
        }
    }

    Dialog {
        id: confirm
        // Без сенсора: фокус на «ОК», Enter подтверждает, Esc отменяет (REQ-P-003)
        onOpened: standardButton(Dialog.Ok) ? standardButton(Dialog.Ok).forceActiveFocus() : standardButton(Dialog.Cancel).forceActiveFocus()
        Shortcut {
            sequences: ["Return", "Enter"]
            enabled: confirm.opened
            onActivated: confirm.standardButton(Dialog.Ok) ? confirm.accept() : confirm.reject()
        }
        objectName: "confirmDialog"
        anchors.centerIn: Overlay.overlay
        modal: true
        title: qsTr("Apply settings?")
        width: 720 * win.s
        standardButtons: Dialog.Ok | Dialog.Cancel
        Label {
            text: qsTr("A new configuration revision will be created and checked by the affected services. If the check fails, the previous revision is restored automatically.")
            wrapMode: Text.WordWrap
            width: confirm.availableWidth
        }
        onAccepted: backend.applyForm(page.form(), true)
    }

    Dialog {
        id: restoreDialog
        // Без сенсора: фокус на «ОК», Enter подтверждает, Esc отменяет (REQ-P-003)
        onOpened: standardButton(Dialog.Ok) ? standardButton(Dialog.Ok).forceActiveFocus() : standardButton(Dialog.Cancel).forceActiveFocus()
        Shortcut {
            sequences: ["Return", "Enter"]
            enabled: restoreDialog.opened
            onActivated: restoreDialog.standardButton(Dialog.Ok) ? restoreDialog.accept() : restoreDialog.reject()
        }
        objectName: "restoreDialog"
        property var files: []
        anchors.centerIn: Overlay.overlay
        modal: true
        title: qsTr("Restore configuration from USB?")
        width: 720 * win.s
        standardButtons: files.length > 0 ? (Dialog.Ok | Dialog.Cancel) : Dialog.Cancel
        ColumnLayout {
            width: restoreDialog.availableWidth
            Label {
                text: restoreDialog.files.length > 0 ? qsTr("The backup will be checked for integrity and compatibility, then applied as a new revision.") : qsTr("No backups found on the USB drive.")
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
            ComboBox { id: backupBox; model: restoreDialog.files; visible: restoreDialog.files.length > 0; Layout.fillWidth: true }
        }
        onAccepted: if (files.length > 0) backend.restore(backupBox.currentText, true)
    }
}
