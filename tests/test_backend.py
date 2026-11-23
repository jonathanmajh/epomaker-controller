"""Backend selection, version-skew and daemon-start tests."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from epomakercontroller import backend as backend_mod
from epomakercontroller.backend import (
    DirectBackend,
    connect_backend,
)
from epomakercontroller.daemon import DaemonServer, DeviceWorker
from epomakercontroller.epomakercontroller import EpomakerController
from epomakercontroller.exceptions import (
    DaemonUnavailableError,
    ProtocolVersionError,
)
from epomakercontroller.ipc import (
    ensure_runtime_dir,
    get_socket_path,
    recv_message,
    send_message,
)


@pytest.fixture
def runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "runtime"
    monkeypatch.setenv("EPOMAKER_RUNTIME_DIR", str(path))
    return path


def test_direct_backend_dry_run(main_config) -> None:
    backend = DirectBackend(main_config, dry_run=True)
    backend.open()
    assert backend.connected is True
    backend.send_time()
    backend.send_cpu(10)
    backend.close()
    assert backend.connected is False


def test_direct_backend_passes_flash(main_config) -> None:
    backend = DirectBackend(main_config, dry_run=True)
    backend.controller = MagicMock()
    backend.send_dynatab_frames([[(0, 0, 0)] * 540], flash=True)
    backend.controller.send_dynatab_frames.assert_called_once()
    assert backend.controller.send_dynatab_frames.call_args.kwargs["flash"] is True


def test_connect_backend_without_daemon_raises(main_config, runtime_dir) -> None:
    with pytest.raises(DaemonUnavailableError):
        connect_backend(main_config, start=False)


def test_connect_backend_rejects_version_skew(main_config, runtime_dir) -> None:
    ensure_runtime_dir()
    path = get_socket_path()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def serve_once() -> None:
        conn, _ = listener.accept()
        with conn:
            message = recv_message(conn)
            send_message(
                conn,
                {
                    "id": message["id"],
                    "ok": True,
                    "result": {
                        "protocol": 999,
                        "connected": False,
                        "capabilities": [],
                    },
                },
            )

    thread = threading.Thread(target=serve_once, daemon=True)
    thread.start()
    try:
        with pytest.raises(ProtocolVersionError, match="Restart"):
            connect_backend(main_config, start=False)
    finally:
        thread.join(timeout=2)
        listener.close()


def test_connect_backend_starts_daemon(main_config, runtime_dir, monkeypatch) -> None:
    controller = EpomakerController(main_config, dry_run=True)
    worker = DeviceWorker(controller, stats=False)
    server = DaemonServer(worker, idle_timeout_s=5)
    thread = threading.Thread(target=server.serve_forever, daemon=True)

    started: list[bool] = []

    def fake_start() -> None:
        started.append(True)
        thread.start()

    monkeypatch.setattr(backend_mod, "start_daemon", fake_start)
    client = connect_backend(main_config, timeout=3)
    try:
        assert started == [True]
        assert client.connected is True
        client.send_time()
        client._request("shutdown")
    finally:
        client.close()
        server.stop()
        thread.join(timeout=3)


def test_start_via_systemd_when_unit_installed(tmp_path, monkeypatch) -> None:
    unit = tmp_path / "epomaker-daemon.service"
    unit.write_text("[Service]\n", encoding="utf-8")
    monkeypatch.setattr(backend_mod, "_unit_file", lambda: unit)
    monkeypatch.setattr(
        backend_mod.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return Result()

    monkeypatch.setattr(backend_mod.subprocess, "run", fake_run)
    assert backend_mod._start_via_systemd() is True
    assert calls == [["/usr/bin/systemctl", "--user", "start", "epomaker-daemon"]]


def test_spawn_daemon_uses_detached_serve(tmp_path, monkeypatch) -> None:
    log_path = tmp_path / "daemon.log"
    monkeypatch.setattr(backend_mod, "_log_path", lambda: log_path)
    calls: list[tuple[list[str], dict]] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            calls.append((list(argv), kwargs))

    monkeypatch.setattr(backend_mod.subprocess, "Popen", FakePopen)
    backend_mod._spawn_daemon()
    argv, kwargs = calls[0]
    assert argv[-1] == "serve"
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is backend_mod.subprocess.DEVNULL


def test_connect_existing_updates_capabilities(main_config, runtime_dir) -> None:
    controller = EpomakerController(main_config, dry_run=True)
    worker = DeviceWorker(controller, stats=False)
    server = DaemonServer(worker, idle_timeout_s=5)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not get_socket_path().exists():
        time.sleep(0.01)
    try:
        client = backend_mod._connect_existing(main_config)
        assert client is not None
        assert client.has_capability("rt100_screen")
        client._request("shutdown")
        client.close()
    finally:
        server.stop()
        thread.join(timeout=3)


def test_socket_backend_sends_keys(main_config, runtime_dir) -> None:
    from epomakercontroller.commands.EpomakerKeyRGBCommand import (
        KeyMap,
        KeyboardRGBFrame,
    )
    from epomakercontroller.utils.keyboard_keys import KeyboardKeys

    controller = EpomakerController(main_config, dry_run=True)
    worker = DeviceWorker(controller, stats=False)
    server = DaemonServer(worker, idle_timeout_s=5)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not get_socket_path().exists():
        time.sleep(0.01)
    try:
        client = backend_mod._connect_existing(main_config)
        assert client is not None
        keys = KeyboardKeys(client.config_keymap)
        mapping = KeyMap(keys)
        mapping.key_map[next(iter(keys))] = (1, 2, 3)
        client.send_keys([KeyboardRGBFrame(key_map=mapping)])
        client._request("shutdown")
        client.close()
    finally:
        server.stop()
        thread.join(timeout=3)
