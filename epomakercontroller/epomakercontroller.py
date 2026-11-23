"""EpomakerController module.

This module contains the EpomakerController class, which represents a controller
for an Epomaker USB HID device.
"""

import dataclasses
import logging
from datetime import datetime
from json import dumps
import os
import time
from typing import Any, Callable, Optional
import hid  # type: ignore[import-not-found]
import signal
import subprocess
from types import FrameType, TracebackType
import re
import threading

from .utils.sensors import get_cpu_usage, get_device_temp
from .utils.time_helper import TimeHelper
from .utils.keyboard_keys import KeyboardKeys

from .commands import (
    EpomakerCommand,
    EpomakerImageCommand,
    EpomakerRemapKeysCommand,
    EpomakerTimeCommand,
    EpomakerTempCommand,
    EpomakerCpuCommand,
    EpomakerKeyRGBCommand,
    EpomakerProfileCommand,
)
from .commands.data.constants import (
    BUFF_LENGTH,
    CAPABILITY_DYNATAB_SCREEN,
    CAPABILITY_RT100_SCREEN,
    ERASE_DELAY_S,
    PACKET_DELAY_S,
    Profile,
    SCREEN_FLASH_WAIT_S,
)
from .commands.EpomakerDynaTabScreenCommand import EpomakerDynaTabScreenCommand
from .configs.configs import Config, ConfigType
from .exceptions import (
    CommandNotPreparedError,
    DeviceCommunicationError,
    DeviceNotOpenError,
    ProtocolError,
)


log = logging.getLogger(__name__)

# While waiting for a missing keyboard, log a warning on this many attempts
# (after the first few) and keep the rest at DEBUG. At the 30s backoff cap
# this is one warning every 10 minutes.
_RECONNECT_LOG_EVERY = 20


