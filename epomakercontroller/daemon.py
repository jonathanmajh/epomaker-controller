"""The Epomaker daemon: one process owns the HID device.

Clients (the CLI and both GUIs) talk to this daemon over a Unix socket in
``$XDG_RUNTIME_DIR``.  A single worker thread owns the :class:`EpomakerController`
so that the CPU/temperature loop, per-key RGB updates and screen uploads never
race for the HID handle.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import queue
import signal
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

from .commands.data.constants import Profile
from .configs.configs import Config, load_main_config
from .epomakercontroller import EpomakerController
from .exceptions import (
    DaemonError,
    DeviceCommunicationError,
    DeviceNotOpenError,
    EpomakerError,
    ProtocolError,
)
from .ipc import (
    PROTOCOL_VERSION,
    ensure_runtime_dir,
    get_socket_path,
    peer_uid,
    recv_message,
    send_message,
)
from .utils.sensors import get_cpu_usage, get_device_temp

log = logging.getLogger(__name__)

SUPPORTED_COMMANDS = frozenset(
    {
        "hello",
        "status",
        "send_time",
        "send_cpu",
        "send_temperature",
        "set_profile",
        "set_rgb_all_keys",
        "set_keys",
        "remap_keys",
        "upload_screen",
        "upload_image",
        "cycle_light_modes",
        "reload_config",
        "shutdown",
    }
)

# Commands that need a live HID handle.
_DEVICE_COMMANDS = SUPPORTED_COMMANDS - {
    "hello",
    "status",
    "reload_config",
    "shutdown",
}

_MAX_UPLOAD_FRAMES = 15
_FRAME_PIXELS = 60 * 9


def _as_int(value: Any, name: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise ProtocolError(f"{name} must be {bounds}: {value}")
    return value


def _as_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ProtocolError(f"{name} must be a boolean")
    return value


def _validate_rgb(value: Any, name: str = "rgb") -> tuple[int, int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ProtocolError(f"{name} must be a list of three integers")
    return (
        _as_int(value[0], f"{name}[0]", maximum=255),
        _as_int(value[1], f"{name}[1]", maximum=255),
        _as_int(value[2], f"{name}[2]", maximum=255),
    )


def _validate_screen_frames(frames: Any) -> list[list[tuple[int, int, int]]]:
    if not isinstance(frames, list) or not frames:
        raise ProtocolError("frames must be a non-empty list")
    if len(frames) > _MAX_UPLOAD_FRAMES:
        raise ProtocolError(
            f"too many frames ({len(frames)} > {_MAX_UPLOAD_FRAMES})"
        )
    result: list[list[tuple[int, int, int]]] = []
    for frame_index, frame in enumerate(frames):
        if not isinstance(frame, list) or len(frame) != _FRAME_PIXELS:
            raise ProtocolError(
                f"frame {frame_index} must contain {_FRAME_PIXELS} pixels"
            )
        result.append(
            [
                _validate_rgb(pixel, f"frames[{frame_index}][{pixel_index}]")
                for pixel_index, pixel in enumerate(frame)
            ]
        )
    return result


def _validate_key_frames(frames: Any) -> list[dict[str, Any]]:
    if not isinstance(frames, list) or not frames:
        raise ProtocolError("frames must be a non-empty list")
    if len(frames) > _MAX_UPLOAD_FRAMES:
        raise ProtocolError(
            f"too many frames ({len(frames)} > {_MAX_UPLOAD_FRAMES})"
        )
    result: list[dict[str, Any]] = []
    for frame_index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise ProtocolError(f"frame {frame_index} must be an object")
        keys = frame.get("keys")
        if not isinstance(keys, list):
            raise ProtocolError(f"frame {frame_index} keys must be a list")
        clean_keys: list[list[int]] = []
        for entry in keys:
            if not isinstance(entry, (list, tuple)) or len(entry) != 4:
                raise ProtocolError(
                    "each key entry must be [value, r, g, b]"
                )
            key_value = _as_int(entry[0], "key value")
            r, g, b = _validate_rgb(entry[1:], "key rgb")
            clean_keys.append([key_value, r, g, b])
        result.append(
            {
                "keys": clean_keys,
                "time_ms": _as_int(
                    frame.get("time_ms", 0), "time_ms", maximum=255
                ),
                "index": _as_int(frame.get("index", 0), "index", maximum=255),
            }
        )
    return result


def validate_request(command: str, args: dict[str, Any]) -> None:
    """Validate a client request before it reaches the HID layer."""
    if command == "send_cpu":
        _as_int(args.get("cpu"), "cpu", maximum=100)
    elif command == "send_temperature":
        _as_int(args.get("temperature"), "temperature", maximum=99)
    elif command == "set_rgb_all_keys":
        _validate_rgb([args.get("r"), args.get("g"), args.get("b")])
    elif command == "set_keys":
        _validate_key_frames(args.get("frames"))
    elif command == "upload_screen":
        _validate_screen_frames(args.get("frames"))
        _as_int(args.get("delay_ms", 100), "delay_ms", maximum=255)
        _as_bool(args.get("flash", False), "flash")
    elif command == "upload_image":
        path = args.get("path")
        if not isinstance(path, str) or not path:
            raise ProtocolError("path must be a non-empty string")
        _as_int(args.get("delay_ms", 100), "delay_ms", maximum=255)
        _as_bool(args.get("flash", False), "flash")
    elif command == "set_profile":
        try:
            Profile.Mode(args.get("mode"))
            Profile.Speed(args.get("speed"))
            Profile.Brightness(args.get("brightness"))
            Profile.Dazzle(args.get("dazzle"))
            Profile.Option(args.get("option"))
        except (TypeError, ValueError) as e:
            raise ProtocolError(f"invalid profile value: {e}") from e
        _validate_rgb(args.get("rgb"))
    elif command == "remap_keys":
        _as_int(args.get("key_index"), "key_index")
        _as_int(args.get("key_combo"), "key_combo")
    elif command == "cycle_light_modes":
        _as_int(args.get("sleep_seconds", 5), "sleep_seconds")
    elif command == "reload_config":
        pass
    elif command not in SUPPORTED_COMMANDS:
        raise ProtocolError(f"unknown command: {command}")
    else:
        # send_time/status/hello/shutdown take no arguments.
        pass


@dataclasses.dataclass
class Job:
    """A unit of work submitted to the device worker."""

    command: str
    args: dict[str, Any]
    progress: Callable[[dict[str, Any]], None] | None = None
    generation: int = 0
    event: threading.Event = dataclasses.field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None

    def wait(self, timeout: float | None = None) -> None:
        if not self.event.wait(timeout):
            raise DaemonError("timed out waiting for the daemon")


class DeviceWorker:
    """Serialise all HID access onto one thread with a priority queue."""

    def __init__(
        self,
        controller: EpomakerController,
        *,
        stats: bool = False,
        temp_key: str | None = None,
        poll_interval_s: float = 1.6,
        test_mode: bool = False,
        reconnect_attempts: int = 0,
        reconnect_min_delay_s: float = 1.0,
        reconnect_max_delay_s: float = 30.0,
        clock_resync_s: float = 3600.0,
        config_loader: Callable[[], Config] = load_main_config,
    ) -> None:
        self._controller = controller
        self._config_loader = config_loader
        self._stats = stats
        self._temp_key = temp_key
        self._poll_interval_s = poll_interval_s
        self._test_mode = test_mode
        self._reconnect_attempts = reconnect_attempts
        self._reconnect_min_delay_s = reconnect_min_delay_s
        self._reconnect_max_delay_s = reconnect_max_delay_s
        self._clock_resync_s = clock_resync_s

        self._jobs: queue.PriorityQueue[
            tuple[int, int, Job | None]
        ] = queue.PriorityQueue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._connected = False
        self._seq = 0
        self._set_keys_generation = 0
        self._next_open_at = 0.0
        self._open_delay = reconnect_min_delay_s
        self._last_stats_at = 0.0
        self._last_clock_sync = time.monotonic()
        self._clock_pending = False
        self._last_cpu: int | None = None
        self._last_temperature: int | None = None
        self._open_attempts = 0
        self._fatal_error: BaseException | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="epomaker-device-worker", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._state_lock:
            self._seq += 1
            self._jobs.put((-1, self._seq, None))
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._close_device()

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._connected

    @property
    def fatal_error(self) -> BaseException | None:
        """The error that made the worker give up, if any."""
        return self._fatal_error

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "connected": self._connected,
                "state": "connected" if self._connected else "waiting",
                "capabilities": sorted(self._controller.capabilities),
                "stats": self._stats,
                "pid": os.getpid(),
            }

    # -- submission --------------------------------------------------------

    def submit(
        self,
        command: str,
        args: dict[str, Any],
        progress: Callable[[dict[str, Any]], None] | None = None,
        *,
        priority: int = 0,
    ) -> Job:
        job = Job(command=command, args=args, progress=progress)
        with self._state_lock:
            self._seq += 1
            if command == "set_keys":
                self._set_keys_generation += 1
                job.generation = self._set_keys_generation
            self._jobs.put((priority, self._seq, job))
        return job

    def set_stats(self, enabled: bool) -> None:
        with self._state_lock:
            self._stats = enabled

    # -- worker loop -------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            if (
                self._stats
                and self._jobs.empty()
                and now - self._last_stats_at >= self._poll_interval_s
            ):
                self._run_stats()
                continue

            timeout = self._next_timeout(now)
            try:
                _priority, _seq, job = self._jobs.get(timeout=timeout)
            except queue.Empty:
                self._tick()
                continue

            if job is None:
                break
            self._process(job)

    def _next_timeout(self, now: float) -> float:
        if not self.connected and now >= self._next_open_at:
            return 0.0
        if self._stats:
            due = self._last_stats_at + self._poll_interval_s
            return max(0.0, min(due - now, 0.5))
        return 0.5

    def _tick(self) -> None:
        if not self.connected and time.monotonic() >= self._next_open_at:
            self._try_open()

    def _try_open(self) -> bool:
        try:
            self._controller.open_device()
        except (DeviceNotOpenError, OSError) as e:
            self._open_attempts += 1
            if (
                self._reconnect_attempts
                and self._open_attempts >= self._reconnect_attempts
            ):
                self._fatal_error = e
                self._stop.set()
                log.error(
                    "Giving up after %s open attempts: %s",
                    self._open_attempts,
                    e,
                )
            self._mark_disconnected()
            log.log(
                logging.DEBUG,
                "Daemon is waiting for the keyboard: %s",
                e,
            )
            return False
        with self._state_lock:
            self._connected = True
        self._open_attempts = 0
        self._open_delay = self._reconnect_min_delay_s
        self._next_open_at = 0.0
        self._last_cpu = None
        self._last_temperature = None
        try:
            self._controller.send_time()
            self._last_clock_sync = time.monotonic()
            self._clock_pending = False
        except (DeviceCommunicationError, DeviceNotOpenError, OSError):
            self._clock_pending = True
        log.info("Connected to keyboard")
        return True

    def _mark_disconnected(self) -> None:
        with self._state_lock:
            self._connected = False
        self._close_device()
        self._open_delay = max(self._open_delay, self._reconnect_min_delay_s)
        self._next_open_at = time.monotonic() + self._open_delay
        self._open_delay = min(
            self._open_delay * 2, self._reconnect_max_delay_s
        )

    def _close_device(self) -> None:
        try:
            self._controller.close_device()
        except Exception:
            pass

    def _ensure_connected(self) -> None:
        if self.connected:
            return
        if not self._try_open():
            raise DeviceNotOpenError(
                "device_unavailable: no matching keyboard is connected"
            )

    # -- jobs --------------------------------------------------------------

    def _process(self, job: Job) -> None:
        try:
            if job.command == "set_keys":
                with self._state_lock:
                    if job.generation != self._set_keys_generation:
                        job.result = {"superseded": True}
                        return
            if job.command == "status":
                job.result = self.status()
                return
            if job.command == "reload_config":
                job.result = self._reload_config()
                return
            if job.command == "shutdown":
                self._stop.set()
                job.result = {"stopping": True}
                return

            self._ensure_connected()
            job.result = self._dispatch(job)
        except (EpomakerError, OSError) as e:
            if (
                job.command in _DEVICE_COMMANDS
                and job.command != "upload_screen"
                and isinstance(
                    e, (DeviceCommunicationError, DeviceNotOpenError, OSError)
                )
            ):
                log.warning("Device error during %s: %s", job.command, e)
                self._mark_disconnected()
                self._clock_pending = True
            job.error = e
        except Exception as e:  # unexpected: report to the client, keep serving
            log.exception("Unexpected error during %s", job.command)
            job.error = e
        finally:
            job.event.set()

    def _dispatch(self, job: Job) -> Any:
        c = self._controller
        args = job.args
        command = job.command

        if command == "send_time":
            c.send_time()
            self._last_clock_sync = time.monotonic()
            self._clock_pending = False
            return None
        if command == "send_cpu":
            return c.send_cpu(args["cpu"])
        if command == "send_temperature":
            return c.send_temperature(args["temperature"])
        if command == "set_rgb_all_keys":
            c.set_rgb_all_keys(args["r"], args["g"], args["b"])
            return None
        if command == "set_profile":
            c.set_profile(
                Profile(
                    mode=Profile.Mode(args["mode"]),
                    speed=Profile.Speed(args["speed"]),
                    brightness=Profile.Brightness(args["brightness"]),
                    dazzle=Profile.Dazzle(args["dazzle"]),
                    option=Profile.Option(args["option"]),
                    rgb=tuple(args["rgb"]),
                )
            )
            return None
        if command == "set_keys":
            c.send_keys(self._frames_from_payload(args["frames"]))
            return None
        if command == "remap_keys":
            c.remap_keys(args["key_index"], args["key_combo"])
            return None
        if command == "upload_screen":
            return self._upload_screen(job)
        if command == "upload_image":
            c.send_image(
                args["path"],
                delay_ms=args.get("delay_ms", 100),
                flash=_as_bool(args.get("flash", False), "flash"),
                progress=job.progress,
            )
            return None
        if command == "cycle_light_modes":
            c.cycle_light_modes(args.get("sleep_seconds", 5))
            return None
        raise ProtocolError(f"unknown command: {command}")

    def _frames_from_payload(
        self, payload: list[dict[str, Any]]
    ) -> list[Any]:
        from .commands.EpomakerKeyRGBCommand import (
            KeyMap,
            KeyboardRGBFrame,
        )
        from .utils.keyboard_keys import KeyboardKey, KeyboardKeys

        keys = KeyboardKeys(self._controller.config_keymap)
        by_value = {key.value: key for key in keys}
        frames = []
        for frame in payload:
            mapping = KeyMap(keys)
            for value, r, g, b in frame["keys"]:
                key = by_value.get(value) or KeyboardKey(
                    name=str(value), value=value
                )
                mapping.key_map[key] = (r, g, b)
            frames.append(
                KeyboardRGBFrame(
                    key_map=mapping,
                    time_ms=frame.get("time_ms", 0),
                    index=frame.get("index", 0),
                )
            )
        return frames

    def _upload_screen(self, job: Job) -> dict[str, Any]:
        frames = _validate_screen_frames(job.args["frames"])
        delay_ms = job.args.get("delay_ms", 100)
        flash = _as_bool(job.args.get("flash", False), "flash")
        if flash:
            self._close_device()
            with self._state_lock:
                self._connected = False
        try:
            self._controller.send_dynatab_frames(
                frames,
                delay_ms=delay_ms,
                flash=flash,
                progress=job.progress,
            )
        finally:
            if flash:
                self._reconnect_after_upload()
        return {"uploaded": True, "connected": self.connected}

    def _reconnect_after_upload(self) -> None:
        # The keyboard reboots mid-upload.  Try to come back for a while; if it
        # stays away, the normal idle reconnect loop keeps trying.
        attempts = self._reconnect_attempts or 30
        delay = 0.5
        for _attempt in range(max(1, attempts)):
            if self._stop.is_set():
                return
            try:
                if self._controller.open_device():
                    with self._state_lock:
                        self._connected = True
                    self._open_delay = self._reconnect_min_delay_s
                    self._next_open_at = 0.0
                    self._last_cpu = None
                    self._last_temperature = None
                    self._clock_pending = True
                    try:
                        self._controller.send_time()
                        self._last_clock_sync = time.monotonic()
                        self._clock_pending = False
                    except (DeviceCommunicationError, DeviceNotOpenError, OSError):
                        self._clock_pending = True
                    return
            except (DeviceNotOpenError, OSError):
                pass
            time.sleep(delay)
            delay = min(delay * 2, 2.0)
        self._mark_disconnected()
        if self._reconnect_attempts:
            self._fatal_error = DeviceNotOpenError(
                "device_unavailable: keyboard did not come back after upload"
            )
            self._stop.set()

    def _reload_config(self) -> dict[str, Any]:
        new_config = self._config_loader()
        old = self._controller
        self._close_device()
        try:
            new_controller = EpomakerController(new_config)
        except Exception as e:
            raise DaemonError(f"could not reload config: {e}") from e
        self._controller = new_controller
        with self._state_lock:
            self._connected = False
        del old
        self._next_open_at = time.monotonic()
        return {"capabilities": sorted(new_controller.capabilities)}

    # -- stats loop --------------------------------------------------------

    def _paced_wait(self, started: float) -> bool:
        """Wait out the stats interval, but yield to queued client work.

        Returns False when a client job arrived (or the daemon is stopping),
        so the caller can skip the rest of this stats round.
        """
        deadline = started + self._poll_interval_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            if self._stop.is_set() or not self._jobs.empty():
                return False
            time.sleep(min(0.05, remaining))

    def _run_stats(self) -> None:
        self._last_stats_at = time.monotonic()
        if not self.connected:
            if not self._try_open():
                return
        try:
            if self._clock_pending or (
                self._clock_resync_s
                and time.monotonic() - self._last_clock_sync
                >= self._clock_resync_s
            ):
                self._controller.send_time()
                self._last_clock_sync = time.monotonic()
                self._clock_pending = False
                self._last_cpu = None
                self._last_temperature = None

            cpu_started = time.monotonic()
            cpu = get_cpu_usage(self._test_mode)
            if self._last_cpu != cpu or self._test_mode:
                self._controller.send_cpu(
                    cpu, skip_unchanged=not self._test_mode
                )
                self._last_cpu = cpu
            if not self._paced_wait(cpu_started):
                return

            if self._temp_key:
                if not self._jobs.empty():
                    return
                temp_started = time.monotonic()
                temperature = get_device_temp(
                    self._temp_key, self._test_mode
                )
                if (
                    self._last_temperature != temperature
                    or self._test_mode
                ):
                    self._controller.send_temperature(
                        temperature,
                        skip_unchanged=not self._test_mode,
                    )
                    self._last_temperature = temperature
                self._paced_wait(temp_started)
            elif self._test_mode:
                if not self._jobs.empty():
                    return
                temp_started = time.monotonic()
                self._controller.send_temperature(
                    get_device_temp("dummy_device", True)
                )
                self._paced_wait(temp_started)
        except (DeviceCommunicationError, DeviceNotOpenError, OSError) as e:
            log.warning("Device error while updating stats: %s", e)
            self._mark_disconnected()
            self._clock_pending = True


class DaemonServer:
    """Accept client connections and dispatch them to a :class:`DeviceWorker`."""

    def __init__(
        self,
        worker: DeviceWorker,
        *,
        socket_path: os.PathLike[str] | str | None = None,
        idle_timeout_s: float | None = None,
    ) -> None:
        self._worker = worker
        self._socket_path = (
            os.fspath(socket_path) if socket_path else str(get_socket_path())
        )
        self._idle_timeout_s = idle_timeout_s
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._clients = 0
        self._last_activity = time.monotonic()
        self._client_threads: set[threading.Thread] = set()
        self._threads_lock = threading.Lock()

    def stop(self) -> None:
        self._stop.set()

    def serve_forever(self) -> bool:
        """Run until stopped.  Returns False when another daemon owns the socket."""
        ensure_runtime_dir()
        if self._another_daemon_is_running():
            log.info("Another daemon is already listening; exiting")
            return False

        path = self._socket_path
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(path)
            os.chmod(path, 0o600)
            listener.listen(8)
            listener.settimeout(0.5)
        except BaseException:
            listener.close()
            raise
        self._listener = listener
        self._worker.start()
        log.info("Daemon listening on %s", path)

        try:
            while not self._stop.is_set():
                if self._worker.fatal_error is not None:
                    log.error("Stopping after fatal device error")
                    break
                if self._should_exit_idle():
                    log.info("No clients for a while; exiting")
                    break
                try:
                    conn, _addr = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                thread = threading.Thread(
                    target=self._handle_connection,
                    args=(conn,),
                    name="epomaker-client",
                    daemon=True,
                )
                with self._threads_lock:
                    self._client_threads.add(thread)
                thread.start()
        finally:
            self._stop.set()
            self._worker.stop()
            self._close_listener()
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            self._join_client_threads()
        return True

    def _join_client_threads(self) -> None:
        with self._threads_lock:
            threads = list(self._client_threads)
        for thread in threads:
            thread.join(timeout=1.0)

    def _close_listener(self) -> None:
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None

    def _should_exit_idle(self) -> bool:
        if not self._idle_timeout_s:
            return False
        with self._threads_lock:
            clients = self._clients
        return (
            clients == 0
            and time.monotonic() - self._last_activity >= self._idle_timeout_s
        )

    def _another_daemon_is_running(self) -> bool:
        path = self._socket_path
        if not os.path.exists(path):
            return False
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(path)
            send_message(probe, {"id": 0, "command": "hello", "args": {}})
            reply = recv_message(probe)
            return reply.get("ok") is True
        except (OSError, ConnectionError, ProtocolError, ValueError):
            return False
        finally:
            probe.close()

    def _handle_connection(self, conn: socket.socket) -> None:
        uid = peer_uid(conn)
        if uid is not None and uid != os.getuid():
            log.warning("Rejecting daemon client with uid %s", uid)
            conn.close()
            return

        with self._threads_lock:
            self._clients += 1
        self._last_activity = time.monotonic()
        try:
            with conn:
                while not self._stop.is_set():
                    try:
                        message = recv_message(conn)
                    except (ConnectionError, OSError):
                        break
                    except ProtocolError as e:
                        self._send_error(conn, None, e)
                        break
                    self._last_activity = time.monotonic()
                    reply = self._dispatch(message, conn)
                    try:
                        send_message(conn, reply)
                    except OSError:
                        break
        finally:
            with self._threads_lock:
                self._clients -= 1
                self._client_threads.discard(threading.current_thread())
            self._last_activity = time.monotonic()

    def _dispatch(
        self, message: dict[str, Any], conn: socket.socket
    ) -> dict[str, Any]:
        message_id = message.get("id")
        command = message.get("command")
        args = message.get("args") or {}
        if not isinstance(command, str):
            return _error_reply(message_id, ProtocolError("missing command"))
        if not isinstance(args, dict):
            return _error_reply(
                message_id, ProtocolError("args must be an object")
            )
        if command not in SUPPORTED_COMMANDS:
            return _error_reply(
                message_id, ProtocolError(f"unknown command: {command}")
            )

        if command == "hello":
            return {
                "id": message_id,
                "ok": True,
                "result": {
                    "protocol": PROTOCOL_VERSION,
                    **self._worker.status(),
                },
            }
        if command == "status":
            return {
                "id": message_id,
                "ok": True,
                "result": self._worker.status(),
            }
        if command == "shutdown":
            self._stop.set()
            return {"id": message_id, "ok": True, "result": {"stopping": True}}

        try:
            validate_request(command, args)
        except (ProtocolError, ValueError, TypeError) as e:
            return _error_reply(message_id, e)

        def progress(event: dict[str, Any]) -> None:
            try:
                send_message(
                    conn,
                    {"id": message_id, "event": "progress", **event},
                )
            except OSError:
                pass

        job = self._worker.submit(command, args, progress)
        try:
            job.wait(timeout=300.0)
        except DaemonError as e:
            return _error_reply(message_id, e)

        if job.error is not None:
            return _error_reply(message_id, job.error)
        return {"id": message_id, "ok": True, "result": job.result}

    def _send_error(
        self,
        conn: socket.socket,
        message_id: Any,
        error: BaseException,
    ) -> None:
        try:
            send_message(conn, _error_reply(message_id, error))
        except OSError:
            pass


def _error_reply(message_id: Any, error: BaseException) -> dict[str, Any]:
    return {
        "id": message_id,
        "ok": False,
        "error": {"type": type(error).__name__, "message": str(error)},
    }


def run_daemon(
    *,
    stats: bool,
    temp_key: str | None = None,
    poll_interval_s: float = 1.6,
    test_mode: bool = False,
    reconnect_attempts: int = 0,
    idle_timeout_s: float | None = None,
    verbose: bool = False,
    socket_path: str | None = None,
    config_loader: Callable[[], Config] = load_main_config,
) -> int:
    """Create the controller/worker/server, run it, and return an exit code."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = config_loader()
    controller = EpomakerController(config, install_signal_handlers=False)
    worker = DeviceWorker(
        controller,
        stats=stats,
        temp_key=temp_key,
        poll_interval_s=poll_interval_s,
        test_mode=test_mode,
        reconnect_attempts=reconnect_attempts,
        config_loader=config_loader,
    )
    server = DaemonServer(worker, socket_path=socket_path, idle_timeout_s=idle_timeout_s)

    def _handle_signal(_sig: int, _frame: Any) -> None:
        server.stop()

    old_int = signal.signal(signal.SIGINT, _handle_signal)
    old_term = signal.signal(signal.SIGTERM, _handle_signal)
    old_hup = signal.signal(signal.SIGHUP, lambda *_: _reload_in_background(worker))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.stop()
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGHUP, old_hup)
        worker.stop()
    return 1 if worker.fatal_error is not None else 0


def _reload_in_background(worker: DeviceWorker) -> None:
    def _reload() -> None:
        try:
            job = worker.submit("reload_config", {})
            job.wait(timeout=30.0)
            if job.error is not None:
                raise job.error
            log.info("Config reloaded: %s", job.result)
        except Exception as e:
            log.warning("Config reload failed: %s", e)

    threading.Thread(target=_reload, daemon=True).start()
