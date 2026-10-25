// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: tile
    property string title
    property color accent: win.p.border
    default property alias content: body.data
    // Второстепенная строка: показывается, только если с ней содержимое помещается в плитку (1280×800 — высота
    // ограничена; строка не выталкивает остальные за рамку). Высота без неё считается по остальным строкам, а не по
    // implicitHeight тела, — иначе видимость зависела бы сама от себя (binding loop).
    property Item optional: null
    readonly property bool optionalFits: {
        if (!optional)
            return true
        var h = 0, n = 0
        for (var i = 0; i < body.children.length; i++) {
            var c = body.children[i]
            if (c === optional || !c.visible)
                continue
            h += c.implicitHeight
            n++
        }
        // место под тело: поля 16+16 и промежутки заголовок–тело–заполнитель 8+8
        return h + n * body.spacing + optional.implicitHeight <= height - head.implicitHeight - 48 * win.s + 1
    }

    color: win.p.panel
    radius: 8 * win.s
    border.color: tile.activeFocus ? win.p.accent : win.p.border
    border.width: tile.activeFocus ? 3 : 1
    implicitHeight: body.implicitHeight + head.implicitHeight + 40 * win.s
    activeFocusOnTab: true

    Rectangle {
        width: 6 * win.s
        radius: 3 * win.s
        color: tile.accent
        anchors { left: parent.left; top: parent.top; bottom: parent.bottom; margins: 8 * win.s }
    }
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 16 * win.s
        anchors.leftMargin: 28 * win.s
        spacing: 8 * win.s
        Label {
            id: head
            text: tile.title
            color: win.p.muted
            font.pixelSize: 18 * win.s
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
        ColumnLayout {
            id: body
            spacing: 6 * win.s
            Layout.fillWidth: true
        }
        Item { Layout.fillHeight: true }
    }
}
