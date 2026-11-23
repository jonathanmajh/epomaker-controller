"""Controller dry-run and capability routing tests (no real HID traffic)."""

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


def test_send_dynatab_frames_uses_iface1_live(dynatab_main_config) -> None:
    ctl = EpomakerController(dynatab_main_config)
    sent: list[bytes] = []
    writes: list[bytes] = []

    class FakeDevice:
        def send_feature_report(self, data) -> None:
            sent.append(bytes(data))

        def write(self, data) -> None:
            writes.append(bytes(data))

        def close(self) -> None:
            pass

    ctl._opened = True
    ctl.device = FakeDevice()

    with patch("epomakercontroller.epomakercontroller.time.sleep"):
        ctl.send_dynatab_frames([[(0, 0, 0)] * 540], delay_ms=1)

    # 1 init (0xA9) + 29 data (0x29) reports, all 64 bytes.
    assert len(sent) == 30
    assert all(len(report) == 64 for report in sent)
    assert sent[0][0] == 0xA9
    assert all(report[0] == 0x29 for report in sent[1:])
    # The live path must never write to iface 0.
    assert writes == []


def test_send_dynatab_frames_flash_uses_legacy_path(
    dynatab_main_config, monkeypatch
) -> None:
    ctl = EpomakerController(dynatab_main_config)
    calls = []
    monkeypatch.setattr(
        ctl,
        "_upload_dynatab_command",
        lambda command, progress=None: calls.append(command),
    )
    ctl.send_dynatab_frames([[(0, 0, 0)] * 540], delay_ms=1, flash=True)
    assert len(calls) == 1


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


def test_daemon_resets_cache_after_clock_resync(main_config) -> None:
    """A time packet can redraw the screen, so cached values must be re-sent."""
    ctl = EpomakerController(main_config, dry_run=True)
    sent: list[str] = []

    real_send_time = ctl.send_time
    ctl.send_time = lambda *a, **k: (sent.append("time"), real_send_time())[1]  # type: ignore[method-assign]
    ctl.send_cpu = lambda value, **kw: sent.append(f"cpu{kw.get('skip_unchanged')}") or True  # type: ignore[method-assign]

    with patch("epomakercontroller.epomakercontroller.get_cpu_usage", return_value=42):
        ctl.start_daemon(
            None,
            False,
            poll_interval_s=0,
            clock_resync_s=1e-9,
            _max_cycles=1,
        )

    # Startup time, resync time, then a cpu send that was not skipped.
    assert sent == ["time", "time", "cpuTrue"]


def test_initial_clock_failure_does_not_kill_daemon(main_config) -> None:
    """The first send_time() is retried, not fatal."""
    from epomakercontroller.exceptions import DeviceCommunicationError

    ctl = EpomakerController(main_config, dry_run=True)
    state = {"failed": False}

    real_send_time = ctl.send_time

    def flaky_time(*a, **k):
        if not state["failed"]:
            state["failed"] = True
            raise DeviceCommunicationError("not ready")
        return real_send_time(*a, **k)

    ctl.send_time = flaky_time  # type: ignore[method-assign]
    ctl.send_cpu = lambda *a, **k: True  # type: ignore[method-assign]

    with patch("epomakercontroller.epomakercontroller.get_cpu_usage", return_value=1):
        ctl.start_daemon(
            None, False, poll_interval_s=0, reconnect_min_delay_s=0, _max_cycles=1
        )

    assert state["failed"] is True


def _stub_daemon_io(ctl: EpomakerController) -> None:
    """Make the daemon's HID writes no-ops so only control flow is tested."""
    ctl.send_cpu = lambda *a, **k: True  # type: ignore[method-assign]
    ctl.send_temperature = lambda *a, **k: True  # type: ignore[method-assign]


def test_reconnect_attempts_zero_retries_forever(main_config) -> None:
    """With max_attempts=0 a long outage is waited out, with capped backoff."""
    from epomakercontroller.exceptions import DeviceCommunicationError

    ctl = EpomakerController(main_config, dry_run=True)
    ctl.send_time = lambda *a, **k: None  # type: ignore[method-assign]
    failures = {"n": 1}

    def flaky_cpu(value: int, **kwargs: object) -> bool:
        if failures["n"]:
            failures["n"] -= 1
            raise DeviceCommunicationError("unplugged")
        return True

    ctl.send_cpu = flaky_cpu  # type: ignore[method-assign]
    ctl.send_temperature = lambda *a, **k: True  # type: ignore[method-assign]

    # Keyboard stays away for 7 attempts, far past the old limit of 5.
    open_results = [DeviceNotOpenError("gone")] * 7 + [True]
    sleeps: list[float] = []

    with patch.object(ctl, "open_device", side_effect=open_results) as mock_open:
        with patch("epomakercontroller.epomakercontroller.time.sleep", sleeps.append):
            with patch(
                "epomakercontroller.epomakercontroller.get_cpu_usage", return_value=3
            ):
                ctl.start_daemon(
                    None,
                    False,
                    poll_interval_s=0,
                    reconnect_attempts=0,
                    reconnect_min_delay_s=1,
                    reconnect_max_delay_s=4,
                    _max_cycles=1,
                )

    assert mock_open.call_count == 8
    assert sleeps == [1, 2, 4, 4, 4, 4, 4]  # doubles, then stays at the cap


