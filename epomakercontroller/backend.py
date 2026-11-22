"""Client backends: talk to the daemon, or (as an escape hatch) the HID device.

Everything that changes the keyboard goes through a :class:`Backend`.  The
socket backend lets the GUI and CLI run while the daemon owns the keyboard;
``--direct`` / ``EPOMAKER_DIRECT=1`` keeps the old direct-HID path for
debugging and tests.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .commands.data.constants import Profile
from .configs.configs import Config, ConfigType, load_main_config
from .epomakercontroller import EpomakerController
from .exceptions import (
    CommandNotPreparedError,
    ConfigError,
    DaemonError,
    DaemonUnavailableError,
    DeviceCommunicationError,
    DeviceNotOpenError,
    EpomakerError,
    ProtocolError,
    ProtocolVersionError,
    UnsupportedImageError,
)
from .ipc import (
    PROTOCOL_VERSION,
    connect_socket,
    get_runtime_dir,
    get_socket_path,
    recv_message,
    send_message,
)

log = logging.getLogger(__name__)

_EXCEPTION_TYPES: dict[str, type[EpomakerError]] = {
    "CommandNotPreparedError": CommandNotPreparedError,
    "ConfigError": ConfigError,
    "DaemonError": DaemonError,
    "DaemonUnavailableError": DaemonUnavailableError,
    "DeviceCommunicationError": DeviceCommunicationError,
    "DeviceNotOpenError": DeviceNotOpenError,
    "EpomakerError": EpomakerError,
    "ProtocolError": ProtocolError,
    "ProtocolVersionError": ProtocolVersionError,
    "UnsupportedImageError": UnsupportedImageError,
}

_MANUAL_START = "epomakercontroller serve"


def _error_from_reply(error: Any) -> EpomakerError:
    if not isinstance(error, dict):
        return DaemonError("daemon returned an error")
    error_type = error.get("type")
    message = str(error.get("message", "daemon returned an error"))
    cls = _EXCEPTION_TYPES.get(str(error_type), DaemonError)
    return cls(message)


class Backend:
    """The operations the GUIs and CLI need from a keyboard connection."""

    def __init__(self, config_main: Config) -> None:
        self.config_main = config_main
        self.config_layout = Config(
            ConfigType.CONF_LAYOUT, config_main["CONF_LAYOUT_PATH"]
        )
        self.config_keymap = Config(
            ConfigType.CONF_KEYMAP, config_main["CONF_KEYMAP_PATH"]
        )

    @property
    def connected(self) -> bool:
        raise NotImplementedError

    def has_capability(self, name: str) -> bool:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        raise NotImplementedError

    def refresh_status(self) -> dict[str, Any]:
        return self.status()

    def close(self) -> None:
        pass

    def close_device(self) -> None:
        """Release a directly-held HID handle; a no-op for socket clients."""
        pass

    def __enter__(self) -> "Backend":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def send_time(self) -> None:
        raise NotImplementedError

    def send_cpu(self, cpu: int, *, skip_unchanged: bool = False) -> bool:
        raise NotImplementedError

    def send_temperature(
        self, temperature: int | None, *, skip_unchanged: bool = False
    ) -> bool:
        raise NotImplementedError

    def set_rgb_all_keys(self, r: int, g: int, b: int) -> None:
        raise NotImplementedError

    def send_keys(self, frames: list[Any]) -> None:
        raise NotImplementedError

    def set_profile(self, profile: Profile) -> None:
        raise NotImplementedError

    def remap_keys(self, key_index: int, key_combo: int) -> None:
        raise NotImplementedError

    def cycle_light_modes(self, sleep_seconds: int = 5) -> None:
        raise NotImplementedError

    def send_dynatab_frames(
        self,
        frames_rgb: list[list[tuple[int, int, int]]],
        delay_ms: int = 100,
        *,
        progress: Any = None,
    ) -> None:
        raise NotImplementedError

    def send_image(
        self,
        image_path: str,
        delay_ms: int = 100,
        *,
        progress: Any = None,
    ) -> None:
        raise NotImplementedError


class DirectBackend(Backend):
    """The pre-daemon behaviour: own the HID handle in this process."""

    def __init__(
        self,
        config_main: Config,
        *,
        dry_run: bool = False,
        controller: EpomakerController | None = None,
    ) -> None:
        super().__init__(config_main)
        self.controller = controller or EpomakerController(
            config_main, dry_run=dry_run
        )

    @property
    def connected(self) -> bool:
        return self.controller.is_open

    def has_capability(self, name: str) -> bool:
        return self.controller.has_capability(name)

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "state": "connected" if self.connected else "waiting",
            "capabilities": sorted(self.controller.capabilities),
            "stats": False,
            "pid": os.getpid(),
        }

    def open(self) -> None:
        if not self.controller.open_device():
            raise DeviceNotOpenError("Failed to open device.")

    def close(self) -> None:
        self.controller.close_device()

    def close_device(self) -> None:
        self.controller.close_device()

    def send_time(self) -> None:
        self.controller.send_time()

    def send_cpu(self, cpu: int, *, skip_unchanged: bool = False) -> bool:
        return self.controller.send_cpu(cpu, skip_unchanged=skip_unchanged)

    def send_temperature(
        self, temperature: int | None, *, skip_unchanged: bool = False
    ) -> bool:
        return self.controller.send_temperature(
            temperature, skip_unchanged=skip_unchanged
        )

    def set_rgb_all_keys(self, r: int, g: int, b: int) -> None:
        self.controller.set_rgb_all_keys(r, g, b)

    def send_keys(self, frames: list[Any]) -> None:
        self.controller.send_keys(frames)

    def set_profile(self, profile: Profile) -> None:
        self.controller.set_profile(profile)

    def remap_keys(self, key_index: int, key_combo: int) -> None:
        self.controller.remap_keys(key_index, key_combo)

    def cycle_light_modes(self, sleep_seconds: int = 5) -> None:
        self.controller.cycle_light_modes(sleep_seconds)

    def send_dynatab_frames(
        self,
        frames_rgb: list[list[tuple[int, int, int]]],
        delay_ms: int = 100,
        *,
        progress: Any = None,
    ) -> None:
        self.controller.send_dynatab_frames(
            frames_rgb, delay_ms=delay_ms, progress=progress
        )

    def send_image(
        self,
        image_path: str,
        delay_ms: int = 100,
        *,
        progress: Any = None,
    ) -> None:
        self.controller.send_image(
            image_path, delay_ms=delay_ms, progress=progress
        )


class SocketBackend(Backend):
    """A client connection to the daemon socket."""

    def __init__(
        self,
        sock: Any,
        hello: dict[str, Any],
        config_main: Config,
    ) -> None:
        super().__init__(config_main)
        self._sock = sock
        try:
            # Uploads include a 12s flash wait, so the 1s connect timeout must
            # not stay in force for requests.
            sock.settimeout(300.0)
        except OSError:
            pass
        self._next_id = 1
        self._hello = hello
        self._connected = bool(hello.get("connected"))
        self._capabilities = set(hello.get("capabilities") or [])

    @property
    def connected(self) -> bool:
        return self._connected

    def has_capability(self, name: str) -> bool:
        return name in self._capabilities

    def status(self) -> dict[str, Any]:
        result = self._request("status")
        return result or {}

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _request(
        self,
        command: str,
        args: dict[str, Any] | None = None,
        *,
        progress: Any = None,
    ) -> Any:
        request_id = self._next_id
        self._next_id += 1
        try:
            send_message(
                self._sock,
                {"id": request_id, "command": command, "args": args or {}},
            )
            while True:
                reply = recv_message(self._sock)
                if reply.get("id") != request_id:
                    continue
                if reply.get("event") == "progress":
                    if progress is not None:
                        event = {
                            key: value
                            for key, value in reply.items()
                            if key not in {"id", "event"}
                        }
                        progress(event)
                    continue
                if not reply.get("ok"):
                    raise _error_from_reply(reply.get("error"))
                result = reply.get("result")
                if isinstance(result, dict) and "connected" in result:
                    self._connected = bool(result["connected"])
                return result
        except (ConnectionError, OSError) as e:
            self._connected = False
            raise DaemonUnavailableError(f"lost connection to daemon: {e}") from e

    def refresh_status(self) -> dict[str, Any]:
        try:
            result = self.status()
        except DaemonError:
            raise
        if "capabilities" in result:
            self._capabilities = set(result["capabilities"])
        return result

    def send_time(self) -> None:
        self._request("send_time")

    def send_cpu(self, cpu: int, *, skip_unchanged: bool = False) -> bool:
        self._request("send_cpu", {"cpu": cpu})
        return True

    def send_temperature(
        self, temperature: int | None, *, skip_unchanged: bool = False
    ) -> bool:
        if temperature is None:
            return False
        self._request("send_temperature", {"temperature": temperature})
        return True

    def set_rgb_all_keys(self, r: int, g: int, b: int) -> None:
        self._request("set_rgb_all_keys", {"r": r, "g": g, "b": b})

    def send_keys(self, frames: list[Any]) -> None:
        payload = []
        for frame in frames:
            keys = [
                [key.value, rgb[0], rgb[1], rgb[2]]
                for key, rgb in frame.key_map
            ]
            payload.append(
                {
                    "keys": keys,
                    "time_ms": frame.time_ms,
                    "index": frame.index,
                }
            )
        self._request("set_keys", {"frames": payload})

    def set_profile(self, profile: Profile) -> None:
        self._request(
            "set_profile",
            {
                "mode": profile.mode.value,
                "speed": profile.speed.value,
                "brightness": profile.brightness.value,
                "dazzle": profile.dazzle.value,
                "option": profile.option.value,
                "rgb": list(profile.rgb),
            },
        )

    def remap_keys(self, key_index: int, key_combo: int) -> None:
        self._request(
            "remap_keys",
            {"key_index": key_index, "key_combo": key_combo},
        )

    def cycle_light_modes(self, sleep_seconds: int = 5) -> None:
        self._request("cycle_light_modes", {"sleep_seconds": sleep_seconds})

    def send_dynatab_frames(
        self,
        frames_rgb: list[list[tuple[int, int, int]]],
        delay_ms: int = 100,
        *,
        progress: Any = None,
    ) -> None:
        frames = [
            [list(pixel) for pixel in frame] for frame in frames_rgb
        ]
        self._request(
            "upload_screen",
            {"frames": frames, "delay_ms": delay_ms},
            progress=progress,
        )

    def send_image(
        self,
        image_path: str,
        delay_ms: int = 100,
        *,
        progress: Any = None,
    ) -> None:
        path = Path(image_path).expanduser().resolve()
        self._request(
            "upload_image",
            {"path": str(path), "delay_ms": delay_ms},
            progress=progress,
        )


def _connect_existing(config_main: Config) -> SocketBackend | None:
    path = get_socket_path()
    if not path.exists():
        return None
    try:
        sock = connect_socket(path, timeout=1.0)
    except OSError:
        return None
    try:
        send_message(sock, {"id": 0, "command": "hello", "args": {}})
        reply = recv_message(sock)
    except (OSError, ConnectionError, ProtocolError) as e:
        sock.close()
        log.debug("Ignoring unusable daemon socket: %s", e)
        return None
    if not reply.get("ok"):
        sock.close()
        return None
    hello = reply.get("result") or {}
    protocol = hello.get("protocol")
    if protocol != PROTOCOL_VERSION:
        sock.close()
        raise ProtocolVersionError(
            "The running epomaker daemon speaks protocol "
            f"{protocol!r}, but this client needs {PROTOCOL_VERSION}. "
            "Restart it with: systemctl --user restart epomaker-daemon"
        )
    return SocketBackend(sock, hello, config_main)


def _unit_file() -> Path:
    return Path.home() / ".config/systemd/user/epomaker-daemon.service"


def _start_via_systemd() -> bool:
    systemctl = shutil.which("systemctl")
    if not systemctl or not _unit_file().exists():
        return False
    try:
        result = subprocess.run(
            [systemctl, "--user", "start", "epomaker-daemon"],
            timeout=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _log_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    directory = base / "epomaker-controller"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "daemon.log"


def _spawn_daemon() -> None:
    log_file = _log_path()
    with open(log_file, "ab") as handle:
        subprocess.Popen(
            [sys.executable, "-m", "epomakercontroller", "serve"],
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )


def start_daemon() -> None:
    """Start the daemon via systemd if installed, otherwise spawn ``serve``."""
    if _start_via_systemd():
        return
    _spawn_daemon()


def connect_backend(
    config_main: Config | None = None,
    *,
    direct: bool = False,
    dry_run: bool = False,
    start: bool = True,
    timeout: float = 10.0,
    on_status: Any = None,
) -> Backend:
    """Return a backend, starting the daemon if one is not running.

    ``direct=True`` (or ``EPOMAKER_DIRECT=1``) bypasses the daemon entirely.
    That is the escape hatch for debugging, tests and ``--dry-run``.
    """
    config = config_main or load_main_config()
    if direct or os.environ.get("EPOMAKER_DIRECT") == "1" or dry_run:
        backend = DirectBackend(config, dry_run=dry_run)
        backend.open()
        return backend

    backend = _connect_existing(config)
    if backend is not None:
        return backend

    if not start:
        raise DaemonUnavailableError(
            "The epomaker daemon is not running. Start it with: "
            f"{_MANUAL_START}"
        )

    if on_status:
        on_status("Starting daemon…")
    try:
        start_daemon()
    except OSError as e:
        raise DaemonUnavailableError(
            f"Could not start the epomaker daemon: {e}. "
            f"Try starting it manually: {_MANUAL_START}"
        ) from e

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        backend = _connect_existing(config)
        if backend is not None:
            return backend
        time.sleep(0.1)

    raise DaemonUnavailableError(
        "Could not start the epomaker daemon. Try starting it manually: "
        f"{_MANUAL_START}"
    )


def runtime_dir_for_tests() -> Path:
    """Expose the runtime directory without importing ipc in callers."""
    return get_runtime_dir()
