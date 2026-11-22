"""Controller dry-run and capability routing tests (no real HID traffic)."""

import logging
from unittest.mock import patch

import pytest

from epomakercontroller.commands.EpomakerKeyRGBCommand import (
    KeyboardRGBFrame,
    KeyMap,
)
from epomakercontroller.commands.data.constants import (
    CAPABILITY_DYNATAB_SCREEN,
    CAPABILITY_RT100_SCREEN,
    ERASE_DELAY_S,
    PACKET_DELAY_S,
)
from epomakercontroller.epomakercontroller import EpomakerController
from epomakercontroller.exceptions import (
    DeviceNotOpenError,
    ProtocolError,
    UnsupportedImageError,
)
from epomakercontroller.utils.keyboard_keys import KeyboardKeys


def test_has_capability(main_config) -> None:
    ctl = EpomakerController(main_config, dry_run=True)
    # Default package config is RT100 capabilities
    assert ctl.has_capability("per_key_rgb")
    assert ctl.has_capability(CAPABILITY_RT100_SCREEN)
    assert not ctl.has_capability(CAPABILITY_DYNATAB_SCREEN)


def test_dynatab_config_capabilities(dynatab_main_config) -> None:
    ctl = EpomakerController(dynatab_main_config, dry_run=True)
    assert ctl.has_capability(CAPABILITY_DYNATAB_SCREEN)
    assert not ctl.has_capability(CAPABILITY_RT100_SCREEN)


def test_send_keys_applies_pacing(main_config) -> None:
    ctl = EpomakerController(main_config, dry_run=True)
    keys = KeyboardKeys(ctl.config_keymap)
    mapping = KeyMap(keys)
    for key in keys:
        mapping[key] = (1, 2, 3)
    frame = KeyboardRGBFrame(key_map=mapping)

    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with patch("epomakercontroller.epomakercontroller.time.sleep", fake_sleep):
        ctl.send_keys([frame])

    # 1 erase delay after first report + 7 packet delays after data reports
    assert sleeps[0] == ERASE_DELAY_S
    assert sleeps[1:] == [PACKET_DELAY_S] * 7
    assert len(sleeps) == 8


def test_send_dynatab_frames_dry_run(dynatab_main_config) -> None:
    ctl = EpomakerController(dynatab_main_config, dry_run=True)
    ctl.send_dynatab_frames([[(0, 0, 0)] * 540], delay_ms=80)


def test_send_image_no_capability_raises(main_config) -> None:
    data = dict(main_config.data)
    data["CAPABILITIES"] = ["per_key_rgb"]  # no screen
    main_config.data = data
    ctl = EpomakerController(main_config, dry_run=True)
    # Re-apply after constructor (constructor already read CAPABILITIES)
    ctl.capabilities = {"per_key_rgb"}
    with pytest.raises(ProtocolError, match="No screen upload capability"):
        ctl.send_image("/tmp/anything.png")


def test_send_image_rt100_bad_extension(main_config, tmp_path) -> None:
    ctl = EpomakerController(main_config, dry_run=True)
    assert ctl.has_capability(CAPABILITY_RT100_SCREEN)
    bad = tmp_path / "x.xyz"
    bad.write_bytes(b"nope")
    with pytest.raises(UnsupportedImageError):
        ctl.send_image(str(bad))


def test_context_manager_closes_device(main_config) -> None:
    ctl = EpomakerController(main_config, dry_run=True)
    with ctl:
        assert ctl.open_device()
        # dry_run leaves a placeholder hid.device object
        assert ctl.device is not None
    # __exit__ always calls close_device
    assert ctl.device is None


def test_context_manager_closes_on_exception(main_config) -> None:
    ctl = EpomakerController(main_config, dry_run=True)
    with pytest.raises(RuntimeError, match="boom"):
        with ctl:
            assert ctl.open_device()
            raise RuntimeError("boom")
    assert ctl.device is None


def test_signal_handlers_opt_in(main_config, monkeypatch) -> None:
    calls: list[tuple] = []

    def fake_signal(sig, handler):
        calls.append((sig, handler))

    monkeypatch.setattr(
        "epomakercontroller.epomakercontroller.signal.signal", fake_signal
    )
    EpomakerController(main_config, dry_run=True)
    assert calls == []

    EpomakerController(
        main_config, dry_run=True, install_signal_handlers=True
    )
    assert len(calls) == 2  # SIGINT + SIGTERM


