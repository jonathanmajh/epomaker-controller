"""CLI smoke tests using Click's CliRunner (no device required)."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from epomakercontroller.cli import cli, open_controller


def test_cli_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "set-keys" in result.output
    assert "upload-image" in result.output


def test_show_keymap_filter(main_config, monkeypatch) -> None:
    # Ensure CLI uses isolated/default package config already loaded at import
    # is fine; show-keymap only needs dry_run controller + keymap JSON.
    monkeypatch.setattr("epomakercontroller.cli.CONFIG_MAIN", main_config)
    runner = CliRunner()
    result = runner.invoke(cli, ["show-keymap", "--filter", "enter"])
    assert result.exit_code == 0
    assert "ENTER" in result.output.upper() or "enter" in result.output.lower()


def test_open_controller_dry_run_context(main_config, monkeypatch) -> None:
    monkeypatch.setattr("epomakercontroller.cli.CONFIG_MAIN", main_config)
    with open_controller(dry_run=True) as ctl:
        assert ctl.dry_run is True
    assert ctl.device is None


def test_send_time_direct_uses_open_controller(main_config, monkeypatch) -> None:
    """--direct keeps the old in-process path for debugging."""
    monkeypatch.setattr("epomakercontroller.cli.CONFIG_MAIN", main_config)
    mock_ctl = MagicMock()
    mock_ctl.open_device.return_value = True
    # Make ``with controller:`` behave like EpomakerController context manager.
    mock_ctl.__enter__.return_value = mock_ctl
    mock_ctl.__exit__.side_effect = lambda *a: mock_ctl.close_device()

    with patch("epomakercontroller.cli.EpomakerController", return_value=mock_ctl):
        runner = CliRunner()
        result = runner.invoke(cli, ["--direct", "send-time"])

    assert result.exit_code == 0, result.output
    mock_ctl.open_device.assert_called_once()
    mock_ctl.send_time.assert_called_once()
    mock_ctl.close_device.assert_called()
    assert "Time sent successfully" in result.output


def test_send_time_uses_backend_by_default(main_config, monkeypatch) -> None:
    monkeypatch.setattr("epomakercontroller.cli.CONFIG_MAIN", main_config)
    backend = MagicMock()

    with patch("epomakercontroller.cli.connect_backend", return_value=backend):
        result = CliRunner().invoke(cli, ["send-time"])

    assert result.exit_code == 0, result.output
    backend.send_time.assert_called_once()
    backend.close.assert_called_once()
    assert "Time sent successfully" in result.output


def test_start_daemon_interval_lower_bound(main_config, monkeypatch) -> None:
    """--interval below the hardware limit is rejected."""
    monkeypatch.setattr("epomakercontroller.cli.CONFIG_MAIN", main_config)
    runner = CliRunner()
    result = runner.invoke(cli, ["start-daemon", "--interval", "0.2"])
    assert result.exit_code != 0
    assert "1.6" in result.output


def test_start_daemon_reads_temp_key_from_env(main_config, monkeypatch) -> None:
    """EPOMAKER_TEMP_KEY supplies TEMP_KEY, so the unit needs no edit."""
    monkeypatch.setattr("epomakercontroller.cli.CONFIG_MAIN", main_config)

    with patch("epomakercontroller.cli.run_daemon", return_value=0) as run:
        result = CliRunner().invoke(
            cli, ["start-daemon"], env={"EPOMAKER_TEMP_KEY": "coretemp-0"}
        )

    assert result.exit_code == 0, result.output
    assert run.call_args.kwargs["temp_key"] == "coretemp-0"
    assert run.call_args.kwargs["stats"] is True


def test_start_daemon_exits_nonzero_on_failure(main_config, monkeypatch) -> None:
    """A fatal error must exit non-zero so systemd can restart the unit."""
    monkeypatch.setattr("epomakercontroller.cli.CONFIG_MAIN", main_config)

    with patch(
        "epomakercontroller.cli.run_daemon", side_effect=RuntimeError("no device")
    ):
        result = CliRunner().invoke(cli, ["start-daemon"])

    assert result.exit_code == 1
    assert "no device" in result.output


def test_start_daemon_retries_forever_by_default(main_config, monkeypatch) -> None:
    monkeypatch.setattr("epomakercontroller.cli.CONFIG_MAIN", main_config)

    with patch("epomakercontroller.cli.run_daemon", return_value=0) as run:
        result = CliRunner().invoke(cli, ["start-daemon"])

    assert result.exit_code == 0, result.output
    assert run.call_args.kwargs["reconnect_attempts"] == 0
    assert run.call_args.kwargs["stats"] is True


def test_start_daemon_reconnect_attempts_option(main_config, monkeypatch) -> None:
    monkeypatch.setattr("epomakercontroller.cli.CONFIG_MAIN", main_config)

    with patch("epomakercontroller.cli.run_daemon", return_value=0) as run:
        result = CliRunner().invoke(
            cli, ["start-daemon", "--reconnect-attempts", "3"]
        )

    assert result.exit_code == 0, result.output
    assert run.call_args.kwargs["reconnect_attempts"] == 3


def test_serve_defaults_to_no_stats_and_idle_exit(main_config, monkeypatch) -> None:
    monkeypatch.setattr("epomakercontroller.cli.CONFIG_MAIN", main_config)

    with patch("epomakercontroller.cli.run_daemon", return_value=0) as run:
        result = CliRunner().invoke(cli, ["serve"])

    assert result.exit_code == 0, result.output
    assert run.call_args.kwargs["stats"] is False
    assert run.call_args.kwargs["idle_timeout_s"] == 600.0


def test_start_daemon_reconnect_attempts_rejects_negative(
    main_config, monkeypatch
) -> None:
    monkeypatch.setattr("epomakercontroller.cli.CONFIG_MAIN", main_config)
    result = CliRunner().invoke(cli, ["start-daemon", "--reconnect-attempts", "-1"])
    assert result.exit_code != 0


def _daemon_mock_controller() -> MagicMock:
    mock_ctl = MagicMock()
    mock_ctl.__enter__.return_value = mock_ctl

    def _close(*_a):
        mock_ctl.close_device()
        return False  # don't swallow exceptions, like the real __exit__

    mock_ctl.__exit__.side_effect = _close
    return mock_ctl


def test_open_controller_connect_false_does_not_open(main_config, monkeypatch) -> None:
    monkeypatch.setattr("epomakercontroller.cli.CONFIG_MAIN", main_config)
    mock_ctl = _daemon_mock_controller()

    with patch("epomakercontroller.cli.EpomakerController", return_value=mock_ctl):
        with open_controller(connect=False):
            pass

    mock_ctl.open_device.assert_not_called()
