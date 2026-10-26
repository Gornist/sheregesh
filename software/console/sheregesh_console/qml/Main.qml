// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ConsoleWindow {
    id: win
    property alias currentPage: tabs.currentIndex

    // Экраны: Ctrl+1…9 или F1…F9 (без сенсора, REQ-P-003); «Модуль» (S6) и «Управление» (S7) — в конце, прежние
    // индексы не меняются
    Repeater {
        model: 9
        delegate: Item {
            required property int index
            Shortcut { sequences: ["Ctrl+" + (index + 1), "F" + (index + 1)]; onActivated: { tabs.currentIndex = index; tabs.forceActiveFocus() } }
        }
    }
    Shortcut { sequence: "Escape"; onActivated: tabs.forceActiveFocus() }

    header: Rectangle {
        color: p.panel
        height: 132 * s
        border.color: p.border
        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 12 * s
            spacing: 8 * s
            RowLayout {
                spacing: 16 * s
                Label {
                    text: qsTr("Sheregesh console")
                    font.pixelSize: 26 * win.s
                    font.bold: true
                }
                Label {
                    objectName: "emulationNote"
                    text: qsTr("Emulation · synthetic data only")
                    color: p.muted
                    elide: Text.ElideRight          // на 1280×800 уступает место индикаторам связи и состояния
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                }
                Rectangle {
                    objectName: "linkBadge"
                    radius: 6 * win.s
                    color: win.linkColor(st.linkView)
                    implicitWidth: linkBadgeText.implicitWidth + 32 * win.s
                    implicitHeight: linkBadgeText.implicitHeight + 12 * win.s
                    Label {
                        id: linkBadgeText
                        objectName: "linkBadgeText"
                        anchors.centerIn: parent
                        text: win.linkText(st.linkView)
                        color: "#ffffff"
                        font.bold: true
                    }
                }
                Rectangle {
                    objectName: "stateBadge"
                    radius: 6 * win.s
                    color: win.stateColor(st.overall)
                    implicitWidth: badgeText.implicitWidth + 32 * win.s
                    implicitHeight: badgeText.implicitHeight + 12 * win.s
                    Label {
                        id: badgeText
                        objectName: "stateBadgeText"
                        anchors.centerIn: parent
                        text: win.stateText(st.overall)
                        color: "#ffffff"
                        font.bold: true
                    }
                }
            }
            TabBar {
                id: tabs
                objectName: "tabBar"
                Layout.fillWidth: true
                Layout.preferredWidth: 1            // ширина — только от раскладки, не от суммы вкладок (pad зависит от width)
                // Поля вкладок: 24·s, пока девять вкладок помещаются; иначе поровну урезаются до нуля
                // (шрифты CI шире шрифтов устройства — вкладка «Управление» уходила за край на 1280×800).
                readonly property real pad: {
                    var sum = 0
                    for (var i = 0; i < count; ++i)
                        sum += itemAt(i).implicitWidth
                    return count ? Math.max(0, Math.min(24 * win.s, Math.floor((width - sum) / count))) : 24 * win.s
                }
                focus: true
                Keys.onLeftPressed: decrementCurrentIndex()
                Keys.onRightPressed: incrementCurrentIndex()
                Keys.onDownPressed: pages.children[currentIndex].forceActiveFocus()
                TabButton { objectName: "tabDashboard"; text: qsTr("Dashboard"); width: implicitWidth + tabs.pad }
                TabButton { objectName: "tabVideo"; text: qsTr("Video"); width: implicitWidth + tabs.pad }
                TabButton { objectName: "tabDiagnostics"; text: qsTr("Diagnostics") + (st.activeCount > 0 ? " (" + st.activeCount + ")" : ""); width: implicitWidth + tabs.pad }
                TabButton { objectName: "tabPower"; text: qsTr("Power"); width: implicitWidth + tabs.pad }
                TabButton { objectName: "tabLogs"; text: qsTr("Logs"); width: implicitWidth + tabs.pad }
                TabButton { objectName: "tabSettings"; text: qsTr("Settings"); width: implicitWidth + tabs.pad }
                TabButton { objectName: "tabAbout"; text: qsTr("About"); width: implicitWidth + tabs.pad }
                TabButton { objectName: "tabModule"; text: qsTr("Module"); width: implicitWidth + tabs.pad }
                TabButton { objectName: "tabSticks"; text: qsTr("Control"); width: implicitWidth + tabs.pad }
            }
        }
    }

    StackLayout {
        id: pages
        anchors.fill: parent
        anchors.margins: 16 * win.s
        currentIndex: tabs.currentIndex
        DashboardPage { objectName: "dashboardPage" }
        VideoPage { objectName: "videoPage" }
        DiagnosticsPage { objectName: "diagnosticsPage" }
        PowerPage { objectName: "powerPage" }
        LogsPage { objectName: "logsPage" }
        SettingsPage { objectName: "settingsPage" }
        AboutPage { objectName: "aboutPage" }
        ModulePage { objectName: "modulePage" }
        SticksPage { objectName: "sticksPage" }
    }

    footer: Rectangle {
        color: p.panel
        height: 44 * win.s
        border.color: p.border
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 16 * win.s
            anchors.rightMargin: 16 * win.s
            spacing: 32 * win.s
            Label {
                objectName: "footerTime"
                text: qsTr("Model time %1\u00a0s").arg(win.num(st.t, 1))
                color: p.muted
            }
            Label {
                visible: st.scenario !== ""
                text: qsTr("Scenario: %1").arg(st.scenarioLabel !== "" ? st.scenarioLabel : st.scenario)
                color: p.muted
                elide: Text.ElideRight
                Layout.fillWidth: true
            }
            Item { Layout.fillWidth: st.scenario === "" }
            Label {
                text: qsTr("Software %1 · protocol %2").arg(st.software).arg(st.localProtocol)
                color: p.muted
            }
        }
    }
}
