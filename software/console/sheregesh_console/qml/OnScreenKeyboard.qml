// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Экранная клавиатура пульта: появляется, пока в фокусе редактируемое поле (имя, адрес блока, числа), и вводит
// символы прямо в него. Клавиши фокус не забирают. Раскладки RU/EN, Shift, цифры и знаки адреса.
// Готово — снять фокус (поле применяет ввод по editingFinished); у верхнего края, если поле внизу экрана.
Rectangle {
    id: kb
    objectName: "onScreenKeyboard"
    property Item target: null             // поле ввода в фокусе (TextInput / TextField / поле SpinBox)
    property real k: 1
    property bool ru: false
    property bool shift: false
    readonly property var rowsEn: ["1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm.-"]
    readonly property var rowsRu: ["1234567890", "йцукенгшщзх", "фывапролджэ", "ячсмитьбю.-"]
    readonly property var rows: ru ? rowsRu : rowsEn
    readonly property real keyH: 52 * k
    height: col.implicitHeight + 16 * k
    color: "#1b232b"
    border.color: "#3a4650"

    function type(ch) {
        if (!target)
            return
        if (target.selectedText !== undefined && target.selectedText !== "")
            target.remove(target.selectionStart, target.selectionEnd)
        target.insert(target.cursorPosition, shift ? ch.toUpperCase() : ch)
        shift = false
    }
    function backspace() {
        if (!target)
            return
        if (target.selectedText !== "")
            target.remove(target.selectionStart, target.selectionEnd)
        else if (target.cursorPosition > 0)
            target.remove(target.cursorPosition - 1, target.cursorPosition)
    }
    function done() {
        if (target)
            target.focus = false           // editingFinished: поле и SpinBox применяют введённое
    }

    ColumnLayout {
        id: col
        anchors { horizontalCenter: parent.horizontalCenter; top: parent.top; topMargin: 8 * kb.k }
        spacing: 6 * kb.k
        Repeater {
            model: kb.rows
            delegate: RowLayout {
                required property string modelData
                Layout.alignment: Qt.AlignHCenter
                spacing: 6 * kb.k
                Repeater {
                    model: modelData.split("")
                    delegate: OskKey {
                        required property string modelData
                        k: kb.k; keyH: kb.keyH
                        objectName: "osk_" + modelData
                        label: kb.shift ? modelData.toUpperCase() : modelData
                        onClicked: kb.type(modelData)
                    }
                }
            }
        }
        RowLayout {
            Layout.alignment: Qt.AlignHCenter
            spacing: 6 * kb.k
            OskKey { k: kb.k; keyH: kb.keyH; objectName: "osk_shift"; label: "⇧"; units: 1.4; checkable: true; checked: kb.shift; onClicked: kb.shift = !kb.shift }
            OskKey { k: kb.k; keyH: kb.keyH; objectName: "osk_lang"; label: kb.ru ? "EN" : "RU"; units: 1.4; onClicked: kb.ru = !kb.ru }
            OskKey { k: kb.k; keyH: kb.keyH; label: ":"; onClicked: kb.type(":") }
            OskKey { k: kb.k; keyH: kb.keyH; label: "_"; onClicked: kb.type("_") }
            OskKey { k: kb.k; keyH: kb.keyH; objectName: "osk_space"; label: qsTr("space"); units: 4; onClicked: kb.type(" ") }
            OskKey { k: kb.k; keyH: kb.keyH; objectName: "osk_backspace"; label: "⌫"; units: 1.6; onClicked: kb.backspace() }
            OskKey { k: kb.k; keyH: kb.keyH; objectName: "osk_done"; label: qsTr("Done"); units: 2.2; onClicked: kb.done() }
        }
    }
}
