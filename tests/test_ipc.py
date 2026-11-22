"""Tests for the daemon framing helpers."""

from __future__ import annotations

import socket

import pytest

from epomakercontroller import ipc
from epomakercontroller.exceptions import ProtocolError


def test_message_round_trip() -> None:
    left, right = socket.socketpair()
    try:
        message = {"id": 3, "command": "hello", "args": {"nested": [1, 2]}}
        ipc.send_message(left, message)
        assert ipc.recv_message(right) == message
    finally:
        left.close()
        right.close()


def test_message_too_large(monkeypatch) -> None:
    left, right = socket.socketpair()
    monkeypatch.setattr(ipc, "MAX_MESSAGE_BYTES", 10)
    try:
        with pytest.raises(ProtocolError):
            ipc.send_message(left, {"x": "longer than ten bytes"})
    finally:
        left.close()
        right.close()


def test_recv_rejects_malformed_json() -> None:
    left, right = socket.socketpair()
    try:
        payload = b"not json"
        left.sendall(ipc._HEADER.pack(len(payload)) + payload)
        with pytest.raises(ProtocolError):
            ipc.recv_message(right)
    finally:
        left.close()
        right.close()


def test_runtime_dir_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EPOMAKER_RUNTIME_DIR", str(tmp_path))
    assert ipc.get_runtime_dir() == tmp_path
    assert ipc.get_socket_path() == tmp_path / ipc.SOCKET_NAME
    assert ipc.ensure_runtime_dir() == tmp_path
    assert (tmp_path.stat().st_mode & 0o777) == 0o700