class EpomakerController:
    """EpomakerController class represents a controller for an Epomaker USB HID device.

    Attributes:
        vendor_id (int): The vendor ID of the USB HID device.
        product_id (int): The product ID of the USB HID device.
        device (hid.device): The HID device object.
        dry_run (bool): Whether to run in dry run mode.

    Methods:
        open_device: Opens the USB HID device and prints device information.
        send_basic_command: Sends a command to the HID device.
        close_device: Closes the USB HID device.
        format_current_time: Gets the current time and formats it into the required
            byte string format.
    """

    def __init__(
        self,
        config_main: Config,
        dry_run: bool = False,
        *,
        install_signal_handlers: bool = False,
    ) -> None:
        """Initializes the EpomakerController object.

        Args:
            config_main (Config): Main configuration (VID/PID, layout, capabilities).
            dry_run (bool): Whether to run in dry run mode (default: False).
            install_signal_handlers: Install process-wide SIGINT/SIGTERM handlers
                that close the device. Prefer True only for long-running CLI tools
                (e.g. the daemon); leave False when embedding as a library.
        """

        self.config_layout = Config(
            ConfigType.CONF_LAYOUT, config_main.data["CONF_LAYOUT_PATH"]  # type: ignore
        )
        self.config_keymap = Config(
            ConfigType.CONF_KEYMAP, config_main.data["CONF_KEYMAP_PATH"]  # type: ignore
        )

        self.vendor_id = config_main["VENDOR_ID"]
        self.use_wireless = config_main["USE_WIRELESS"]
        self.product_ids: list[int] = (
            config_main["PRODUCT_IDS_WIRED"]
            if not self.use_wireless
            else config_main["PRODUCT_IDS_24G"]
        )
        self.device_description = config_main["DEVICE_DESCRIPTION_REGEX"]
        caps = config_main["CAPABILITIES"] or []
        self.capabilities: set[str] = set(caps)
        self.device = hid.device()
        # True only after a successful open. ``self.device`` is a non-None
        # hid.device object even before it is opened, so it cannot be used as
        # the "connected" test on its own.
        self._opened = False
        # RLock so the signal handler (same thread) can close the device even if
        # a send is interrupted mid-flight; cross-thread exclusion is preserved.
        self.lock = threading.RLock()
        self.dry_run = dry_run
        self.device_list: list[dict[str, Any]] = []
        # Last values pushed to the screen, so the daemon can skip redundant
        # feature reports. None means "nothing sent yet".
        self._last_cpu: int | None = None
        self._last_temperature: int | None = None
        print(
            """WARNING: If this program errors out or you cancel early, the keyboard
              may become unresponsive. It should work fine again if you unplug and plug
               it back in!"""
        )

        if install_signal_handlers:
            self._setup_signal_handling()

    def has_capability(self, name: str) -> bool:
        """Return True if this model advertises the given capability flag."""
        return name in self.capabilities

    @property
    def is_open(self) -> bool:
        """Return True once the HID device has been opened successfully."""
        return self._opened and self.device is not None

    def _setup_signal_handling(self) -> None:
        """Sets up signal handling to close the HID device on termination."""
        signal.signal(signal.SIGINT, self._signal_handler)  # Handle Ctrl+C
        signal.signal(signal.SIGTERM, self._signal_handler)  # Handle termination

    def _signal_handler(self, sig: int, frame: Optional[FrameType]) -> None:
        """Handles signals: close the HID device, then unwind via KeyboardInterrupt.

        Raising (instead of ``os._exit``) lets the normal try/except/finally flow
        run, so the daemon's ``except KeyboardInterrupt`` path and any ``finally``
        cleanup execute cleanly.
        """
        self.close_device()
        raise KeyboardInterrupt

    def __enter__(self) -> "EpomakerController":
        """Context-manager entry; device must still be opened via ``open_device``."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Always release the HID device when leaving a ``with`` block."""
        self.close_device()

    def __del__(self) -> None:
        """Destructor to ensure the device is closed."""
        self.close_device()

    def open_device(self, only_info: bool = False) -> bool:
        """Opens the USB HID device and prints device information.

        Args:
            only_info (bool): Print device information and exit (default: False).

        Raises:
            ValueError: If no device is found with the specified interface number.

        Returns:
            bool: True if the device is opened successfully, False otherwise.
        """
        if self.dry_run:
            print("Dry run: skipping device open")
            self._opened = True
            return True

        product_id = self._find_product_id()
        if not product_id:
            raise DeviceNotOpenError(
                "No matching Epomaker/Royuan HID device found"
            )

        if only_info:
            self._print_device_info()
            return True

        # Find the device with the specified interface number so we can open by path
        # This way we don't block usage of the keyboard whilst the device is open
        device_path = self._find_device_path()
        if device_path is None:
            raise DeviceNotOpenError("No device found")
        self._open_device(device_path)

        return self.device is not None

    def _find_product_id(self) -> int | None:
        """Finds the product ID of the device using a list of possible product IDs.

        Returns:
            int | None: The product ID if found, None otherwise.
        """
        for pid in self.product_ids:
            self.device_list = hid.enumerate(self.vendor_id, pid)
            if self.device_list:
                return pid
        return None

    def _open_device(self, device_path: bytes) -> None:
        """Opens the USB HID device.

        Args:
            device_path (bytes): The path to the device.
        """
        with self.lock:
            try:
                self.device = hid.device()
                self.device.open_path(device_path)
            except IOError as e:
                self.device = None
                self._opened = False
                raise DeviceNotOpenError(
                    f"Failed to open device: {e}\n"
                    "Please make sure the device is connected\n"
                    "and you have the necessary permissions.\n\n"
                    "You may need to run this program as root or with sudo, or\n"
                    "set up a udev rule to allow access to the device.\n\n"
                ) from e

            if self.device is None:
                raise DeviceNotOpenError("Failed to open HID device")
            self._opened = True

    def generate_udev_rule(self) -> None:
        """Generates a udev rule for the connected keyboard."""
        rule_content = (
            f"# Epomaker keyboard (VID {self.vendor_id:04x})\n"
            f'SUBSYSTEM=="usb", ATTRS{{idVendor}}=="{self.vendor_id:04x}", '
            f'ATTRS{{idProduct}}=="{self._find_product_id():04x}", '
            'MODE="0660", TAG+="uaccess"\n'
        )

        rule_file_path = "/etc/udev/rules.d/99-epomaker-keyboard.rules"

        print("Generating udev rule for Epomaker keyboard")
        print(f"Rule content:\n{rule_content}")
        print(f"Rule file path: {rule_file_path}")
        print("Please enter your password if prompted")

        sudo = ["sudo"] if os.geteuid() != 0 else []

        # Pipe the rule straight into place as root. This avoids the previous
        # predictable /tmp file, which was a symlink/TOCTOU attack surface for
        # the privileged install.
        subprocess.run(
            sudo + ["tee", rule_file_path],
            input=rule_content.encode("utf-8"),
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(sudo + ["udevadm", "control", "--reload-rules"], check=True)
        subprocess.run(sudo + ["udevadm", "trigger"], check=True)

        print("Rule generated successfully")

    def _print_device_info(self) -> None:
        """Prints device information."""
        devices = self.device_list.copy()
        for device in devices:
            device["path"] = device["path"].decode("utf-8")
            device["vendor_id"] = f"0x{device['vendor_id']:04x}"
            device["product_id"] = f"0x{device['product_id']:04x}"
        print(
            dumps(
                devices,
                indent="  ",
            )
        )

    @dataclasses.dataclass
    class HIDInfo:
        device_name: str
        event_path: str
        hid_path: Optional[str] = None

    def _find_device_path(self) -> Optional[bytes]:
        """Finds the device path with the specified interface number.

        Returns:
            Optional[bytes]: The device path if found, None otherwise.
        """
        input_dir = "/sys/class/input"
        hid_infos = []
        try:
            hid_infos = EpomakerController._get_hid_infos(
                input_dir, self.device_description
            )
        except Exception as e:
            print(f"Error reading input directory: {e}")

        if not hid_infos:
            print(f"No events found with description: '{self.device_description}' in sysfs, falling back to direct HID enumeration")
            product_id = self._find_product_id()
            if product_id:
                for dev in hid.enumerate(self.vendor_id, product_id):
                    if dev.get('interface_number') == 1:
                        return dev['path']
                devs = hid.enumerate(self.vendor_id, product_id)
                if devs:
                    return devs[0]['path']
            return None

        EpomakerController._populate_hid_paths(hid_infos)

        return self._select_device_path(hid_infos)

    @staticmethod
    def _get_hid_infos(input_dir: str, description: str) -> list[HIDInfo]:
        """Retrieve HID information based on the given description."""
        hid_infos = []
        for event in os.listdir(input_dir):
            if event.startswith("event"):
                device_name_path = os.path.join(input_dir, event, "device", "name")
                try:
                    with open(device_name_path, "r", encoding="utf-8") as f:
                        device_name = f.read().strip()
                        if re.search(description, device_name):
                            event_path = os.path.join(input_dir, event)
                            hid_infos.append(
                                EpomakerController.HIDInfo(device_name, event_path)
                            )
                except FileNotFoundError:
                    continue
        return hid_infos

    @staticmethod
    def _populate_hid_paths(hid_infos: list[HIDInfo]) -> None:
        """Populate the HID paths for each HIDInfo object in the list."""
        for hi in hid_infos:
            device_symlink = os.path.join(hi.event_path, "device")
            if not os.path.islink(device_symlink):
                print(f"No 'device' symlink found in {hi.event_path}")
                continue

            hid_device_path = os.path.realpath(device_symlink)
            match = re.search(r"\b\d+-[\d.]+:\d+\.\d+\b", hid_device_path)
            hi.hid_path = match.group(0) if match else None

    def _select_device_path(self, hid_infos: list[HIDInfo]) -> Optional[bytes]:
        """Select the appropriate device path based on interface preference."""
        device_name_filter = "Wireless" if self.use_wireless else "Wired"
        filtered_devices = [h for h in hid_infos if device_name_filter in h.device_name]

        if not filtered_devices:
            print(f"Could not find {device_name_filter} interface, falling back to any matching interface")
            filtered_devices = hid_infos

        if not filtered_devices:
            return None

        selected_device = filtered_devices[0]
        return (
            selected_device.hid_path.encode("utf-8")
            if selected_device.hid_path
            else None
        )

    def _send_command(
        self,
        command: EpomakerCommand.EpomakerCommand,
        *,
        erase_delay_s: float | None = None,
        packet_delay_s: float | None = None,
    ) -> None:
        """Sends a command to the HID device, optionally pacing packets.

        Args:
            command: The command to send.
            erase_delay_s: Sleep after the first (init) report for SRAM erase.
            packet_delay_s: Sleep after each subsequent data report.
        """
        if not command.report_data_prepared:
            raise CommandNotPreparedError("Report data not prepared")

        if not self.dry_run:
            if not self.device:
                raise DeviceNotOpenError("Device is not set!")
            try:
                self.device.get_product_string()
            except Exception as e:
                raise DeviceCommunicationError(
                    "Could not communicate with device"
                ) from e

        with self.lock:
            reports = list(command)
            for i, packet in enumerate(reports):
                if len(packet) != BUFF_LENGTH:
                    raise ProtocolError(
                        f"Report length {len(packet)} != {BUFF_LENGTH}"
                    )
                if self.dry_run:
                    print(f"Dry run: skipping command send: {packet!r}")
                else:
                    self.device.send_feature_report(packet.get_all_bytes())
                if i == 0 and erase_delay_s:
                    time.sleep(erase_delay_s)
                elif i > 0 and packet_delay_s:
                    time.sleep(packet_delay_s)

    def send_raw_report(self, data: bytes) -> None:
        """Sends a raw feature report to the HID device in a thread-safe manner."""
        with self.lock:
            if self.device:
                self.device.send_feature_report(data)

    @staticmethod
    def _assert_range(value: int, r: range | None = None) -> bool:
        """Asserts that a value is within a specified range.

        Args:
            value (int): The value to check.
            r (range): The range to check against (default: None).

        Returns:
            bool: True if the value is within the range, False otherwise.
        """
        if not r:
            r = range(0, 100)  # 0 to 99
        return value in r

    def _find_interface_path(self, interface_number: int) -> bytes | None:
        """Return the HID path for a given interface number, if present."""
        for device_info in hid.enumerate(self.vendor_id):
            if (
                device_info["product_id"] in self.product_ids
                and device_info["interface_number"] == interface_number
            ):
                return device_info["path"]
        return None

    def _upload_dynatab_command(
        self,
        screen_command: EpomakerDynaTabScreenCommand,
        *,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Upload a prepared DynaTab screen command via interfaces 2 then 0."""
        if self.dry_run:
            if progress:
                progress({"stage": "erasing"})
                progress({"stage": "uploading", "sent": 0, "total": 0})
                progress({"stage": "flashing"})
                progress({"stage": "applying"})
                progress({"stage": "done"})
            print(
                f"Dry run: skipping DynaTab screen upload "
                f"({len(list(screen_command))} reports)"
            )
            return

        # 1. Open Interface 2 and send the screen command packets
        dev2_path = self._find_interface_path(2)
        if not dev2_path:
            raise DeviceNotOpenError("Interface 2 not found for screen upload.")

        dev2 = None
        upload_ok = False
        try:
            dev2 = hid.device()
            dev2.open_path(dev2_path)
            reports_list = list(screen_command)
            if reports_list:
                if progress:
                    progress({"stage": "erasing"})
                # Init report (0xa9) then wait for flash memory erase/init
                dev2.send_feature_report(
                    b"\x00" + reports_list[0].get_all_bytes()
                )
                time.sleep(ERASE_DELAY_S)

                # Data reports (0x29) with packet pacing
                data_reports = reports_list[1:]
                total = len(data_reports)
                if progress:
                    progress({"stage": "uploading", "sent": 0, "total": total})
                for index, packet in enumerate(data_reports, start=1):
                    dev2.send_feature_report(b"\x00" + packet.get_all_bytes())
                    time.sleep(PACKET_DELAY_S)
                    if progress and (index == total or index % 5 == 0):
                        progress(
                            {
                                "stage": "uploading",
                                "sent": index,
                                "total": total,
                            }
                        )
            upload_ok = True
        except Exception as e:
            raise DeviceCommunicationError(
                f"Failed to upload screen design on Interface 2: {e}"
            ) from e
        finally:
            if dev2 is not None:
                dev2.close()

        if upload_ok:
            if progress:
                progress(
                    {"stage": "flashing", "seconds": SCREEN_FLASH_WAIT_S}
                )
            print(
                "Animation uploaded on Interface 2. Waiting "
                f"{SCREEN_FLASH_WAIT_S:g} seconds for the keyboard to write to "
                "flash and reboot..."
            )
            time.sleep(SCREEN_FLASH_WAIT_S)

        # 2. Open Interface 0 to apply/activate (re-enumerate after reboot)
        dev0_path = self._find_interface_path(0)
        if not dev0_path:
            raise DeviceNotOpenError(
                "Interface 0 not found after screen upload (keyboard may still "
                "be rebooting)."
            )
        dev0 = None
        try:
            if progress:
                progress({"stage": "applying"})
            dev0 = hid.device()
            dev0.open_path(dev0_path)
            dev0.write(b"\x00\x00")
            time.sleep(0.3)
            dev0.write(b"\x00\x01")
            if progress:
                progress({"stage": "done"})
        except Exception as e:
            raise DeviceCommunicationError(
                f"Failed to apply screen design on Interface 0: {e}"
            ) from e
        finally:
            if dev0 is not None:
                dev0.close()

    def _send_dynatab_live_command(
        self,
        screen_command: EpomakerDynaTabScreenCommand,
        *,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Send a DynaTab screen command over iface 1 without flashing.

        This mirrors the official Epomaker app: the ``0xA9`` prep report and
        the ``0x29`` data reports go to the vendor configuration interface,
        with no iface-0 apply and no reboot. The screen updates live and
        typing is not interrupted.
        """
        if self.dry_run:
            if progress:
                progress({"stage": "uploading", "sent": 0, "total": 0})
                progress({"stage": "done"})
            return

        reports = list(screen_command)
        if not reports:
            return

        dev = self.device if self._opened and self.device is not None else None
        close_after = False
        if dev is None:
            path = self._find_interface_path(1)
            if not path:
                raise DeviceNotOpenError(
                    "Interface 1 not found for live screen upload."
                )
            dev = hid.device()
            dev.open_path(path)
            close_after = True

        total = len(reports) - 1
        try:
            with self.lock:
                if progress:
                    progress({"stage": "uploading", "sent": 0, "total": total})
                dev.send_feature_report(bytes(reports[0].get_all_bytes()))
                time.sleep(ERASE_DELAY_S)
                for index, packet in enumerate(reports[1:], start=1):
                    dev.send_feature_report(bytes(packet.get_all_bytes()))
                    time.sleep(PACKET_DELAY_S)
                    if progress and (index == total or index % 5 == 0):
                        progress(
                            {
                                "stage": "uploading",
                                "sent": index,
                                "total": total,
                            }
                        )
                if progress:
                    progress({"stage": "done"})
        finally:
            if close_after:
                dev.close()

    def send_dynatab_frames(
        self,
        frames_rgb: list[list[tuple[int, int, int]]],
        delay_ms: int = 100,
        *,
        flash: bool = False,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Send pixel frames directly to the DynaTab 75X screen (no image file).

        By default the frames are sent live over iface 1, the same path the
        official app uses: no 12 s flash wait and no iface-0 apply, so typing
        is not interrupted. Pass ``flash=True`` for the legacy flash-and-reboot
        path.

        Args:
            frames_rgb: Column-major RGB frames (60×9 = 540 pixels each).
            delay_ms: Animation frame delay in milliseconds.
            flash: Write to flash and reboot instead of updating live.
            progress: Optional callback receiving upload stage dictionaries.
        """
        screen_command = EpomakerDynaTabScreenCommand(frames_rgb, delay_ms)
        if flash:
            self._upload_dynatab_command(screen_command, progress=progress)
        else:
            self._send_dynatab_live_command(screen_command, progress=progress)

    def send_dynatab_screen(
        self,
        image_path: str,
        delay_ms: int = 100,
        *,
        flash: bool = False,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Sends an image or GIF to the DynaTab 75X screen.

        Args:
            image_path (str): The path to the image/GIF file.
            delay_ms (int): The animation frame delay in milliseconds (default: 100).
            flash: Write to flash and reboot instead of updating live.
            progress: Optional callback receiving upload stage dictionaries.
        """
        screen_command = EpomakerDynaTabScreenCommand.from_image(
            image_path, delay_ms
        )
        if flash:
            self._upload_dynatab_command(screen_command, progress=progress)
        else:
            self._send_dynatab_live_command(screen_command, progress=progress)

    def send_image(
        self,
        image_path: str,
        delay_ms: int = 100,
        *,
        flash: bool = False,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Sends an image or GIF to the HID device.

        Args:
            image_path (str): The path to the image file.
            delay_ms (int): The animation frame delay in milliseconds (default: 100).
            flash: Write to flash and reboot instead of updating live.
            progress: Optional callback receiving upload stage dictionaries.
        """
        if self.has_capability(CAPABILITY_DYNATAB_SCREEN):
            if flash:
                was_open = self.is_open
                if was_open:
                    self.close_device()
                try:
                    self.send_dynatab_screen(
                        image_path, delay_ms, flash=True, progress=progress
                    )
                finally:
                    if was_open:
                        self.open_device()
            else:
                self.send_dynatab_screen(
                    image_path, delay_ms, flash=False, progress=progress
                )
            return

        if not self.has_capability(CAPABILITY_RT100_SCREEN):
            raise ProtocolError(
                "No screen upload capability for this model "
                f"(capabilities={sorted(self.capabilities)})"
            )

        image_command = EpomakerImageCommand.EpomakerImageCommand()
        image_command.encode_image(image_path)
        if progress:
            progress({"stage": "uploading", "sent": 0, "total": 0})
        self._send_command(image_command)
        if progress:
            progress({"stage": "done"})

    def send_time(self, time: datetime | None = None) -> None:
        """Sends `time` to the HID device.

        Args:
            time (datetime): The time to send (default: None).
        """
        if not time:
            time = datetime.now()
        time_command = EpomakerTimeCommand.EpomakerTimeCommand(time)
        self._send_command(time_command)

    def send_temperature(
        self, temperature: int | None, *, skip_unchanged: bool = False
    ) -> bool:
        """Sends the temperature to the HID device.

        Args:
            temperature (int): The temperature value in C (0-99).
            skip_unchanged (bool): Return False instead of resending when the
                value equals the last one written.

        Returns:
            bool: True if a report was written, False if skipped.

        Raises:
            ValueError: If the temperature is not in the range 0-99.
        """
        if temperature is None:
            # Don't send anything if no temperature was provided (0 °C is valid)
            return False
        if not self._assert_range(temperature):
            raise ValueError(f"Temperature must be in range 0-99: {temperature}")
        if skip_unchanged and temperature == self._last_temperature:
            return False
        temperature_command = EpomakerTempCommand.EpomakerTempCommand(temperature)
        log.debug("Sending temperature %sC", temperature)
        self._send_command(temperature_command)
        self._last_temperature = temperature
        return True

    def send_cpu(self, cpu: int, *, skip_unchanged: bool = False) -> bool:
        """Sends the CPU percentage to the HID device.

        Args:
            cpu (int): The CPU percentage to send.

        Returns:
            bool: True if a report was written, False if the value was rejected
                as unchanged and ``skip_unchanged`` is set.

        Raises:
            ValueError: If the CPU percentage is not in the range 0-100.
        """
        if not self._assert_range(cpu, range(0, 101)):
            raise ValueError("CPU percentage must be in range 0-100")
        if skip_unchanged and cpu == self._last_cpu:
            return False
        cpu_command = EpomakerCpuCommand.EpomakerCpuCommand(cpu)
        log.debug("Sending CPU %s%%", cpu)
        self._send_command(cpu_command)
        self._last_cpu = cpu
        return True

    def set_rgb_all_keys(self, r: int, g: int, b: int) -> None:
        # Make sure values are within range
        for value in (r, g, b):
            if not self._assert_range(value, range(0, 256)):
                raise ValueError(f"RGB value {value} out of range 0-255")

        # Get all the keyboard keys
        keyboard_keys = KeyboardKeys(self.config_keymap)

        # Construct a KeyMap object
        mapping = EpomakerKeyRGBCommand.KeyMap(keyboard_keys)

        # Set all keys to r, g, b
        for key in keyboard_keys:
            mapping[key] = (r, g, b)

        frames = [EpomakerKeyRGBCommand.KeyboardRGBFrame(key_map=mapping)]
        self.send_keys(frames)

    def send_keys(self, frames: list[EpomakerKeyRGBCommand.KeyboardRGBFrame]) -> None:
        """Sends key RGB frames to the HID device with hardware-safe pacing.

        Args:
            frames (list): The list of KeyboardRGBFrame to send.
        """
        rgb_command = EpomakerKeyRGBCommand.EpomakerKeyRGBCommand(frames)
        self._send_command(
            rgb_command,
            erase_delay_s=ERASE_DELAY_S,
            packet_delay_s=PACKET_DELAY_S,
        )

    def remap_keys(self, key_index: int, key_combo: int) -> None:
        key_map_command = EpomakerRemapKeysCommand.EpomakerRemapKeysCommand(
            key_index, key_combo
        )
        self._send_command(key_map_command)

    def cycle_light_modes(self, sleep_seconds: int = 5) -> None:
        for counter, mode in enumerate(Profile.Mode):
            profile = Profile(
                mode=mode,
                speed=Profile.Speed.DEFAULT,
                brightness=Profile.Brightness.DEFAULT,
                dazzle=Profile.Dazzle.OFF,
                option=Profile.Option.OFF,
                rgb=(180, 180, 180),
            )
            self.set_profile(profile)
            print(
                f"[{counter + 1}/{len(Profile.Mode)}] Cycled to light mode: {mode.name}"
            )
            time.sleep(sleep_seconds)

    def set_profile(self, profile: Profile) -> None:
        """Set the keyboard profile."""
        profile_command = EpomakerProfileCommand.EpomakerProfileCommand(profile)
        self._send_command(profile_command)

    def _reopen_with_backoff(
        self,
        max_attempts: int,
        min_delay_s: float,
        max_delay_s: float,
    ) -> None:
        """Close and reopen the HID device, retrying with exponential backoff.

        Used when a send fails: the keyboard may have been unplugged, slept, or
        re-enumerated. Each attempt re-enumerates from scratch, so a device that
        comes back at a different hidraw path is picked up.

        Args:
            max_attempts: Open attempts before giving up. ``0`` means retry
                forever (the delay stays capped at *max_delay_s*), which is
                what an unattended daemon wants: a keyboard that is unplugged
                for minutes or hours is picked up whenever it comes back.
            min_delay_s: First backoff delay in seconds.
            max_delay_s: Cap for the exponential backoff.

        Raises:
            EpomakerError: The last open error, once a non-zero *max_attempts*
                is reached. Never raised when *max_attempts* is ``0``.
        """
        delay = min_delay_s
        attempt = 0

        while True:
            attempt += 1
            self.close_device()
            try:
                if self.open_device():
                    log.info("Reconnected to keyboard on attempt %d", attempt)
                    return
                last_error: Exception = DeviceNotOpenError("Device did not open")
            except (DeviceNotOpenError, OSError) as e:
                last_error = e

            # Don't write a warning every 30s for days: the first few attempts
            # and then one in every _RECONNECT_LOG_EVERY are warnings.
            noisy = attempt <= 5 or attempt % _RECONNECT_LOG_EVERY == 0
            log.log(
                logging.WARNING if noisy else logging.DEBUG,
                "Reconnect attempt %d%s failed: %s",
                attempt,
                f"/{max_attempts}" if max_attempts else "",
                last_error,
            )
            if max_attempts and attempt >= max_attempts:
                raise last_error

            time.sleep(delay)
            delay = min(delay * 2, max_delay_s)

    def _sync_clock_quietly(self) -> bool:
        """Send the clock, logging rather than raising on failure.

        Used on paths where a failure must not kill the daemon.

        Returns:
            bool: True if the clock was sent. On False the caller must retry
                the sync itself; the next daemon cycle only notices a dead
                device, not a clock that is still unset.
        """
        try:
            self.send_time()
        except (DeviceCommunicationError, DeviceNotOpenError, OSError) as e:
            log.warning("Clock sync failed, will retry: %s", e)
            return False
        return True

    def _daemon_cycle(
        self, temp_key: str | None, test_mode: bool, poll_interval_s: float
    ) -> None:
        """Send one round of CPU (and optional temperature) values."""
        # Send CPU usage, paced to at least `poll_interval_s` between updates
        with TimeHelper(min_duration=poll_interval_s):
            self.send_cpu(get_cpu_usage(test_mode), skip_unchanged=not test_mode)

        # Get device temperature using the provided key
        if temp_key:
            with TimeHelper(min_duration=poll_interval_s):
                self.send_temperature(
                    get_device_temp(temp_key, test_mode),
                    skip_unchanged=not test_mode,
                )
        elif test_mode:
            self.send_temperature(get_device_temp("dummy_device", test_mode))
            time.sleep(poll_interval_s)

    def start_daemon(
        self,
        temp_key: str | None,
        test_mode: bool,
        poll_interval_s: float = 1.6,
        *,
        reconnect_attempts: int = 5,
        reconnect_min_delay_s: float = 1.0,
        reconnect_max_delay_s: float = 30.0,
        clock_resync_s: float = 3600.0,
        wait_for_device: bool = False,
        _max_cycles: int | None = None,
    ) -> None:
        """Start a daemon to update the CPU usage and optionally a temperature.

        Values are only written when they change from the last sent value, so
        an idle machine does not emit a feature report every cycle.

        The loop survives device hiccups (unplug, sleep, mode switch): a failed
        send closes the HID handle and retries with exponential backoff. If the
        keyboard cannot be reopened after ``reconnect_attempts`` tries, the last
        error propagates so a supervisor (e.g. systemd ``Restart=on-failure``)
        can restart the daemon. With ``reconnect_attempts=0`` it never gives
        up, so the daemon simply waits for the keyboard to come back.

        Args:
            temp_key (str): A label corresponding to the device to monitor.
            test_mode (bool): Send random ints instead of real values.
            poll_interval_s (float): Minimum seconds between display updates.
            reconnect_attempts (int): Open attempts before giving up; 0 means
                retry forever.
            reconnect_min_delay_s (float): First backoff delay in seconds.
            reconnect_max_delay_s (float): Cap for the exponential backoff.
            clock_resync_s (float): Re-send the clock this often (seconds), in
                case the keyboard loses time across sleep or power loss.
            wait_for_device (bool): The device has not been opened yet; open
                it first, with the same backoff, instead of failing at once
                when the keyboard is not plugged in.

        Raises:
            EpomakerError: If the device cannot be reopened after reconnecting
                (only when ``reconnect_attempts`` is non-zero).
            KeyboardInterrupt: On SIGINT/SIGTERM (clean shutdown).
        """
        if wait_for_device and not self.dry_run:
            log.info("Waiting for the keyboard...")
            self._reopen_with_backoff(
                reconnect_attempts,
                reconnect_min_delay_s,
                reconnect_max_delay_s,
            )

        # Set current time and date. Failures here are retried, not fatal.
        last_clock_sync = time.monotonic()
        # True while the clock still needs sending after a failed attempt.
        clock_pending = False
        try:
            self.send_time()
        except (DeviceCommunicationError, DeviceNotOpenError, OSError) as e:
            log.warning("Initial clock sync failed, reconnecting: %s", e)
            self._reopen_with_backoff(
                reconnect_attempts,
                reconnect_min_delay_s,
                reconnect_max_delay_s,
            )
            clock_pending = not self._sync_clock_quietly()

        cycles = 0
        while True:
            try:
                if clock_pending or (
                    clock_resync_s
                    and time.monotonic() - last_clock_sync >= clock_resync_s
                ):
                    self.send_time()
                    last_clock_sync = time.monotonic()
                    clock_pending = False
                    # A time packet can redraw the screen, so values we think
                    # are already on it may no longer be. Re-send them.
                    self._last_cpu = None
                    self._last_temperature = None

                self._daemon_cycle(temp_key, test_mode, poll_interval_s)
            except (DeviceCommunicationError, DeviceNotOpenError, OSError) as e:
                log.warning("Device error, reconnecting: %s", e)
                self._reopen_with_backoff(
                    reconnect_attempts,
                    reconnect_min_delay_s,
                    reconnect_max_delay_s,
                )
                # The screen lost its contents across the reconnect.
                self._last_cpu = None
                self._last_temperature = None
                # If the clock can't be sent now, retry it at the top of the
                # next iteration instead of waiting for the hourly resync.
                clock_pending = not self._sync_clock_quietly()
                if not clock_pending:
                    last_clock_sync = time.monotonic()
                continue

            cycles += 1
            if _max_cycles is not None and cycles >= _max_cycles:
                return

    def close_device(self) -> None:
        """Closes the USB HID device."""
        with self.lock:
            self._opened = False
            if self.device:
                self.device.close()
                self.device = None
