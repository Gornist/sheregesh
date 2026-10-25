<!--
SPDX-FileCopyrightText: 2026 Sheregesh contributors
SPDX-License-Identifier: GPL-3.0-or-later
-->
# Предложение для tools/image/verify_image.py: лабораторная граница п. 7

Владелец `tools/image/**` — сессия S5; это готовый фрагмент для неё, сюда он не применён.

**Что проверяется.**
- Рабочий (строгий) вариант: в образе нет ни одного пути из `tools/lab-video/pi2/manifest.tsv` и нет правил polkit,
  где упоминается `sheregesh-lab-`.
- Dev-вариант:
  - все пути есть;
  - правило `51-…` разрешает ровно шесть юнитов и только `start`/`stop`/`restart`;
  - каждый юнит стоит на `ConditionPathExists=/etc/sheregesh/DEV-MODE` и не включён ни в один `*.wants`;
  - помощник root вызывается только из юнитов.

Модули `vivid`, `vhci-hcd`, `usbip-core` входят в пакет ядра в обоих вариантах. В рабочем образе их некому загрузить:
нет помощника, нет правила polkit, у `sheregesh-ui` нет root. Поэтому отсутствие модулей не проверяется.

```python
LAB_MANIFEST = ROOT / "tools/lab-video/pi2/manifest.tsv"
LAB_UNITS = {"sheregesh-lab-vivid.service", "sheregesh-lab-vivid-feed@cvbs.service",
             "sheregesh-lab-vivid-feed@hdmi.service", "sheregesh-lab-usbip@hdmi.service",
             "sheregesh-lab-usbip@cvbs.service", "sheregesh-lab-usbip@camera.service"}


def lab_paths():
    rows = [ln.split("\t") for ln in LAB_MANIFEST.read_text().splitlines() if ln and not ln.startswith("#")]
    return [dest for _src, dest, _mode in rows]


def labvideo_checks(rep, root, dev):
    """П. 7 (tools/lab-video/pi2): эмулированные источники видео — только в dev-образе."""
    paths = lab_paths()
    present = [p for p in paths if root.exists(p)]
    polkit = " ".join(root.cat(f"/etc/polkit-1/rules.d/{n}") for n in ls_names(root, "/etc/polkit-1/rules.d"))
    if not dev:
        rep.add("labvideo.absent", "строгий вариант: лабораторных источников видео (п. 7) нет — файлы и polkit",
                not present and "sheregesh-lab-" not in polkit, ", ".join(present) or f"0 из {len(paths)}")
        return
    rep.add("labvideo.files", "dev: файлы п. 7 на месте (manifest.tsv)", len(present) == len(paths),
            ", ".join(sorted(set(paths) - set(present))) or f"{len(paths)} файлов")
    rules = root.cat("/etc/polkit-1/rules.d/51-sheregesh-lab-video.rules")
    units = set(re.findall(r'"(sheregesh-lab-[a-z-]+(?:@[a-z]+)?\.service)"', rules))
    rep.add("labvideo.polkit", "dev: polkit п. 7 — только start/stop/restart шести юнитов у sheregesh-ui",
            units == LAB_UNITS and 'subject.user != "sheregesh-ui"' in rules
            and set(re.findall(r'verb == "([a-z-]+)"', rules)) == {"start", "stop", "restart"},
            ", ".join(sorted(units)))
    texts = {p: root.cat(p) for p in paths if p.startswith("/usr/lib/systemd/system/")}
    wants = " ".join(n for d in ls_names(root, "/etc/systemd/system") if d.endswith(".wants")
                     for n in root.ls(f"/etc/systemd/system/{d}"))
    rep.add("labvideo.units", "dev: юниты п. 7 только при DEV-MODE и не включены (запуск по polkit)",
            all("ConditionPathExists=/etc/sheregesh/DEV-MODE" in t for t in texts.values())
            and "sheregesh-lab-" not in wants, ", ".join(p for p, t in texts.items() if "DEV-MODE" not in t) or "ok")
```

Вызов — в `verify()` рядом с `deploy_checks(...)`: `labvideo_checks(rep, root, dev)`.

**Hook dev-слоя** (`os/layer/sheregesh-dev-access.yaml`, в духе существующих `install -D`):

```sh
lab="$IGconf_sheregesh_assetdir/../../tools/lab-video"        # путь к tools/lab-video от assetdir слоя — уточнить
grep -v '^#' "$lab/pi2/manifest.tsv" | while IFS="$(printf '\t')" read -r src dest mode; do
    install -D -m "$mode" "$lab/$src" "$1$dest"
done
```

Тест для `tests/tools/test_image_tools.py`: строгий вариант с подсунутым `51-sheregesh-lab-video.rules` → FAIL
`labvideo.absent`; dev-вариант без одного файла из манифеста → FAIL `labvideo.files`.
