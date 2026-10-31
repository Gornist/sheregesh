<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Maintenance

Day-to-day work with the pair of boards: status, logs, updates.

## Status

- **Console, "Diagnostics" section:** link, video, console and remote unit services, events with codes and advice.
- **"System" section:** software version, OTA slots, image version, developer mode.
- **On a board:** `sheregesh-health` checks the services, storage, power and temperature. `sheregesh-info` shows the
  device passport: role, software version, image build, hardware revision and serial number.
- **From the owner's machine:** `python3 tools/deploy/shdeploy.py status pi1`.

The temperature in the diagnostics comes from the board's SoC sensor. Power is shown as "No data": there is no
power module on the stand.

## Logs and backup

- **"Logs" → "Export to USB"** (in Minimal, the export button in "Diagnostics") saves the log with a checksum;
  secrets in it are replaced with `[REDACTED]`.
- **"Settings" → "Back up to USB"** saves the configuration.

On the stand, the console's `/var/lib/sheregesh/usb` directory stands in for a USB drive.
Service logs on a board: `journalctl -u sheregesh-console`, `-u sheregesh-remote-net`, `-u sheregesh-video-tx`.

## Settings

Every apply creates a new configuration revision. The services check it, and on an error the previous revision is
restored automatically. Actions that change the device run only after a separate confirmation.

## Updating the application

Application code without system changes is updated with a signed release, without rewriting the card:

```sh
python3 tools/deploy/shdeploy.py deploy pi1        # then pi2
python3 tools/deploy/shdeploy.py rollback pi1      # return to the previous release
python3 tools/deploy/shdeploy.py factory pi1       # return to the code in the image
```

## Updating the system (OTA A/B)

The new image is written to the inactive slot. The working slot does not change meanwhile.

```sh
python3 tools/deploy/shdeploy.py ota-build --image IMAGE --build-report build-report.json --out PACKAGE
python3 tools/deploy/shdeploy.py ota-install pi1 --payload PACKAGE/*.shota --sha256 SHA
python3 tools/deploy/shdeploy.py ota-enable pi1
python3 tools/deploy/shdeploy.py ota-tryboot pi1   # boot the new slot once, as a trial
python3 tools/deploy/shdeploy.py ota-confirm pi1   # confirm after the checks
python3 tools/deploy/shdeploy.py ota-disable pi1
```

Update one board at a time, the console first. If the trial boot fails its checks, the board returns to the
previous slot by itself.
