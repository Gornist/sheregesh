// SPDX-FileCopyrightText: 2026 Sheregesh contributors
// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import org.freedesktop.gstreamer.Qt6GLVideoItem

// Кадр видео текстурой GPU от qml6glsink (ADR-0015, glvideo.py). Загружается только при доступном пути GL:
// без gstreamer1.0-qt6 этого модуля QML нет, и MainMinimal.qml показывает VideoView.
GstGLQt6VideoItem {
    objectName: "glVideoItem"
    forceAspectRatio: true
}
