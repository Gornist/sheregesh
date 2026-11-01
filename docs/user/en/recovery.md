<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Recovery

What to do when something goes wrong. Start with the first section that fits.

## No link between the console and the remote unit

1. Check the cable: the link comes back by itself, usually within a few seconds of reconnecting.
2. Look at the event code in "Diagnostics":
   - `LINK-AUTH-FAIL` — the certificate does not fit: issue certificates again (`labpki.py pair`);
   - `PEER-LOST` — the remote unit does not respond.
3. On the remote unit: `systemctl status sheregesh-remote-net` and `sheregesh-health`.

## No video

"No video" appears when the stream breaks, and the picture comes back by itself once the stream resumes. If it
does not:

- check the source on the remote unit — the "Video" section shows its state, for example "no device" or
  "no signal";
- check the `sheregesh-video-tx` service on the remote unit.

While there is no source, the remote unit does not keep recreating the stream: it waits for the device to be
connected.

## Failed update

- **Application:** `shdeploy.py rollback BOARD` returns the previous release, `factory` returns the code in the
  image.
- **System:** an unconfirmed slot rolls back by itself. If the checks on the trial boot did not pass, run
  `shdeploy.py ota-abort BOARD`: the board returns to the working slot. A confirmed slot can be changed only by the
  next update.
- **The board does not boot:** switch the power off and on. The firmware boots the confirmed slot.

## Wrong settings

An invalid revision never becomes active. To get a working configuration back, restore it from a backup:
"Settings" → "Restore from USB…". The backup is checked for integrity and compatibility and applied as a new
revision.

## Your own image

An image without the project's signature can be installed once, through the one-time developer mode: "Settings" →
"Allow one own image…" on the console. For the remote unit, use "Allow on remote unit…" next to it: the
confirmation is also given on the console. The mode lasts 15 minutes and turns off after the first installation. A
faulty own image rolls back in the same way as a regular one.

## Full recovery

Write the card again (see "Building"), then issue the certificates again. The data on the persistent partition is
lost: save a configuration backup before rewriting the card.
