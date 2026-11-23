"""Daemon worker/server tests.  All HID access is dry-run."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from epomakercontroller.backend import SocketBackend
from epomakercontroller.commands.EpomakerKeyRGBCommand import (
    KeyboardRGBFrame,
    KeyMap,
)
from epomakercontroller.daemon import (
    DaemonServer,
    DeviceWorker,
    validate_request,
)
from epomakercontroller.epomakercontroller import EpomakerController
from epomakercontroller.exceptions import DeviceNotOpenError, ProtocolError
from epomakercontroller.ipc import (
    PROTOCOL_VERSION,
    connect_socket,
    get_socket_path,
    recv_message,
    send_message,
)
from epomakercontroller.utils.keyboard_keys import KeyboardKeys


def _wait_for_socket(path: Path, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"socket {path} was not created")


@pytest.fixture
def runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "runtime"
    monkeypatch.setenv("EPOMAKER_RUNTIME_DIR", str(path))
    return path


@pytest.fixture
def running_server(main_config, runtime_dir):
    controller = EpomakerController(main_config, dry_run=True)
    worker = DeviceWorker(controller, stats=False)
    server = DaemonServer(worker, idle_timeout_s=10)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_for_socket(get_socket_path())
    try:
        yield server, worker
    finally:
        server.stop()
        thread.join(timeout=3)
        assert not thread.is_alive()


def _hello(path: Path) -> dict:
    sock = connect_socket(path)
    try:
        send_message(sock, {"id": 0, "command": "hello", "args": {}})
        return recv_message(sock)
    finally:
        sock.close()


def test_worker_status_and_send_time(main_config) -> None:
    worker = DeviceWorker(
        EpomakerController(main_config, dry_run=True), stats=False
    )
    worker.start()
    try:
        job = worker.submit("send_time", {})
        job.wait(timeout=2)
        assert job.error is None
        assert worker.status()["connected"] is True
    finally:
        worker.stop()


def test_worker_returns_device_unavailable_without_keyboard(main_config) -> None:
    controller = EpomakerController(main_config)

    # Pretend opening always fails, as if the keyboard were unplugged.
    def _fail_open(*_a, **_k):
        raise DeviceNotOpenError("absent")

    controller.open_device = _fail_open  # type: ignore[method-assign]
    worker = DeviceWorker(controller, stats=False)
    worker.start()
    try:
        job = worker.submit("send_time", {})
        job.wait(timeout=2)
        assert job.error is not None
        assert "device_unavailable" in str(job.error)
        assert worker.status()["state"] == "waiting"
    finally:
        worker.stop()


def test_server_hello_status_and_command(running_server, runtime_dir, main_config) -> None:
    server, _worker = running_server
    reply = _hello(get_socket_path())
    assert reply["ok"] is True
    assert reply["result"]["protocol"] == PROTOCOL_VERSION
    assert reply["result"]["connected"] is True

    sock = connect_socket(get_socket_path())
    backend = SocketBackend(sock, reply["result"], main_config)
    try:
        backend.send_time()
        assert backend.status()["state"] == "connected"
    finally:
        backend.close()


def test_server_rejects_invalid_screen_frames(running_server, runtime_dir) -> None:
    sock = connect_socket(get_socket_path())
    try:
        send_message(
            sock,
            {
                "id": 1,
                "command": "upload_screen",
                "args": {"frames": [[[0, 0, 0]]], "delay_ms": 100},
            },
        )
        reply = recv_message(sock)
    finally:
        sock.close()
    assert reply["ok"] is False
    assert reply["error"]["type"] == "ProtocolError"


def test_server_streams_upload_progress(running_server, runtime_dir, main_config) -> None:
    sock = connect_socket(get_socket_path())
    hello = _hello(get_socket_path())["result"]
    backend = SocketBackend(sock, hello, main_config)
    stages: list[str] = []
    try:
        backend.send_dynatab_frames(
            [[(0, 0, 0)] * 540],
            progress=lambda event: stages.append(event.get("stage", "")),
        )
    finally:
        backend.close()
    assert stages[0] == "uploading"
    assert "uploading" in stages
    assert stages[-1] == "done"
    # Live uploads must not use the legacy flash/reboot stages.
    assert "flashing" not in stages
    assert "applying" not in stages


def test_server_second_instance_exits(running_server, runtime_dir) -> None:
    server2 = DaemonServer(DeviceWorker.__new__(DeviceWorker))
    # A real worker would be started only after the duplicate check; use a
    # harmless object here because serve_forever must return before using it.
    assert server2.serve_forever() is False


def test_server_idle_exit(main_config, runtime_dir) -> None:
    worker = DeviceWorker(
        EpomakerController(main_config, dry_run=True), stats=False
    )
    server = DaemonServer(worker, idle_timeout_s=0.2)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    thread.join(timeout=3)
    assert not thread.is_alive()


def test_validate_request_ranges() -> None:
    validate_request("send_cpu", {"cpu": 100})
    with pytest.raises(ProtocolError):
        validate_request("send_cpu", {"cpu": 101})
    with pytest.raises(ProtocolError):
        validate_request("send_temperature", {"temperature": 100})
    with pytest.raises(ProtocolError):
        validate_request(
            "upload_screen",
            {"frames": [[[0, 0, 0]] * 540] * 16},
        )


def test_set_keys_round_trip(running_server, runtime_dir, main_config) -> None:
    sock = connect_socket(get_socket_path())
    hello = _hello(get_socket_path())["result"]
    backend = SocketBackend(sock, hello, main_config)
    try:
        keys = KeyboardKeys(backend.config_keymap)
        mapping = KeyMap(keys)
        for key in keys:
            mapping[key] = (1, 2, 3)
        backend.send_keys([KeyboardRGBFrame(key_map=mapping)])
    finally:
        backend.close()


def test_set_keys_latest_wins(main_config) -> None:
    controller = EpomakerController(main_config, dry_run=True)
    worker = DeviceWorker(controller, stats=False)
    worker.start()
    started = threading.Event()
    release = threading.Event()
    sent: list[object] = []

    def slow_send(frames):
        sent.append(frames)
        started.set()
        release.wait(2)

    controller.send_keys = slow_send  # type: ignore[method-assign]
    try:
        first = worker.submit("set_keys", {"frames": [{"keys": []}]})
        assert started.wait(2)
        others = [
            worker.submit("set_keys", {"frames": [{"keys": []}]})
            for _ in range(3)
        ]
        release.set()
        first.wait(2)
        for job in others:
            job.wait(2)
        # The in-flight frame and the newest frame are sent; the two queued
        # intermediate frames are dropped.
        assert len(sent) == 2
    finally:
        worker.stop()


def test_worker_reload_config(main_config, dynatab_main_config) -> None:
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return dynatab_main_config if calls["n"] == 1 else main_config

    controller = EpomakerController(main_config, dry_run=True)
    worker = DeviceWorker(controller, stats=False, config_loader=loader)
    worker.start()
    try:
        job = worker.submit("reload_config", {})
        job.wait(2)
        assert job.error is None
        assert worker.status()["capabilities"] == sorted(
            dynatab_main_config["CAPABILITIES"]
        )
    finally:
        worker.stop()


def test_worker_gives_up_after_reconnect_attempts(main_config) -> None:
    controller = EpomakerController(main_config, dry_run=True)

    def always_missing() -> bool:
        raise DeviceNotOpenError("missing")

    controller.open_device = always_missing  # type: ignore[method-assign]
    worker = DeviceWorker(
        controller,
        stats=False,
        reconnect_attempts=2,
        reconnect_min_delay_s=0.01,
        reconnect_max_delay_s=0.01,
    )
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and worker.fatal_error is None:
            time.sleep(0.02)
        assert worker.fatal_error is not None
        assert worker.connected is False
    finally:
        worker.stop()
