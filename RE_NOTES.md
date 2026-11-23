# DynaTab 75X — live screen / arbitrary text investigation

Branch: `reverse-engineer-arbitrary-text` (off `main` @ `b5c9fd6`)
Date: 2026-11-22
Status: live text confirmed and implemented (iface-1 path)

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

### 7. App-path live updates (iface 1)

With the host Epomaker service stopped (it was holding iface 1), sent five
consecutive frames `LIVE1`..`LIVE5` over iface 1 using the app's exact
`0xA9` + `0x29` layout, with no iface-0 write:

- All five frames appeared in order.
- Holding Enter produced no interruptions.
- `input25` never disappeared; the USB device never re-enumerated.
- Input gaps: **none**.

This is the path to use for live text; the legacy iface-2 + iface-0 path is
only needed for `flash=True`.

## Results / conclusions

- **Live screen updates work with no input dropout** via the app's iface-1
  path (`0xA9` prep + `0x29` data, no iface-0 write). Verified 2026-11-23:
  five consecutive frames (`LIVE1`..`LIVE5`) all appeared, and holding Enter
  had no interruptions. `input25` never went away and the USB device never
  re-enumerated.
- The legacy iface-2 + iface-0 path (12 s flash wait + `00 00`/`00 01`
  apply) does work, but each commit removes the main input device for
  ~0.37–0.39 s. It is now only used with `flash=True`.
- The app's own screen upload path is iface 1: `getTFTLCDDataRGBImg`
  (`0xA9`) then `setTFTLCDDataRGBImg` (`0x29`, 8-byte header + 56 bytes,
  ~5–10 ms apart), with no iface-0 write. The main MCU drives the screen.
- Arbitrary text is rendered host-side (`bitmap_text.render_text_frame`) and
  sent as a 60×9 RGB frame; no firmware text widget is needed.

## Official firmware / app investigation (2026-11-23)

The Epomaker app is **EPOMAKER Driver v4** (Electron, v3.2.31). I downloaded the
official Windows package
`https://orders.epomaker.com/software/EPOMAKER_v4_setup_3.2.31_WIN2026109.zip`,
extracted the NSIS installer and `app-32.7z`, and read the update code. It was
not run with USB (Windows-only; container is Linux), but the download/flash
logic is fully visible in `resources/app/dist/js/index.e985458e.js`.

### Firmware download API

`POST https://api2.rongyuan.tech:3816/api/v2/get_fw_version`
body `{"dev_id":1723}` (DynaTab 75X model id; VID 0x3151 / PID 0x4015):

```json
{"code":0,"data":{
  "dev_id":1723,
  "version_str":"v102_oledv103",
  "lowest_app_version_str":"1.2.65",
  "file_path":"fw_upgrade_file/1723_v102_oledv103"
}}
```

File: `GET https://api2.rongyuan.tech:3816/download/fw_upgrade_file/1723_v102_oledv103`
→ ZIP with:
- `firmwareFile.bin` 282,912 B (main MCU)
- `firmwareOledFile.bin` 211,728 B (screen MCU)

Related: `get_iot_version` with `{"sys_type":"win"}` returns `v218` /
`iot_upgrade_file/win/iot_v218`; the app bundles IOT 222.

### Firmware format

- Raw ARM Thumb, **not encrypted** (entropy 4.47 / 3.45, 50% / 57% zero bytes).
- Strings confirm the **YiChip YC3x SDK**: `yc_gpio.c`, `yc_uart.c`,
  `yc_timer.c`, `yc_systick.c`, `yc_sysctrl.c`, `GPIO_Init`, `UART_Init`, etc.
  The device name is `yc3121_kf850_uk_screen`, so the main MCU is a YC3121.
- Main image contains `EPOMAKER DynaTab 75X` / `DynaTab 75X 5.0`.
- Layout: 20 KB bootloader at `0x00000`, padding to `0x10000`, app at `0x10000`.
  `upgrade_usb(file, 20480, 10)` skips the first 20,480 bytes and sends the rest.

### Flashing protocol (from the app)

1. Bootloader entry: 64-byte HID **feature report** on the main config interface
   (iface 1): `7F 55 AA 55 AA 00 00 <chk>`, `<chk> = 0xFF - (sum[0:7] & 0xFF)`.
   The device re-enumerates as bootloader **PID 0x4016** (usage page 0xFF01,
   usage 1, interface 0).
2. Start: `BA C0 <packets:2 LE> <bytes:3 LE> <chk>` (checksum at byte 7).
   `packets = ceil((len(file) - 20480) / 64)`.
3. Data: the app image (`file[20480:]`) in 64-byte feature reports, 10 ms apart,
   no checksum (`ei.None`).
4. Verify: `BA C2 <packets:2 LE> <sum32:4 LE>`; response byte 4 must be `0x55`.
   `sum32` is a plain 32-bit sum of all data bytes (`Xo = reduce((a,b)=>a+b,0)`).
5. The bootloader flashes, verifies, and reboots.

The desktop app proxies HID through `iot_manager_rs.exe`, a gRPC-Web service on
127.0.0.1:3814 (`/hid.HidService/Write`, `SendFeatureReport`,
`ListenToReports`, `GetVersion`, ...). The JS talks to it with
`@protobuf-ts/grpcweb-transport`.

### Implications

- The official firmware is publicly downloadable; no device dump is needed to
  obtain it.
- The image is unencrypted and can be disassembled/patched (Capstone, Ghidra).
- Custom flashing is possible by reimplementing the bootloader protocol above,
  but the checksum must be fixed and a bad image can brick the keyboard.
- The 0.4 s input reset is in the main app; patching it out would remove the
  live-update typing freeze, but that is a separate firmware-modification
  project.

## Implementation (2026-11-23)

- `EpomakerController._send_dynatab_live_command()` sends the `0xA9` prep and
  `0x29` data reports over iface 1 under `self.lock`; no iface-0 write and no
  reboot. `send_dynatab_frames(..., flash=False)` is the new default;
  `flash=True` keeps the legacy iface-2 + iface-0 path.
- New CLI: `epomakercontroller screen-text "TEXT" [--color r,g,b] [--flash]`
  and `screen-watch` (stdin lines). `upload-image` is live by default with an
  opt-in `--flash`.
- Daemon `upload_screen` / `upload_image` accept `flash` (default false).
  Live uploads keep the HID handle open and do not close/reconnect.
- Tests: 103 pass, including iface-1 report shape, no iface-0 write,
  `flash=True` routing, CLI text/watch, and daemon progress stages.

## Open questions

- Does the firmware have a native text/string widget? No evidence; the screen
  path takes RGB frames, so text is rendered host-side. A native widget would
  require a firmware patch.
- Behaviour in 2.4 GHz wireless mode is untested.
- Does the bootloader expose a read/verify command to dump the device flash and
  compare it with the official image (per-device calibration data)?
- Can the main firmware be patched to add a native text widget (or change the
  commit behaviour), and will the bootloader accept a modified image with a
  recomputed `sum32`?

## Next steps

1. ~~Add `EpomakerController.send_dynatab_live_frame()`~~ Done:
   `_send_dynatab_live_command()` + `send_dynatab_frames(flash=False)`.
2. ~~Verify two consecutive updates~~ Done: `LIVE1`..`LIVE5` all appeared,
   zero input gaps.
3. ~~Add CLI `screen-text` / `screen-watch`~~ Done, with `--color` and
   `--flash`.
4. ~~Probe iface-2-only refresh~~ Superseded by the iface-1 live path.
5. Optionally implement a read-only bootloader probe (device ID/version) and a
   Python flasher for custom firmware using the protocol above.