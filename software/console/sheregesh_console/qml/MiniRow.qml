// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Строка «название — значение» всплывающей панели (MainMinimal.qml).
RowLayout {
    property alias label: key.text
    property alias value: val.text
    property color valueColor: win.p.text
    property alias valueObject: val
    Layout.fillWidth: true
    spacing: 10 * win.k
    Label { id: key; color: win.p.muted; font.pixelSize: 13 * win.k; Layout.alignment: Qt.AlignTop }
    Label {
        id: val
        color: valueColor
        font.pixelSize: 13 * win.k
        font.bold: true
        horizontalAlignment: Text.AlignRight
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
    }
}
