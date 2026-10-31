# Лицензии зависимостей распространяемого ПО (REQ-DOC-001)

Редакция 2026-10-31, образ `c0c509c`. Распространяемое ПО проекта — пакет `sheregesh` (GPL-3.0-or-later); сторонний
код в него не включён. Ниже — прямые зависимости пакета (`DEPENDS` в `tools/image/build_deb.py`) и их основная
лицензия: раздел `Files: *` файла `/usr/share/doc/<пакет>/copyright` (DEP-5) на плате.

**Почему не поле SBOM.** SBOM образа (syft, SPDX 2.3, `*.sbom.zst` артефактов shbuild) в `licenseDeclared` объединяет
через «AND» все лицензии файла `copyright`, включая файлы упаковки Debian и вспомогательные скрипты (например,
`python3-jsonschema` — MIT, но в поле есть GPL-2.0-only). Механическая проверка по нему даёт ложные несовместимости,
поэтому решение — по основной лицензии пакета; SBOM — перечень пакетов.

| Пакеты | Основная лицензия | Как используются | С GPLv3 |
|---|---|---|---|
| python3 | PSF | интерпретатор | совместима |
| python3-yaml, python3-jsonschema | MIT | импорт | совместима |
| python3-pyside6.* (qtcore, qtgui, qtqml, qtquick, qtquickcontrols2) | GPL-3 с исключением или LGPL-3 | импорт | совместима |
| qml6-module-* , qt6-wayland | LGPL-3 или GPL-2 | загрузка Qt | совместима (LGPL-3) |
| gstreamer1.0-* (tools, plugins-base/good/bad, x, gl, qt6), gir1.2-gst* | LGPL-2+ | конвейеры, GObject Introspection | совместима |
| python3-gi | LGPL-2.1+ | импорт | совместима |
| openssl | Apache-2.0 | библиотека и команда | совместима с GPLv3 |
| fonts-dejavu-core | Bitstream Vera (свободная лицензия шрифта) | данные | не код |
| util-linux | GPL-2+ | отдельные команды | агрегация |
| nftables, chrony | GPL-2 (only) | отдельные программы, вызываются командой | агрегация, связывания нет |
| cage, seatd | Expat | отдельные программы | агрегация |

Итог: зависимостей с лицензией, несовместимой с GPLv3, среди связываемых библиотек — 0; программы под GPL-2 only
(nftables, chrony) работают отдельными процессами (агрегация). Повторять при изменении `DEPENDS` и при выпуске.
