<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Building

How to build the Sheregesh image from scratch and start a pair of boards: the console and the remote unit on
Raspberry Pi 4B. Cases, power and display are not selected yet; hardware assembly will be described once the
hardware is chosen. Terms are explained in «Questions and terms».

## What you need

- **Boards:** two Raspberry Pi 4B, two microSD cards of 8 GB or more, an Ethernet cable between the boards or a shared
  network with DHCP.
- **Video for the remote unit:** a USB HDMI capture device (MacroSilicon MS2130 is tested), a UVC camera, an analogue
  input or an RTSP stream. Without a video source a system update is not confirmed — see «Maintenance».
- **Developer machine:** Git, Python 3.11+, OpenSSH. Writing a card with the script needs macOS; on Linux — by hand
  (below).
- **Build machine:** Linux x86-64 with Docker and qemu-user-static (building for aarch64), at least 40 GB of free disk
  and 6 GB of memory, SSH login by key. It may be the same machine as the developer machine.

## Without hardware: emulators

```sh
tools/dev/sheregesh-emu --demo          # console and remote unit as separate processes, fault scenario
tools/dev/sheregesh-emu --test          # all automated tests without a display
```

## Keys

Two SSH keys are needed. Private keys stay on the developer machine; only public keys go into the image.

```sh
ssh-keygen -t ed25519                                               # login to the boards (if you have no key yet)
ssh-keygen -t ed25519 -f ~/.ssh/sheregesh-deploy -C sheregesh-deploy  # signing releases and updates
```

The CA key for board certificates is created later (`labpki.py init`) in `~/.sheregesh-lab-ca`. Keep a copy of this
directory: without it a new board can only get a certificate from a new CA with both boards paired again.

## System image

Images are built only by the shbuild service on the build machine: every image passes the tests, a content check and
a boot test in an emulator, including a first boot without network.

```sh
export SHBUILD_HOST=builder SHBUILD_BULK_HOST=builder   # build machine name in ~/.ssh/config (default: the Raspberry Pi 5 build host, shbuild@pi5.local)
python3 tools/build/shbuild.py install          # once: the service, the builder and your public keys
python3 tools/build/shbuild.py submit           # submit the current commit; prints the job id
python3 tools/build/shbuild.py wait JOB         # wait for the result: PASSED or the reason of failure
python3 tools/build/shbuild.py fetch JOB DIR
```

`install` takes the public keys from `~/.ssh/id_ed25519.pub` and `~/.ssh/sheregesh-deploy.pub` (others — `--dev-key`,
`--deploy-key`). Only an image with the PASSED result can be installed: the writing and update tools refuse others.
The lab variant with SSH login (DEV image) is built — other variants are not supported yet.

## Writing a card

On macOS:

```sh
python3 tools/image/flash_sd.py DIR/sheregesh-lab-dev-pi4.img.zst              # list suitable cards
python3 tools/image/flash_sd.py DIR/sheregesh-lab-dev-pi4.img.zst --disk diskN # write and verify
```

The script writes only to an external disk and verifies the written data against the checksum. It will not overwrite
a card with an installed system without explicit consent.

On Linux, unpack the image and check it against the build report, then write it with Raspberry Pi Imager
(«Use custom») or `dd`, double-checking the card's device name:

```sh
zstd -d DIR/sheregesh-lab-dev-pi4.img.zst -o sheregesh.img
sha256sum sheregesh.img            # must match image_sha256 in DIR/build-report.json
```

## First setup of the pair

1. Write the image to both cards, insert them and power on the boards. Before pairing both have the same name
   `sheregesh-dev`: power them on one at a time and find each address in the router's DHCP list.
2. Check the login: `ssh sheregesh-ui@ADDRESS`. A new board is accepted without editing `known_hosts`. If the card was
   rewritten and the address stayed the same, SSH reports a changed host key: `ssh-keygen -R ADDRESS`.
3. Create the CA and pair the boards:

   ```sh
   python3 tools/lab-pki/labpki.py init
   python3 tools/lab-pki/labpki.py pair --console CONSOLE_ADDRESS --remote REMOTE_ADDRESS
   ```

   `pair` first checks both boards, then issues the certificates, links the boards, records them in the board
   registry (`~/.config/sheregesh/boards.json`, names pi1 and pi2) and restarts the services. The device key is
   created on the board and never leaves it. After a reboot the boards are named by role: `pi1` — console,
   `pi2` — remote unit.
4. The console finds the remote unit by itself and sets up a protected link (TLS 1.3 with mutual authentication).
   Check it in the console's «Diagnostics» section, with `sheregesh-health` on a board or from the developer machine:

   ```sh
   python3 tools/stand/boards.py list
   python3 tools/deploy/shdeploy.py status pi1
   python3 tools/probe/smoke.py --out DIR      # automated items of the pair check
   ```

Replacing one board (new card, new unit) is the same `pair` with the current addresses of both boards. Your own board
names — `--names console,unit`; adding a board to the registry without pairing — `python3 tools/stand/boards.py add
NAME --host ADDRESS --role console|remote`.