def test_send_cpu_skips_unchanged(main_config) -> None:
    ctl = EpomakerController(main_config, dry_run=True)
    sent: list[object] = []
    ctl._send_command = lambda cmd, **kw: sent.append(cmd)  # type: ignore[method-assign]

    assert ctl.send_cpu(10) is True
    assert ctl.send_cpu(10, skip_unchanged=True) is False
    assert ctl.send_cpu(11, skip_unchanged=True) is True
    assert ctl.send_cpu(10) is True  # opt-in: unchanged value still sent
    assert len(sent) == 3


def test_send_temperature_skips_unchanged(main_config) -> None:
    ctl = EpomakerController(main_config, dry_run=True)
    sent: list[object] = []
    ctl._send_command = lambda cmd, **kw: sent.append(cmd)  # type: ignore[method-assign]

    assert ctl.send_temperature(40) is True
    assert ctl.send_temperature(40, skip_unchanged=True) is False
    assert ctl.send_temperature(None) is False
    assert len(sent) == 1


def _run_one_daemon_iteration(ctl, test_mode: bool) -> list[dict]:
    """Run start_daemon for a single cycle and return the calls it made."""
    calls: list[dict] = []

    def fake_cpu(value: int, **kwargs: object) -> bool:
        calls.append({"kind": "cpu", "value": value, **kwargs})
        return True

    def fake_temp(value: int, **kwargs: object) -> bool:
        calls.append({"kind": "temp", "value": value, **kwargs})
        return True

    ctl.send_cpu = fake_cpu  # type: ignore[method-assign]
    ctl.send_temperature = fake_temp  # type: ignore[method-assign]

    with patch("epomakercontroller.epomakercontroller.get_cpu_usage", return_value=5):
        with patch(
            "epomakercontroller.epomakercontroller.get_device_temp", return_value=30
        ):
            ctl.start_daemon(
                "coretemp-0", test_mode, poll_interval_s=0, _max_cycles=1
            )
    return calls


def test_daemon_skips_unchanged_for_real_values(main_config) -> None:
    """Values that haven't changed are not re-sent to the keyboard."""
    ctl = EpomakerController(main_config, dry_run=True)
    calls = _run_one_daemon_iteration(ctl, test_mode=False)
    assert calls[0] == {"kind": "cpu", "value": 5, "skip_unchanged": True}
    assert calls[1] == {"kind": "temp", "value": 30, "skip_unchanged": True}


def test_daemon_always_sends_in_test_mode(main_config) -> None:
    """Test mode sends random data every cycle, so nothing is skipped."""
    ctl = EpomakerController(main_config, dry_run=True)
    calls = _run_one_daemon_iteration(ctl, test_mode=True)
    assert [c["skip_unchanged"] for c in calls] == [False, False]


def test_daemon_reconnects_after_device_error(main_config) -> None:
    """A failed send closes the device, reopens it, and resumes the loop."""
    from epomakercontroller.exceptions import DeviceCommunicationError

    ctl = EpomakerController(main_config, dry_run=True)
    events: list[str] = []
    failures = {"n": 1}

    def flaky_cpu(value: int, **kwargs: object) -> bool:
        if failures["n"]:
            failures["n"] -= 1
            events.append("fail")
            raise DeviceCommunicationError("cable yanked")
        events.append("ok")
        return True

    ctl.send_cpu = flaky_cpu  # type: ignore[method-assign]
    ctl.send_temperature = lambda *a, **k: True  # type: ignore[method-assign]
    ctl.send_time = lambda *a, **k: events.append("time")  # type: ignore[method-assign]

    opens = {"n": 0}
    real_open = ctl.open_device

    def counting_open(*a, **k) -> bool:
        opens["n"] += 1
        return real_open(*a, **k)

    ctl.open_device = counting_open  # type: ignore[method-assign]

    with patch("epomakercontroller.epomakercontroller.get_cpu_usage", return_value=7):
        with patch(
            "epomakercontroller.epomakercontroller.get_device_temp", return_value=30
        ):
            ctl.start_daemon(
                "coretemp-0",
                False,
                poll_interval_s=0,
                reconnect_min_delay_s=0,
                _max_cycles=1,
            )

    assert events == ["time", "fail", "time", "ok"]
    assert opens["n"] == 1  # one reopen after the failure


def test_daemon_raises_when_reconnect_gives_up(main_config) -> None:
    """If the keyboard never comes back, the error propagates to the caller."""
    from epomakercontroller.exceptions import DeviceCommunicationError

    ctl = EpomakerController(main_config, dry_run=True)

    def always_fail(*a, **k):
        raise DeviceCommunicationError("gone")

    ctl.send_cpu = always_fail  # type: ignore[method-assign]

    with patch.object(ctl, "open_device", side_effect=DeviceNotOpenError("nope")):
        with pytest.raises(DeviceNotOpenError):
            ctl.start_daemon(
                None,
                False,
                poll_interval_s=0,
                reconnect_attempts=2,
                reconnect_min_delay_s=0,
            )


