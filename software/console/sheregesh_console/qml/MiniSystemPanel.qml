// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Раздел «Система»: версия ПО, DEV-выпуск, OTA и слоты (только чтение), дисплей; настройки, питание и
// сведения — прежними страницами (опасные действия — их подтверждениями).
ColumnLayout {
    id: panel
    spacing: 8 * win.k
    readonly property var si: systemInfo.info
    readonly property var st: backend.status
    onVisibleChanged: if (visible) systemInfo.refresh()        // команды OTA/выпуска — только при открытии
    Component.onCompleted: if (visible) systemInfo.refresh()

    MiniRow { label: qsTr("Console software"); value: "%1 · %2 %3".arg(panel.st.software).arg(qsTr("protocol")).arg(panel.st.localProtocol) }
    MiniRow { objectName: "miniSysRelease"; label: qsTr("Release"); value: panel.si.release !== "" ? panel.si.release : "—" }
    MiniRow { label: qsTr("Previous release"); value: panel.si.previous !== "" ? panel.si.previous : "—" }
    MiniRow {
        objectName: "miniSysOta"
        label: "OTA"
        value: panel.si.otaState !== "" ? panel.si.otaState + (panel.si.tryboot ? " · tryboot" : "") : (systemInfo.busy ? "…" : "—")
        valueColor: panel.si.otaState === "COMMITTED" ? win.p.ok : panel.si.otaState !== "" ? win.p.warning : win.p.text
    }
    MiniRow {
        label: qsTr("Slots")
        value: panel.si.slotActive !== "" ? qsTr("active %1 · other %2").arg(panel.si.slotActive).arg(panel.si.slotOther) : "—"
    }
    MiniRow { label: qsTr("System image"); value: panel.si.imageVersion !== "" ? panel.si.imageVersion + " · " + panel.si.imageCommit : "—" }
    MiniRow { label: qsTr("Display"); value: "%1×%2 · %3 %4 %".arg(win.width).arg(win.height).arg(qsTr("scale")).arg(backend.uiScale) }
    MiniRow { label: qsTr("Language · theme"); value: backend.language.toUpperCase() + " · " + backend.theme }
    Label {
        visible: panel.si.switching !== ""
        text: panel.si.switching
        color: win.p.muted
        font.pixelSize: 12 * win.k
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
    }
    MiniDetails {
        pages: [{page: "SettingsPage", label: qsTr("Settings")}, {page: "PowerPage", label: qsTr("Power")},
                {page: "AboutPage", label: qsTr("About")}]
    }
}
