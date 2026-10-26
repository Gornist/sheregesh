// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Shapes

// Иконка нижнего меню: контур 24×24 (PathSvg), без растровых файлов и модуля SVG (на плате его нет).
// Геометрия статична — Shapes триангулирует её один раз.
Item {
    id: icon
    property string name
    property color color: "white"
    readonly property var paths: ({
        "video": "M4 4h11a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z M17 9l5-3v12l-5-3",
        "control": "M6.5 8h11a4 4 0 0 1 3.9 3.2l1.1 5.3a3 3 0 0 1-4.6 3.1L15 17H9l-2.9 2.6a3 3 0 0 1-4.6-3.1l1.1-5.3A4 4 0 0 1 6.5 8z M7 12v5 M4.5 14.5h5",
        "module": "M12 21V9 M8 21h8 M9 6a4 4 0 0 1 6 0 M6 3a8 8 0 0 1 12 0",
        "diagnostics": "M2 12h4l3-8 5 16 3-8h5",
        "system": "M12 9a3 3 0 1 1 0 6a3 3 0 1 1 0-6z M10 2h4l1 3 2 1 3-1 2 4-2 2v2l2 2-2 4-3-1-2 1-1 3h-4l-1-3-2-1-3 1-2-4 2-2v-2L2 9l2-4 3 1 2-1z"
    })
    implicitWidth: 16
    implicitHeight: 16
    Shape {
        anchors.fill: parent
        scale: Math.min(icon.width, icon.height) / 24
        transformOrigin: Item.TopLeft
        preferredRendererType: Shape.GeometryRenderer
        ShapePath {
            strokeColor: icon.color
            strokeWidth: 1.8
            fillColor: "transparent"
            capStyle: ShapePath.RoundCap
            joinStyle: ShapePath.RoundJoin
            PathSvg { path: icon.paths[icon.name] || "" }
        }
    }
}
