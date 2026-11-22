import logging
import time

import psutil
import random
import gpustat
from pynvml import NVMLError

log = logging.getLogger(__name__)


def get_cpu_usage(test_mode: bool = False) -> int:
    """Get the current CPU usage.

    Args:
        test_mode (bool): If true, return a random int between 0 and 99.

    Returns:
        int: CPU usage, or a random number if `test_mode`.
    """
    if test_mode:
        return random.randint(0, 99)
    return int(round(psutil.cpu_percent(interval=1)))


# Keys we've already complained about, so a typo warns once per run instead
# of every poll cycle (the daemon would otherwise show 0 °C in silence).
_WARNED_TEMP_KEYS: set[str] = set()


def get_device_temp(temp_key: str, test_mode: bool = False) -> int:
    """Get the temperature of a device specified by `temp_key`, or 0 if the key cannot be found.

    Args:
        temp_key (str): Key corresponding to a device.
        test_mode (bool): If true, return a random int between 0 and 99.

    Returns:
        int: The temperature of the device, 0 if not found, or a random number if `test_mode`.
    """
    if test_mode:
        return random.randint(0, 99)

    temps = _get_temp_devices()
    if not temps:
        if temp_key not in _WARNED_TEMP_KEYS:
            _WARNED_TEMP_KEYS.add(temp_key)
            log.warning(
                "No temperature sensors found; the screen will show 0 °C."
            )
        return 0

    if temp_key in temps:
        device_temp = temps[temp_key]
        return int(round(device_temp))

    if temp_key not in _WARNED_TEMP_KEYS:
        _WARNED_TEMP_KEYS.add(temp_key)
        log.warning(
            "Temperature key %r not found. Available keys: %s "
            "(check with `epomakercontroller list-temp-devices`)",
            temp_key,
            list(temps.keys()),
        )

    return 0


# How long to trust a failed NVIDIA probe before trying again. Long enough
# not to hammer NVML every poll, short enough that loading the driver (or
# resuming from suspend) is picked up without restarting the daemon.
NVIDIA_RETRY_INTERVAL_S = 300.0

# None = not probed yet, True/False = cached result of the NVIDIA probe.
_NVIDIA_AVAILABLE: bool | None = None
_NVIDIA_RETRY_AT: float = 0.0


def _invalidate_nvidia_probe() -> None:
    """Forget the cached NVIDIA answer so the next call probes again."""
    global _NVIDIA_AVAILABLE, _NVIDIA_RETRY_AT
    _NVIDIA_AVAILABLE = None
    _NVIDIA_RETRY_AT = 0.0


def _has_nvidia_sensors() -> bool:
    """Return whether gpustat could query the NVIDIA driver.

    The result is cached so the poll loop does not re-run the NVML query —
    and does not keep logging the same complaint — every cycle on machines
    without an NVIDIA GPU. A negative answer is retried after
    ``NVIDIA_RETRY_INTERVAL_S`` so a driver loaded later is picked up.
    """
    global _NVIDIA_AVAILABLE, _NVIDIA_RETRY_AT

    now = time.monotonic()
    needs_probe = _NVIDIA_AVAILABLE is None or (
        not _NVIDIA_AVAILABLE and now >= _NVIDIA_RETRY_AT
    )

    if needs_probe:
        try:
            _NVIDIA_AVAILABLE = bool(gpustat.new_query())
        except OSError as e:
            log.debug("No NVIDIA sensors available: %s", e)
            _NVIDIA_AVAILABLE = False
        except NVMLError as e:
            log.debug("No NVIDIA driver available: %s", e)
            _NVIDIA_AVAILABLE = False
        _NVIDIA_RETRY_AT = now + NVIDIA_RETRY_INTERVAL_S

    return _NVIDIA_AVAILABLE


def _get_temp_devices() -> dict[str, float] | None:
    try:
        hw_temperatures = psutil.sensors_temperatures()
    except AttributeError:
        log.warning("Temperature monitoring not supported on this system.")
        return None

    temperature_sensors: dict[str, float] = {}
    for device_name, entries in hw_temperatures.items():
        for index, entry in enumerate(entries):
            # A single device may have multiple sensors, need to be able
            # to access each one
            device_key = f"{device_name}-{index}"
            temperature_sensors[device_key] = entry.current

    # Append any NVIDIA sensors (probe cached; a failure here must not take
    # the daemon down, so fall back to the CPU/other sensors).
    gpu_stats = None
    if _has_nvidia_sensors():
        try:
            gpu_stats = gpustat.new_query()
        except (OSError, NVMLError) as e:
            log.debug("GPU temperature query failed, continuing without: %s", e)
            _invalidate_nvidia_probe()
    if gpu_stats:
        for index, gpu_stat in enumerate(gpu_stats):
            if "name" in gpu_stat.keys() and "temperature.gpu" in gpu_stat.keys():
                device_name = gpu_stat["name"].replace(" ", "-")
                device_key = f"{device_name}-{index}"
                temperature_sensors[device_key] = gpu_stat["temperature.gpu"]

    return temperature_sensors


def print_temp_devices() -> None:
    """Print available temperature sensors by key and current temperature."""
    temps = _get_temp_devices()
    if not temps:
        print("No temperature sensors found.")
        return

    format_whitespace = len(max(temps.keys(), key=len)) + 10
    print("{key:{whitespace}} {value}".format(key="DEVICE KEY", whitespace=format_whitespace, value="CURRENT TEMPERATURE"))
    for device_key, temp in temps.items():
        print("{key:{whitespace}} {value}°C".format(key=device_key, whitespace=format_whitespace, value=temp))
