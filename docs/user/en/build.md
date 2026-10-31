<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Building

How to build the Sheregesh software and system image and prepare a pair of boards: the console and the remote unit
on Raspberry Pi 4B. Cases, power and display are not selected yet; hardware assembly will be described once the
hardware is chosen.

## What you need

- Two Raspberry Pi 4B boards, two microSD cards of 8 GB or more, an Ethernet cable between the boards (or a shared
  network).
- A video source for the remote unit: a USB HDMI capture device (MacroSilicon MS2130 is tested), a UVC camera, an
  analogue input or an RTSP stream.
- A developer machine with Git and Python 3.11+, and a Linux x86-64 build machine with Docker (the shbuild service).

## Without hardware: emulators

```sh
tools/dev/sheregesh-emu --demo          # console and remote unit as separate processes, fault scenario
tools/dev/sheregesh-emu --test          # all automated tests without a display
```

## System image

Images are built only by the shbuild service on the build machine. This way every image passes the tests, the
content checks and a boot test.

```sh
python3 tools/build/shbuild.py install          # once: install the service on the build machine
python3 tools/build/shbuild.py submit           # submit the current commit, prints a job id
python3 tools/build/shbuild.py wait JOB         # wait for the result: PASSED or the reason for the failure
python3 tools/build/shbuild.py fetch JOB DIR
```

Only a PASSED image may be installed: the flashing and update tools reject any other.

## Writing the card

```sh
python3 tools/image/flash_sd.py DIR/sheregesh-lab-dev-pi4.img.zst              # list suitable cards
python3 tools/image/flash_sd.py DIR/sheregesh-lab-dev-pi4.img.zst --disk diskN # write and verify
```

The script writes only to an external disk and checks what it wrote against the checksum. It will not overwrite a
card that already holds a system unless you explicitly confirm.

## First setup of the pair

1. Insert the cards and power on the boards. System names are assigned by role: `pi1` is the console, `pi2` is the
   remote unit.
2. From the owner's machine, create the test CA and issue certificates to the boards:

   ```sh
   python3 tools/lab-pki/labpki.py init
   python3 tools/lab-pki/labpki.py pair --console CONSOLE_ADDRESS --remote REMOTE_ADDRESS
   ```

   The device key is created on the board and never leaves it: only the signing request reaches the owner's
   machine.
3. The console finds the remote unit by itself, sets up the secure link (TLS 1.3 with mutual authentication) and
   sets the unit's time. To check, open the console's "Diagnostics" section or run `sheregesh-health` on a board.
