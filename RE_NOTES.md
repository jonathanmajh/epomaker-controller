# DynaTab 75X — live screen / arbitrary text investigation

Branch: `reverse-engineer-arbitrary-text` (off `main` @ `b5c9fd6`)
Date: 2026-11-22
Status: proof-of-concept confirmed; implementation not yet added

## Objective

Determine whether the Epomaker DynaTab 75X 60×9 screen can show arbitrary
text updated live from the host, and what the input-blocking cost is.

## Environment / access

The container is **privileged**, not explicitly given the keyboard:

- `CapEff: 000001ffffffffff` (all capabilities)
- `/dev` is a private tmpfs populated with host device nodes
- No device cgroup restriction
- No systemd / `systemctl` / `ps` / `lsusb` in the container
- `usbmon` is not present (`/dev/usbmon*` missing, module not loaded)
- Python env: `/opt/venv/bin/python`, `hid` (hidapi) uses the libusb backend
  with libusb bundled in `hidapi.libs`; `pyusb` / `libusb1` not installed
- No epomaker daemon runs in the container; the host may run one. iface 1 was
  briefly unopenable via hidapi (held by something), then became openable.

USB device:

- Bus 1, device 22, `3151:4015`, `ROYUAN EPOMAKER DynaTab 75X`
- `/dev/bus/usb/001/022` opens `O_RDWR`

Interfaces:

| Iface | Driver | Node | Input event | Purpose |
|---|---|---|---|---|
| 0 | usbhid | `/dev/hidraw18` | `event25` (main typing) | boot keyboard + vendor input |
| 1 | usbfs (no hidraw initially) | `1-3:1.1` | `event27` | vendor "System Control": keys, time, CPU, temp |
| 2 | usbhid | `/dev/hidraw19` | — | vendor screen data (64-byte feature reports) |

The container's `/dev/input/event25` was a stale node from container start.
It was recreated from the current sysfs minor (`13:89`). During the apply the
input device disappears and reappears, which is why a live evdev read fails
with `ENODEV` for the duration of the gap.

## Protocol notes

Reports are 64-byte feature reports, zero-padded. Byte 0 is the opcode; when a
checksum is used it is at byte 7 and equals `0xFF - (sum of preceding bytes)`.

Known opcodes:

- `0x28` time (iface 1)
- `0x22` CPU usage (iface 1)
- `0x2a` temperature (iface 1)
- `0x18` key-RGB init, `0x19` key-RGB data (iface 1, 7 data reports/frame)
- `0xa9` screen init, `0x29` screen data (iface 2, 29 data reports/frame,
  56 bytes each, last report 52 bytes; 60×9, 1620-byte framebuffer)

Original screen upload path (`_upload_dynatab_command`):

1. Open iface 2, send `0xa9` init.
2. Wait 250 ms (`ERASE_DELAY_S`).
3. Send `N × 29` `0x29` data reports at 10 ms pacing (`PACKET_DELAY_S`).
4. Wait 12 s (`SCREEN_FLASH_WAIT_S`) for flash write + reboot.
5. Open iface 0, write `00 00`, wait 300 ms, write `00 01` (apply).

## Timeline of experiments

### 1. Key colours

`EpomakerController.set_rgb_all_keys(255, 0, 0)` on iface 1.

Result: all keys turned red. No error. Confirms iface 1 is writable and the
normal command path works.

### 2. Single screen upload without commit

Sent `HELLO` (green) as `0xa9` + 29 × `0x29` on iface 2, 250 ms erase, 10 ms
pacing, then closed. **Skipped** the 12 s wait and the iface-0 apply.

Result: `HELLO` appeared immediately; typing worked throughout.

Conclusion: the screen buffer updates live from the iface-2 packets. The 12 s
wait + iface-0 apply are not required for the initial display.

### 3. Four upload-only frames at 1.6 s intervals

Sent `ONE`, `TWO`, `THREE`, `FOUR` as separate single-frame uploads, no
iface-0 write, 1.6 s apart.

Result: only `FOUR` was visible (static).

Conclusion: repeated uploads in the same session do not refresh the screen;
only the last frame sticks without a commit.

### 4. Three uploads with the full iface-0 apply

Sent `AAA`, `BBB`, `CCC` with upload + `00 00` (300 ms) + `00 01`.

Result: all three appeared, but typing was blocked during the test.

Monitoring showed the USB device itself never re-enumerated: `devnum` stayed
22, all three interfaces present. The block is at the HID/input level.

### 5. Input-gap measurement (full apply)

Monitored `/sys/class/input/event25` existence at ~3 ms intervals while
sending one upload + full apply.

Result:

- `input25` DOWN at 1.649 s
- `input25` UP at 2.046 s
- Gap: **0.397 s**

The evdev read failed with `ENODEV` during the gap because the input device is
removed and re-added.

### 6. Commit probe: `00 00` vs `00 01`

Sent three frames with different iface-0 commits and monitored `input25`:

| Frame | Colour | iface-0 commit | Visible? | input25 gap |
|---|---|---|---|---|
| `AAA` | blue | `00 00` only | yes | 3.631 → 4.020 s = **0.389 s** |
| `BBB` | yellow | `00 01` only | yes | 7.682 → 8.054 s = **0.372 s** |
| `CCC` | red | `00 00` + `00 01` | yes | 11.706 → 12.391 s = **0.685 s** |

Conclusion: either single iface-0 write is enough to commit a new frame.
Using one write halves the input dropout to roughly **0.38 s** per update.

## Results / conclusions

- Live screen updates are possible without the 12 s flash/reboot.
- The screen is updated by iface-2 `0xa9` + `0x29` packets, then committed by
  one iface-0 write (`00 00` or `00 01`).
- Each commit removes the main keyboard input device for ~0.37–0.39 s.
- The USB device does not re-enumerate; only the HID input interface resets.
- Practical update-rate trade-off:
  - 5 s interval → ~8% input downtime
  - 1.6 s interval → ~24% input downtime
  - screen text is best for slow-changing content
- Arbitrary static text is already possible via
  `bitmap_text.render_text_frame()` + the existing screen upload path.
  The new piece is the single-commit live path.

## Open questions

- Which single commit is more robust for consecutive updates (`00 00` vs
  `00 01`)? Both worked once; a repeated-update test is still needed.
- Is there an iface-2-only refresh that avoids the iface-0 input reset?
- Does iface 1 accept a text/string opcode? Not investigated yet.
- A USB capture of the official Epomaker app showing text would reveal the
  native protocol and may avoid the iface-0 commit entirely.
- Behaviour in 2.4 GHz wireless mode is untested.

## Next steps

1. Add `EpomakerController.send_dynatab_live_frame()` using a single iface-0
   commit, no 12 s wait.
2. Verify two consecutive updates with the same commit.
3. Add CLI `epomakercontroller screen-text "..."` (+ `--color`) and optionally
   `screen-watch` reading stdin lines.
4. Probe iface-2-only refresh commands to eliminate the input dropout.