def test_daemon_resends_clock_periodically(main_config) -> None:
    """The clock is re-sent during the loop, not just once at startup."""
    ctl = EpomakerController(main_config, dry_run=True)
    times: list[object] = [1, 2, 3]
    ctl.send_time = lambda *a, **k: times.append("sent")  # type: ignore[method-assign]
    ctl.send_cpu = lambda *a, **k: True  # type: ignore[method-assign]
    ctl.send_temperature = lambda *a, **k: True  # type: ignore[method-assign]

    with patch("epomakercontroller.epomakercontroller.get_cpu_usage", return_value=7):
        ctl.start_daemon(
            None,
            False,
            poll_interval_s=0,
            clock_resync_s=1e-9,
            _max_cycles=2,
        )

    # One send at startup, then one before each of the two cycles.
    assert times.count("sent") == 3


def test_daemon_clock_resync_can_be_disabled(main_config) -> None:
    ctl = EpomakerController(main_config, dry_run=True)
    times: list[object] = [1, 2, 3]
    ctl.send_time = lambda *a, **k: times.append("sent")  # type: ignore[method-assign]
    ctl.send_cpu = lambda *a, **k: True  # type: ignore[method-assign]

    with patch("epomakercontroller.epomakercontroller.get_cpu_usage", return_value=7):
        ctl.start_daemon(None, False, poll_interval_s=0, clock_resync_s=0, _max_cycles=3)

    assert times.count("sent") == 1


def test_gpu_query_error_does_not_escape(main_config, monkeypatch) -> None:
    """A failing NVML query must not take the daemon down."""
    from epomakercontroller.utils import sensors

    monkeypatch.setattr(sensors, "_NVIDIA_AVAILABLE", None)
    monkeypatch.setattr(sensors, "_NVIDIA_RETRY_AT", 0.0)
    monkeypatch.setattr(sensors.psutil, "sensors_temperatures", lambda: {})

    calls = {"n": 0}

    def sometimes_broken(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            # First call (the probe) succeeds so the result is cached True.
            return [{"name": "GPU 0", "temperature.gpu": 50.0}]
        raise sensors.NVMLError("driver reset")

    monkeypatch.setattr(sensors.gpustat, "new_query", sometimes_broken)

    # Probe caches True...
    assert sensors._has_nvidia_sensors() is True
    # ...and the later failure is swallowed rather than propagated, leaving
    # just the non-GPU sensors (none, in this test).
    assert sensors._get_temp_devices() == {}

    # The failed probe is invalidated so a later poll retries.
    assert sensors._NVIDIA_AVAILABLE is None


def test_gpu_probe_retries_after_interval(main_config, monkeypatch) -> None:
    """A driver loaded after login is picked up without a daemon restart."""
    from epomakercontroller.utils import sensors

    clock = {"now": 1000.0}
    monkeypatch.setattr(sensors.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(sensors, "_NVIDIA_AVAILABLE", None)
    monkeypatch.setattr(sensors, "_NVIDIA_RETRY_AT", 0.0)

    calls = {"n": 0}

    def failing(*a, **k):
        calls["n"] += 1
        raise sensors.NVMLError("no driver")

    monkeypatch.setattr(sensors.gpustat, "new_query", failing)

    assert sensors._has_nvidia_sensors() is False
    assert calls["n"] == 1

    # Within the retry window the cached negative answer is reused.
    clock["now"] += sensors.NVIDIA_RETRY_INTERVAL_S / 2
    assert sensors._has_nvidia_sensors() is False
    assert calls["n"] == 1

    # After it, a fresh probe happens.
    clock["now"] += sensors.NVIDIA_RETRY_INTERVAL_S
    assert sensors._has_nvidia_sensors() is False
    assert calls["n"] == 2


def test_unknown_temp_key_warns_once(main_config, monkeypatch, caplog) -> None:
    """A typo'd sensor key warns once, not every poll cycle."""
    from epomakercontroller.utils import sensors

    monkeypatch.setattr(sensors, "_WARNED_TEMP_KEYS", set())
    monkeypatch.setattr(
        sensors, "_get_temp_devices", lambda: {"coretemp-0": 40.0}
    )

    with caplog.at_level(logging.WARNING, logger="epomakercontroller.utils.sensors"):
        assert sensors.get_device_temp("coretemp-1") == 0
        assert sensors.get_device_temp("coretemp-1") == 0

    warnings = [r for r in caplog.records if "not found" in r.getMessage()]
    assert len(warnings) == 1
    assert "coretemp-0" in warnings[0].getMessage()
