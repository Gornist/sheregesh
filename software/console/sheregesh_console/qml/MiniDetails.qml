// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Кнопки «Подробно»: прежние страницы на месте видео. Пока команды идут на модуль, недоступны — видео не закрывается.
ColumnLayout {
    property var pages: []                  // [{page: "VideoPage", label: "…"}]
    Layout.fillWidth: true
    Layout.topMargin: 8 * win.k
    spacing: 6 * win.k
    Repeater {
        model: pages
        delegate: Button {
            required property var modelData
            objectName: "details_" + modelData.page
            text: modelData.label
            enabled: !win.outputLive
            font.pixelSize: 14 * win.k
            Layout.fillWidth: true
            Layout.preferredHeight: 44 * win.k
            onClicked: win.showDetails(modelData.page)
        }
    }
    Label {
        visible: win.outputLive
        text: qsTr("Full pages cover the video: unavailable while commands go to the module.")
        color: win.p.muted
        font.pixelSize: 12 * win.k
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
    }
}
