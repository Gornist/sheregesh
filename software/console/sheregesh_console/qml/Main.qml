// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: win
    objectName: "mainWindow"
    width: 1920
    height: 1200
    visible: true
    title: qsTr("Sheregesh — console (laboratory emulation)")

    readonly property real s: backend.uiScale / 100
    readonly property var p: backend.palette
    readonly property var st: backend.status
    property alias currentPage: tabs.currentIndex

    function stateColor(state) {
        return state === "fault" ? p.fault : state === "warning" ? p.warning : state === "boot" ? p.boot : p.ok
    }
    function num(v, digits) {        // дробная часть: запятая в RU, точка в EN; без разделителя тысяч
        var t = Number(v).toFixed(digits)
        return backend.language === "ru" ? t.replace(".", ",") : t
    }
    // Обобщённое состояние канала с удалённым блоком (backend.link_view): индикатор виден на всех экранах
    function linkColor(v) {
        return v === "CONNECTED" ? p.ok : v === "ERROR" ? p.fault : v === "CONNECTING" ? p.info : p.warning
    }
    function linkText(v) {
        return v === "CONNECTED" ? qsTr("Remote unit: connected") : v === "ERROR" ? qsTr("Remote unit: link error")
             : v === "CONNECTING" ? qsTr("Remote unit: connecting…") : qsTr("Remote unit: no link")
    }
    // Источник видео (backend.videoSource, sheregesh_console.videosource): тип входа и состояние источника
    function videoInputText(input) {
        return input === "synthetic" ? qsTr("Synthetic") : input === "hdmi" ? "HDMI" : input === "cvbs" ? "CVBS"
             : input === "usb" ? qsTr("USB (UVC)") : input === "v4l2" ? "V4L2" : input === "rtsp" ? "RTSP"
             : input !== "" ? input.toUpperCase() : qsTr("unknown")
    }
    function videoSourceStateText(state) {
        return state === "streaming" ? qsTr("streaming") : state === "no-signal" ? qsTr("no signal")
             : state === "no-device" ? qsTr("no device") : state === "starting" ? qsTr("starting")
             : state === "connect-error" ? qsTr("connection error") : state === "error" ? qsTr("error")
             : state === "reconnecting" ? qsTr("reconnecting") : qsTr("not reported")
    }
    function stateText(state) {
        return state === "fault" ? qsTr("Fault") : state === "warning" ? qsTr("Warning")
             : state === "boot" ? qsTr("Starting") : qsTr("Normal")
    }

    color: p.bg
    font.pixelSize: 20 * s
    palette.window: p.bg
    palette.windowText: p.text
    palette.base: p.panel
    palette.alternateBase: p.bg
    palette.text: p.text
    palette.button: p.panel
    palette.buttonText: p.text
    palette.highlight: p.accent
    palette.highlightedText: "#ffffff"
    palette.placeholderText: p.muted
    palette.mid: p.border
    palette.light: p.panel
    palette.dark: p.border

    // Экраны: Ctrl+1…8 или F1…F8 (без сенсора, REQ-P-003); «Модуль» — последней, индексы 0…6 прежние
    Repeater {
        model: 8
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
                focus: true
                Keys.onLeftPressed: decrementCurrentIndex()
                Keys.onRightPressed: incrementCurrentIndex()
                Keys.onDownPressed: pages.children[currentIndex].forceActiveFocus()
                TabButton { objectName: "tabDashboard"; text: qsTr("Dashboard"); width: implicitWidth + 32 * win.s }
                TabButton { objectName: "tabVideo"; text: qsTr("Video"); width: implicitWidth + 32 * win.s }
                TabButton { objectName: "tabDiagnostics"; text: qsTr("Diagnostics") + (st.activeCount > 0 ? " (" + st.activeCount + ")" : ""); width: implicitWidth + 32 * win.s }
                TabButton { objectName: "tabPower"; text: qsTr("Power"); width: implicitWidth + 32 * win.s }
                TabButton { objectName: "tabLogs"; text: qsTr("Logs"); width: implicitWidth + 32 * win.s }
                TabButton { objectName: "tabSettings"; text: qsTr("Settings"); width: implicitWidth + 32 * win.s }
                TabButton { objectName: "tabAbout"; text: qsTr("About"); width: implicitWidth + 32 * win.s }
                TabButton { objectName: "tabModule"; text: qsTr("Module"); width: implicitWidth + 32 * win.s }
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
