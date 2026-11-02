// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Клавиша экранной клавиатуры (OnScreenKeyboard.qml): фокус не забирает — поле ввода остаётся в фокусе.
AbstractButton {
    id: key
    property string label: ""
    property real units: 1
    property real k: 1
    property real keyH: 52
    focusPolicy: Qt.NoFocus
    Layout.preferredWidth: keyH * 1.15 * units
    Layout.preferredHeight: keyH
    contentItem: Label {
        text: key.label
        color: "#e8eaed"
        font.pixelSize: 20 * key.k
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
    }
    background: Rectangle {
        radius: 4 * key.k
        color: key.pressed ? "#4a5a66" : (key.checked ? "#2f6f9f" : "#2a333b")
        border.color: "#3a4650"
    }
}
