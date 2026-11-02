// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Sheregesh 1.0

// Интерфейс Minimal: строка состояния 32, видео, нижнее меню 44 (база 1280×800, масштаб k по экрану).
// Разделы открываются панелью справа поверх видео (поле видео не меняет размер); «Подробно» — прежняя страница
// на месте видео, пока команды не идут на модуль. Строка состояния читает backend 4 раза в секунду (bar),
// а не на каждое изменение стиков; панели создаются только открытыми (Loader).
ConsoleWindow {
    id: win
    readonly property real k: Math.min(width / 1280, height / 800) * backend.uiScale / 100   // «Масштаб интерфейса»
    readonly property real barH: Math.round(32 * k)
    readonly property real navH: Math.round(44 * k)
    property string section: ""            // открытый раздел: video | control | module | diagnostics | system | ""
    property string details: ""            // прежняя страница на месте видео: VideoPage | SticksPage | … | ""
    property var bar: collect()
    property real openStartedMs: 0         // время нажатия (замер открытия раздела до первого кадра с панелью)
    property real lastOpenMs: -1

    readonly property var sections: [
        {key: "video", label: qsTr("Video")}, {key: "control", label: qsTr("Control")},
        {key: "module", label: qsTr("Module")}, {key: "diagnostics", label: qsTr("Diagnostics")},
        {key: "system", label: qsTr("System")}]
    readonly property bool outputLive: bar.output === "live"
    readonly property bool linkDown: bar.origin === "remote" && bar.link !== "CONNECTED"
    // Несовместимая версия блока — не «нет связи»: связь есть, обмен не начат (REQ-SYS-008, сообщение с версиями)
    readonly property bool incompatible: linkDown && bar.pstate === "incompatible"
    // Причина отсутствия связи — для текста поверх видео: что произошло и что делать оператору
    readonly property string linkCause: !linkDown || incompatible ? ""
        : bar.peerLink === "AUTH_FAILED" ? "auth"
        : bar.remoteOff ? "off"
        : bar.peerLink === "RECONNECTING" || bar.everConnected ? "lost" : "waiting"
    function linkTitle() {
        return linkCause === "auth" ? qsTr("REMOTE UNIT NOT AUTHORIZED")
             : linkCause === "off" ? qsTr("REMOTE UNIT SWITCHED OFF")
             : linkCause === "waiting" ? qsTr("WAITING FOR THE REMOTE UNIT") : qsTr("NO LINK TO THE REMOTE UNIT")
    }
    function linkHint() {
        return linkCause === "auth"
               ? qsTr("The console and the remote unit do not trust each other's certificates. The link will not recover by itself: pair the devices again (manual, section «Building»).")
             : linkCause === "off"
               ? qsTr("The remote unit was shut down from the console. Switch its power off and on; the link restores automatically.")
             : linkCause === "waiting"
               ? qsTr("No link since the console started. Check that the remote unit is powered and the Ethernet cable is connected; the console connects automatically.")
               : qsTr("The link to the remote unit is lost; reconnecting automatically. Check the Ethernet cable and the remote unit power.")
    }
    // Активные ошибки (REQ-UI-003): полоса поверх видео с текстом и действием; потеря связи — уже в центре экрана
    readonly property var activeErrors: backend.events.filter(function (e) {
        return e.active && e.severity === "error" && !(win.linkDown && (e.code === "PEER-LOST" || e.code === "LINK-AUTH-FAIL"))
    })

    function collect() {
        var sv = backend.sticks, mv = backend.module, vs = backend.videoSource, st = backend.status
        return {link: st.linkView, peerLink: st.peerLink, overall: st.overall, active: st.activeCount,
                pstate: st.peerState, pproto: st.peerProtocol, lproto: st.localProtocol, pdetail: st.peerDetail,
                remoteOff: !!st.remoteOff, everConnected: !!st.linkEverConnected,
                ctl: sv.support, rstate: sv.remoteState, reason: sv.remoteReason, enabled: sv.enabled,
                present: sv.present, device: sv.deviceName,
                msup: mv.support, mstate: mv.state, profile: mv.profileName, mbackend: mv.backendSetting,
                output: mv.support === "ok" ? mv.output : "",
                input: vs.input, display: vs.display, live: vs.live, origin: vs.origin,
                fps: backend.videoReceiver.fps, rtt: st.linkRttMs}
    }
    function open(key) {
        openStartedMs = Date.now()
        openLoaded = false
        details = ""
        section = section === key ? "" : key
    }
    function close() { section = ""; details = "" }
    function showDetails(page) {
        if (outputLive)
            return
        detailsSection = section
        section = ""
        details = page
    }
    onOutputLiveChanged: if (outputLive) details = ""        // команды пошли на модуль — видео снова видно
    property bool openLoaded: false
    property bool preload: false           // через 3 с после запуска — создать все панели в фоне
    readonly property var currentPanel: {
        for (var i = 0; i < sections.length; ++i)
            if (sections[i].key === section) return panels.itemAt(i)
        return null
    }
    Timer { interval: 3000; running: true; onTriggered: win.preload = true }
    Connections {                          // только пока идёт замер: обработчик не вызывается на каждый кадр видео
        target: win
        enabled: win.openStartedMs > 0 && win.openLoaded        // первый кадр с готовым содержимым панели
        function onFrameSwapped() { win.lastOpenMs = Date.now() - win.openStartedMs; win.openStartedMs = 0 }
    }

    // Строка состояния: тексты и цвета
    function ctlText() {
        if (bar.ctl === "no-link") return qsTr("no link")
        if (bar.ctl === "old-remote") return qsTr("not supported")
        var s = bar.rstate === "ACTIVE" ? qsTr("active") : bar.rstate === "FAILSAFE" ? "FAILSAFE"
              : bar.rstate === "IDLE" ? qsTr("idle") : "—"
        var o = bar.output === "live" ? qsTr("output: commands") : bar.output === "neutral" ? qsTr("output: neutral")
              : bar.msup === "ok" ? qsTr("no output") : ""
        return (bar.enabled ? s : qsTr("off") + (s !== "—" ? " · " + s : "")) + (o !== "" ? " · " + o : "")
    }
    function ctlColor() {
        if (bar.rstate === "FAILSAFE") return p.fault
        if (bar.ctl === "no-link") return p.warning
        return bar.rstate === "ACTIVE" ? p.ok : bar.ctl === "ok" ? p.info : p.muted
    }
    function linkValue() {           // переподключение после потери — «нет связи», а не «подключение» (первое)
        return bar.link === "CONNECTED" ? qsTr("connected") + (bar.rtt > 0 ? " · " + qsTr("%1 ms").arg(Math.round(bar.rtt)) : "")
             : bar.peerLink === "RECONNECTING" ? qsTr("no link · reconnecting")
             : bar.link === "CONNECTING" ? qsTr("connecting") : bar.link === "ERROR" ? qsTr("error") : qsTr("no link")
    }
    function linkTone() {
        return bar.link === "CONNECTED" ? p.ok : bar.peerLink === "RECONNECTING" ? p.fault : linkColor(bar.link)
    }
    function videoValue() {
        var what = bar.input !== "" ? videoInputText(bar.input) + " · " : ""
        if (bar.live)
            return what + Math.round(bar.fps) + "\u00a0" + qsTr("FPS")
        return bar.display === "reconnecting" || win.linkDown ? qsTr("no video") : what + videoSourceStateText(bar.display)
    }
    function shortDevice(n) {         // «OpenTX Radiomaster Boxer Joystick» → «Boxer»
        var t = n.replace(/^(OpenTX|EdgeTX)\s+/i, "").replace(/^RadioMaster\s+/i, "").replace(/\s+Joystick$/i, "")
        return t !== "" ? t : n
    }
    function moduleSim() { return bar.msup === "ok" && bar.mbackend !== "crsf-uart" }
    function moduleValue() {
        if (bar.msup !== "ok") return bar.msup === "no-link" ? qsTr("no data") : "—"
        var n = bar.profile !== "" ? bar.profile.replace("RadioMaster ", "") : qsTr("module")
        var s = bar.mstate === "READY" ? qsTr("ready") : bar.mstate === "FAULT" ? qsTr("fault")
              : bar.mstate === "DISCONNECTED" ? qsTr("absent") : bar.mstate === "DISABLED" ? qsTr("off")
              : bar.mstate === "INITIALIZING" ? qsTr("starting") : bar.mstate === "RECOVERING" ? qsTr("recovering")
              : bar.mstate === "DETECTED" ? qsTr("detected") : bar.mstate.toLowerCase()
        return n + (moduleSim() ? " SIM" : "") + " · " + s
    }
    function moduleColor() {
        if (bar.msup !== "ok") return bar.msup === "no-link" ? p.warning : p.muted
        return bar.mstate === "READY" ? p.ok : bar.mstate === "FAULT" ? p.fault : bar.mstate === "DISABLED" ? p.muted : p.info
    }

    Timer { interval: 250; running: true; repeat: true; onTriggered: win.bar = win.collect() }
    property date clockNow: new Date()                 // часы в строке состояния: обновление раз в 5 с
    Timer { interval: 5000; running: true; repeat: true; onTriggered: win.clockNow = new Date() }

    // Без сенсора: F1…F5 — разделы, Esc — закрыть (REQ-P-003)
    Repeater {
        model: 5
        delegate: Item {
            required property int index
            Shortcut { sequences: ["F" + (index + 1), "Ctrl+" + (index + 1)]; onActivated: win.open(win.sections[index].key) }
        }
    }
    Shortcut { sequence: "Escape"; onActivated: win.close() }

    component StatusItem: Row {
        property alias label: lab.text
        property alias value: val.text
        property color tone: win.p.ok
        property bool strong: false
        spacing: 6 * win.k
        height: win.barH
        Rectangle { width: 10 * win.k; height: width; radius: width / 2; color: tone; anchors.verticalCenter: parent.verticalCenter }
        Label { id: lab; color: win.p.muted; font.pixelSize: 12 * win.k; anchors.verticalCenter: parent.verticalCenter }
        Label {
            id: val
            color: strong ? tone : win.p.text
            font.pixelSize: 15 * win.k
            font.bold: strong
            anchors.verticalCenter: parent.verticalCenter
        }
    }

    // ---------- видео ----------
    Rectangle {
        id: field
        objectName: "videoField"
        x: 0; y: win.barH
        width: win.width
        height: win.height - win.barH - win.navH
        color: "#000000"
        VideoView {
            id: view
            objectName: "videoView"
            anchors.fill: parent
            visible: win.details === "" && !backend.videoReceiver.glActive    // под прежней страницей не рисуется
            receiver: backend.videoReceiver
            live: backend.videoSource.live
        }
        Loader {                                     // путь GL (ADR-0015): элемент создаётся заранее, до первого кадра
            id: glVideo
            anchors.fill: parent
            active: backend.videoReceiver.glAvailable
            visible: win.details === "" && backend.videoReceiver.glActive
            source: "GlVideoItem.qml"
        }
        Rectangle {                                  // источник без сигнала: прежний кадр GL не показывается
            anchors.fill: parent
            color: "#000000"
            visible: glVideo.visible && !backend.videoSource.live
        }
        Rectangle {
            objectName: "videoMessage"
            visible: !win.bar.live && win.details === ""
            anchors.centerIn: parent
            width: Math.min(parent.width * 0.6, msg.implicitWidth + 48 * win.k)
            height: msg.implicitHeight + 32 * win.k
            radius: 6 * win.k
            color: "#cc0b1218"
            ColumnLayout {
                id: msg
                anchors.centerIn: parent
                width: Math.min(implicitWidth, field.width * 0.6 - 48 * win.k)
                spacing: 6 * win.k
                Label {
                    objectName: "videoMessageTitle"
                    text: win.incompatible ? qsTr("INCOMPATIBLE REMOTE UNIT VERSION")
                          : win.linkDown ? win.linkTitle() : messages.title()
                    color: "#e8eaed"
                    font.pixelSize: 22 * win.k
                    font.bold: true
                    Layout.alignment: Qt.AlignHCenter
                }
                Label {
                    objectName: "videoMessageText"
                    text: win.incompatible
                          ? qsTr("Protocol: console %1, remote unit %2 (%3). The exchange does not start until the versions are compatible.")
                            .arg(win.bar.lproto).arg(win.bar.pproto || "?").arg(win.bar.pdetail)
                          : win.linkDown ? win.linkHint() : messages.reason()
                    color: "#b5bec6"
                    font.pixelSize: 15 * win.k
                    wrapMode: Text.WordWrap
                    horizontalAlignment: Text.AlignHCenter
                    Layout.maximumWidth: field.width * 0.6 - 48 * win.k
                    Layout.alignment: Qt.AlignHCenter
                }
            }
        }
        MiniVideoText { id: messages }
        Rectangle {                                  // REQ-UI-003: активная ошибка видна поверх видео; тап — диагностика
            objectName: "faultBanner"
            visible: win.activeErrors.length > 0 && win.details === ""
            anchors { left: parent.left; right: parent.right; top: parent.top; margins: 8 * win.k }
            anchors.rightMargin: (win.section !== "" ? panel.width + 18 * win.k : 8 * win.k)
            height: bannerCol.implicitHeight + 16 * win.k
            radius: 6 * win.k
            color: "#e6a3261f"
            border.color: win.p.fault
            z: 2
            ColumnLayout {
                id: bannerCol
                anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter; margins: 12 * win.k }
                spacing: 2 * win.k
                Label {
                    objectName: "faultBannerText"
                    text: win.activeErrors.length > 0 ? win.activeErrors[0].text
                          + (win.activeErrors.length > 1 ? "  " + qsTr("(+%1 more)").arg(win.activeErrors.length - 1) : "") : ""
                    color: "#ffffff"
                    font.pixelSize: 16 * win.k
                    font.bold: true
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                Label {
                    objectName: "faultBannerAction"
                    text: win.activeErrors.length > 0 ? win.activeErrors[0].action : ""
                    color: "#ffe9e6"
                    font.pixelSize: 14 * win.k
                    wrapMode: Text.WordWrap
                    maximumLineCount: 2
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
            }
            MouseArea { anchors.fill: parent; onClicked: win.open("diagnostics") }
        }
        MouseArea {                                  // тап по видео закрывает панель
            anchors.fill: parent
            enabled: win.section !== ""
            onClicked: win.close()
        }
        // Прежняя страница раздела на месте видео (строка состояния и меню остаются)
        Rectangle {
            objectName: "detailsOverlay"
            anchors.fill: parent
            visible: win.details !== ""
            color: win.p.bg
            RowLayout {
                id: detailsHead
                anchors { left: parent.left; right: parent.right; top: parent.top; margins: 8 * win.k }
                height: 40 * win.k
                Button {
                    objectName: "detailsBack"
                    text: qsTr("← Back")
                    font.pixelSize: 14 * win.k
                    Layout.preferredHeight: 40 * win.k
                    onClicked: win.details = ""
                }
                Item { Layout.fillWidth: true }
            }
            Loader {
                objectName: "detailsLoader"
                anchors { left: parent.left; right: parent.right; top: detailsHead.bottom; bottom: parent.bottom; margins: 12 * win.k }
                active: win.details !== ""
                source: win.details !== "" ? win.details + ".qml" : ""
            }
        }
    }

    // ---------- строка состояния ----------
    Rectangle {
        objectName: "statusBar"
        width: win.width
        height: win.barH
        color: win.p.panel
        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: win.p.border }
        Row {
            x: 12 * win.k
            spacing: 18 * win.k
            StatusItem {
                objectName: "statusControl"
                label: qsTr("CONTROL"); value: win.ctlText(); tone: win.ctlColor(); strong: true
            }
            StatusItem {
                objectName: "statusLink"
                label: qsTr("UNIT"); value: win.linkValue(); tone: win.linkTone()
            }
            StatusItem {
                objectName: "statusVideo"
                label: qsTr("VIDEO"); value: win.videoValue()
                tone: win.bar.live ? win.p.ok : win.linkDown ? win.p.fault : win.p.warning
            }
            StatusItem {
                objectName: "statusInput"
                label: qsTr("INPUT")
                value: win.bar.present ? win.shortDevice(win.bar.device) : qsTr("no device")
                tone: win.bar.present ? win.p.ok : win.p.warning
            }
            StatusItem {
                objectName: "statusModule"
                label: qsTr("MODULE"); value: win.moduleValue(); tone: win.moduleColor()
            }
        }
        Label {
            objectName: "statusLab"
            anchors { right: parent.right; rightMargin: 12 * win.k; verticalCenter: parent.verticalCenter }
            text: "LAB" + (win.bar.overall === "fault" || win.bar.overall === "warning" ? " · " + win.stateText(win.bar.overall) : "")
                  + "   " + Qt.formatTime(win.clockNow, "HH:mm")
            color: win.bar.overall === "fault" ? win.p.fault : win.bar.overall === "warning" ? win.p.warning : win.p.muted
            font.pixelSize: 14 * win.k
        }
    }

    // ---------- нижнее меню ----------
    Rectangle {
        objectName: "navBar"
        y: win.height - win.navH
        width: win.width
        height: win.navH
        color: win.p.panel
        Rectangle { width: parent.width; height: 1; color: win.p.border }
        Row {
            anchors.fill: parent
            Repeater {
                model: win.sections
                delegate: AbstractButton {
                    id: nb
                    required property var modelData
                    required property int index
                    objectName: "nav_" + modelData.key
                    readonly property bool current: win.section === modelData.key
                                                    || (win.details !== "" && win.detailsSection === modelData.key)
                    width: win.width / 5
                    height: win.navH
                    onClicked: win.open(modelData.key)
                    background: Rectangle {
                        color: nb.current ? Qt.darker(win.p.accent, 1.6) : nb.pressed ? win.p.border : "transparent"
                        Rectangle { visible: nb.current; width: parent.width; height: 2 * win.k; color: win.p.accent }
                        Rectangle { visible: nb.index > 0; width: 1; height: parent.height; color: win.p.border }
                    }
                    contentItem: Item {
                        Row {
                            anchors.centerIn: parent
                            spacing: 8 * win.k
                            MiniIcon {
                                name: nb.modelData.key
                                color: nb.current ? "#ffffff" : win.p.muted
                                width: 18 * win.k; height: width
                                anchors.verticalCenter: parent.verticalCenter
                            }
                            Label {
                                text: nb.modelData.label
                                color: nb.current ? "#ffffff" : win.p.text
                                font.pixelSize: 14 * win.k
                                font.bold: true
                                anchors.verticalCenter: parent.verticalCenter
                            }
                        }
                    }
                }
            }
        }
    }
    property string detailsSection: ""

    // ---------- панель раздела ----------
    Rectangle {
        id: panel
        objectName: "sidePanel"
        visible: win.section !== ""
        width: Math.min(400 * win.k, win.width * 0.32)
        x: win.width - width - 10 * win.k
        y: win.barH + 8 * win.k
        height: win.height - win.barH - win.navH - 16 * win.k
        radius: 6 * win.k
        color: win.p.panel
        border.color: win.p.border
        MouseArea { anchors.fill: parent }          // тап по панели не закрывает её
        RowLayout {
            id: head
            anchors { left: parent.left; right: parent.right; top: parent.top; margins: 12 * win.k }
            height: 40 * win.k
            Label {
                objectName: "panelTitle"
                text: win.section !== "" ? win.sections.find(function (s) { return s.key === win.section }).label : ""
                font.pixelSize: 18 * win.k
                font.bold: true
                Layout.fillWidth: true
            }
            Button {
                objectName: "panelClose"
                text: "×"
                font.pixelSize: 20 * win.k
                Layout.preferredWidth: 44 * win.k
                Layout.preferredHeight: 40 * win.k
                onClicked: win.close()
            }
        }
        Rectangle { anchors.top: head.bottom; anchors.topMargin: 6 * win.k; width: parent.width; height: 1; color: win.p.border }
        Flickable {
            id: body
            anchors { left: parent.left; right: parent.right; top: head.bottom; bottom: parent.bottom
                      topMargin: 14 * win.k; leftMargin: 14 * win.k; rightMargin: 14 * win.k; bottomMargin: 10 * win.k }
            contentHeight: win.currentPanel && win.currentPanel.item ? win.currentPanel.item.implicitHeight : 0
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            // Панели создаются один раз (в фоне после запуска или при первом открытии) и дальше только
            // показываются: открытие раздела — без создания объектов. Частые обновления внутри панелей (стики)
            // привязаны к видимости панели.
            Repeater {
                id: panels
                model: win.sections
                delegate: Loader {
                    required property var modelData
                    objectName: "panelLoader_" + modelData.key
                    width: body.width
                    visible: win.section === modelData.key
                    active: visible || win.preload
                    asynchronous: true
                    source: ({video: "MiniVideoPanel.qml", control: "MiniControlPanel.qml", module: "MiniModulePanel.qml",
                              diagnostics: "MiniDiagPanel.qml", system: "MiniSystemPanel.qml"})[modelData.key]
                    onLoaded: if (visible) win.openLoaded = true
                    onVisibleChanged: if (visible && status === Loader.Ready) win.openLoaded = true
                }
            }
        }
    }
}
