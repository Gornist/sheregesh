<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Recovery

What to do when something goes wrong. Start with the first matching item.

## No link between the console and the remote unit

The console shows the cause over the video:

- **«Waiting for the remote unit»** — there was no link since the console started. Check the remote unit power and the
  Ethernet cable; the console connects by itself.
- **«No link to the remote unit»** — the link was there and is lost. Check the cable and the unit power; the link
  usually comes back by itself within seconds.
- **«Remote unit not authorized»** — the console and the unit do not accept each other's certificates (expired,
  board replaced, new CA). The link will not recover by itself: pair the boards again (`labpki.py pair`, see
  «Building»).
- **«Remote unit switched off»** — the unit was shut down from the console. Switch its power off and on.
- **«Incompatible remote unit version»** — the console and the unit have different protocol versions. Update the
  older board.

On the remote unit: `systemctl status sheregesh-remote-net` and `sheregesh-health`.

## No video

«No video» appears when the stream breaks, and the picture returns by itself when the stream resumes. If it does not:

- check the source on the remote unit — the «Video» section shows its state, for example «no device» or
  «no signal»;
- check the `sheregesh-video-tx` service on the remote unit.

While there is no source, the unit does not keep recreating the stream: it waits for the device to be connected.

## The console does not start

If the console screen shows no interface or it keeps restarting:

1. Log in to the console over SSH and check the state: `sheregesh-health`, `systemctl status sheregesh-console`.
2. The cause is in the boot journal: `journalctl -b -u sheregesh-console` and
   `journalctl -b | grep -i -E "error|traceback"` (the interface writes to the session journal).
3. If the failure started after an application update — `shdeploy.py rollback pi1` or `factory pi1`.
4. Restart the interface — `systemctl restart sheregesh-console`; if that does not help — reboot the board.

## SSH does not let you in

- **«REMOTE HOST IDENTIFICATION HAS CHANGED»** — the card was rewritten, the board has a new host key:
  `ssh-keygen -R ADDRESS`, then log in again.
- **«Permission denied (publickey)»** — a different public key is built into the image. Rebuild the image with your
  key (`shbuild.py install --dev-key …`, see «Building»).
- **The address does not answer** — the address changed via DHCP: `python3 tools/stand/boards.py find pi1` finds the
  board by MAC in the local network (`SHEREGESH_LAN`, default 192.168.1.0/24).

## Failed update

- **Application:** `shdeploy.py rollback BOARD` returns the previous release, `factory` — the image code.
- **System:** an unconfirmed slot rolls back by itself. If the checks on the trial boot fail, run
  `shdeploy.py ota-abort BOARD`: the board returns to the working slot. A confirmed slot can only be changed by the next
  update.
- **The board does not boot:** switch the power off and on. The firmware boots the confirmed slot.

## Wrong settings

An invalid revision never becomes active. To get a working configuration back, restore it from a backup:
«Settings» → «Restore from USB…». The backup is checked for integrity and compatibility and applied as a new revision.

## Own image

An image without the project signature can be installed once, through the one-time developer mode: «Settings» →
«Allow one own image…» on the console. For the remote unit — the «Allow on remote unit…» button there:
it is also confirmed on the console. The mode lasts 15 minutes and turns off after the first installation. A faulty own
image rolls back like a regular one.

## Full recovery

Write the card again (see «Building»), then pair the boards again. Data on the persistent partition is lost: before
rewriting, save a configuration backup and fetch it from the console over SSH.
