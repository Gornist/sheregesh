<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Questions and terms

## Work and data

**Can it work without internet?** Yes. The console and the remote unit work in a local network without outside
access; the console sets the time for the remote unit. The build's boot test checks the first boot of the image with
no route and no DNS.

**Do I need a second Raspberry Pi to try it?** No: `tools/dev/sheregesh-emu --demo` runs the console and the remote
unit on a computer with synthetic video and a module emulator.

**Where is the manual on the device?** In the image: `/usr/share/sheregesh/manual/{ru,en}`, with the software version
on every page.

**Where does «USB» write?** For now to the directory `/var/lib/sheregesh/usb` on the console; fetch the files over
SSH.

**What does «SIM» next to the module mean?** The module runs through the lab emulator, not through the UART of a real
transmitter.

## Boards and updates

**Why is an update not confirmed?** The confirmation checks services, link and video. Without a video source on the
remote unit the video check fails — connect a source or turn the check off (see «Maintenance»).

**How do I replace a broken remote unit?** Write a card, power on the new board and run `labpki.py pair` with the
addresses of the console and the new unit: both get new certificates and links to each other.

## Terms

- **Console** — the board with the screen and the radio controller (pi1). **Remote unit** — the board with the video
  source and the link module (pi2).
- **Pairing** — issuing certificates to both boards and linking them to each other (`labpki.py pair`).
- **Stand CA** — the key that signs the board certificates (`~/.sheregesh-lab-ca`).
- **Slot A/B** — two system partitions. An update is written to the inactive slot; the working one does not change
  until confirmation.
- **Trial boot (tryboot)** — a one-time boot of the new slot. Without confirmation the next boot returns to the working
  slot.
- **Confirmation** — the check of services, link and video on the trial boot; after it the new slot becomes the
  working one.
- **Application release (DEV release)** — a signed package of application code over the image, without rewriting the
  card.
- **Configuration revision** — an immutable set of settings. Each apply creates a new revision; the previous one stays
  for rollback.
- **persistent** — the card partition shared by both slots: settings, keys, logs.
- **FAILSAFE** — the remote unit's safe mode when commands from the console are lost.
- **Board registry** — the file `~/.config/sheregesh/boards.json` on the developer machine: name, serial number, MAC
  and role of every board.
