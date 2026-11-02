<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Maintenance

Everyday work with the pair of boards: state, logs, updates, certificates.

## State

- **Console status bar:** control, link to the remote unit and its latency, video, input device, module, clock.
  An active fault is shown as a bar over the video; tapping it opens «Diagnostics».
- **«Diagnostics» section:** link, video, console and remote unit services, events with recommendations.
- **«System» section:** software version, update state, system image, developer mode.
- **On a board:** `sheregesh-health` checks services, storage, power and temperature. `sheregesh-info` shows the
  passport: role, software version, image build, hardware revision and serial number.
- **From the developer machine:** `python3 tools/deploy/shdeploy.py status pi1`.

The temperature in diagnostics is the board's SoC sensor. Power is shown as «no data»: the stand has no power module.

## Logs and backup

- **«Diagnostics» → «Export logs»** saves the console log with a checksum; secrets in it are replaced with
  `[REDACTED]`. **«Export remote unit log»** there saves the remote unit's service log for the current boot.
- **«Settings» → «Back up to USB»** saves the configuration. Link keys and certificates are not part of the backup.

An external drive is not mounted yet: «USB» is the directory `/var/lib/sheregesh/usb` on the console. Fetch the files
over SSH:

```sh
scp 'sheregesh-ui@CONSOLE_ADDRESS:/var/lib/sheregesh/usb/*' .
```

Service logs on a board: `journalctl -u sheregesh-console`, `-u sheregesh-remote-net`, `-u sheregesh-video-tx`.
Remote unit maintenance from the console (on the console board, starting with a new image):
`sheregesh-remotectl log-export`, `sheregesh-remotectl shutdown --confirm`.

## Shutting down the remote unit

The remote unit has no power button. It is shut down from the console: «System» → «Shut down the remote unit…» and
a confirmation («Cancel» is selected by default). The unit stops its services and powers off; the console shows
«Remote unit switched off» over the video. To turn the unit on again, switch its power off and on.

## Settings

Each apply creates a new configuration revision. The services check it, and on an error the previous revision is
restored automatically. Actions that change the device run only after a separate confirmation. Input fields are filled
with the on-screen keyboard — it appears when you tap a field.

## Application update

Application code without system changes is updated with a signed release, without rewriting the card:

```sh
python3 tools/deploy/shdeploy.py deploy pi1        # then pi2
python3 tools/deploy/shdeploy.py rollback pi1      # back to the previous release
python3 tools/deploy/shdeploy.py factory pi1       # back to the image code
```

A release after which a service fails is rolled back automatically.

## System update (OTA A/B)

A new image is written to the inactive slot; the working slot does not change. The update package is built from an
image with the PASSED result, then one command takes the board through all steps:

```sh
python3 tools/deploy/shdeploy.py ota-build --image IMAGE --build-report build-report.json --out PACKAGE
python3 tools/deploy/shdeploy.py ota-update pi1 --payload PACKAGE/*.shota --sha256 SHA
```

`ota-update` installs the package, allows switching slots, does a trial boot, waits for the board, checks it and
confirms. If the checks fail, the board returns to the working slot. If the board does not come back after the trial
boot, switch its power off and on: the working slot boots. Separate steps — `ota-install`, `ota-enable`,
`ota-tryboot`, `ota-confirm`, `ota-abort`, `ota-disable`, `ota-status`.

Update the boards one at a time, the console first. The confirmation checks services, link and video. On a stand
without a video source the video check can be turned off (root on the board):

```sh
echo '{"require_video": false}' > /persistent/sheregesh-ota/settings.json
```

## Certificates

Board certificates are valid for 365 days, the CA for 730 days (`labpki.py status --host ADDRESS`). Renewal is pairing
again: `labpki.py pair` with the current board addresses. When the CA expires, create a new one (`labpki.py init` in an
empty `~/.sheregesh-lab-ca`) and pair the boards again. Keep a copy of `~/.sheregesh-lab-ca` off the developer machine.