def test_reconnect_attempts_nonzero_still_gives_up(main_config) -> None:
    """A non-zero limit keeps the old behaviour: raise after N attempts."""
    ctl = EpomakerController(main_config, dry_run=True)
    with patch.object(
        ctl, "open_device", side_effect=DeviceNotOpenError("gone")
    ) as mock_open:
        with patch("epomakercontroller.epomakercontroller.time.sleep"):
            with pytest.raises(DeviceNotOpenError):
                ctl._reopen_with_backoff(3, 1, 4)
    assert mock_open.call_count == 3


def test_daemon_waits_for_keyboard_at_startup(main_config) -> None:
    """wait_for_device: a keyboard that isn't plugged in yet is waited for."""
    ctl = EpomakerController(main_config)  # not a dry run: really "opens"
    ctl.send_time = lambda *a, **k: None  # type: ignore[method-assign]
    _stub_daemon_io(ctl)
    open_results = [DeviceNotOpenError("absent")] * 2 + [True]

    with patch.object(ctl, "open_device", side_effect=open_results) as mock_open:
        with patch("epomakercontroller.epomakercontroller.time.sleep"):
            with patch(
                "epomakercontroller.epomakercontroller.get_cpu_usage", return_value=3
            ):
                ctl.start_daemon(
                    None,
                    False,
                    poll_interval_s=0,
                    reconnect_attempts=0,
                    wait_for_device=True,
                    _max_cycles=1,
                )

    assert mock_open.call_count == 3


def test_daemon_does_not_open_device_unless_asked(main_config) -> None:
    """Without wait_for_device the daemon assumes the caller opened it."""
    ctl = EpomakerController(main_config)
    ctl.send_time = lambda *a, **k: None  # type: ignore[method-assign]
    _stub_daemon_io(ctl)

    with patch.object(ctl, "open_device") as mock_open:
        with patch(
            "epomakercontroller.epomakercontroller.get_cpu_usage", return_value=3
        ):
            ctl.start_daemon(None, False, poll_interval_s=0, _max_cycles=1)

    mock_open.assert_not_called()


def test_sync_clock_quietly_reports_success_and_failure(main_config) -> None:
    from epomakercontroller.exceptions import DeviceCommunicationError

    ctl = EpomakerController(main_config, dry_run=True)
    assert ctl._sync_clock_quietly() is True

    def boom(*a, **k):
        raise DeviceCommunicationError("nope")

    ctl.send_time = boom  # type: ignore[method-assign]
    assert ctl._sync_clock_quietly() is False


def test_clock_sync_failed_after_reconnect_is_retried_next_cycle(main_config) -> None:
    """A clock send that fails right after a reconnect is retried promptly,
    not left for the hourly resync (which is disabled here to prove it)."""
    from epomakercontroller.exceptions import DeviceCommunicationError

    ctl = EpomakerController(main_config, dry_run=True)
    time_calls = {"n": 0}

    def flaky_time(*a, **k) -> None:
        time_calls["n"] += 1
        # 1 = startup (ok), 2 = quiet sync after the reconnect (fails),
        # 3 = the retry at the top of the next iteration (ok).
        if time_calls["n"] == 2:
            raise DeviceCommunicationError("not ready yet")

    ctl.send_time = flaky_time  # type: ignore[method-assign]
    cpu_state = {"failed": False}

    def cpu_fails_once(value: int, **kwargs: object) -> bool:
        if not cpu_state["failed"]:
            cpu_state["failed"] = True
            raise DeviceCommunicationError("cable yanked")
        return True

    ctl.send_cpu = cpu_fails_once  # type: ignore[method-assign]
    ctl.send_temperature = lambda *a, **k: True  # type: ignore[method-assign]

    with patch("epomakercontroller.epomakercontroller.get_cpu_usage", return_value=7):
        ctl.start_daemon(
            None,
            False,
            poll_interval_s=0,
            reconnect_min_delay_s=0,
            clock_resync_s=0,
            _max_cycles=1,
        )

    assert time_calls["n"] == 3
