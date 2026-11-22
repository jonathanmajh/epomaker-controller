"""Small length-prefixed JSON protocol used by the Epomaker daemon.

The protocol is deliberately standard-library only: a 4-byte big-endian
length followed by a UTF-8 JSON object.  It is used over a Unix domain socket
in ``$XDG_RUNTIME_DIR`` so only the same user can talk to the daemon.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import tempfile
from pathlib import Path
from typing import Any

from .exceptions import ProtocolError

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
SOCKET_NAME = "daemon.sock"

_HEADER = struct.Struct(">I")


def get_runtime_dir() -> Path:
    """Return the directory that holds the daemon socket.

    ``EPOMAKER_RUNTIME_DIR`` is mainly useful for tests.  Otherwise the
    per-user XDG runtime directory is used, with a private temp directory as a
    fallback for systems without one.
    """
    override = os.environ.get("EPOMAKER_RUNTIME_DIR") or os.environ.get(
        "EPOMAKER_SOCKET_DIR"
    )
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "epomaker-controller"
    return Path(tempfile.gettempdir()) / f"epomaker-controller-{os.getuid()}"


def get_socket_path() -> Path:
    """Return the full path of the daemon socket."""
    return get_runtime_dir() / SOCKET_NAME


def ensure_runtime_dir() -> Path:
    """Create the runtime directory with private permissions."""
    runtime_dir = get_runtime_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(runtime_dir, 0o700)
    except OSError:
        pass
    return runtime_dir


def send_message(sock: socket.socket, message: dict[str, Any]) -> None:
    """Send one JSON message, prefixed with its length."""
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ProtocolError(
            f"Message too large ({len(payload)} > {MAX_MESSAGE_BYTES} bytes)"
        )
    sock.sendall(_HEADER.pack(len(payload)) + payload)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("daemon closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_message(sock: socket.socket) -> dict[str, Any]:
    """Receive one JSON message, enforcing the size limit."""
    header = _recv_exact(sock, _HEADER.size)
    (size,) = _HEADER.unpack(header)
    if size > MAX_MESSAGE_BYTES:
        raise ProtocolError(
            f"Message too large ({size} > {MAX_MESSAGE_BYTES} bytes)"
        )
    payload = _recv_exact(sock, size)
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtocolError(f"Malformed message: {e}") from e
    if not isinstance(message, dict):
        raise ProtocolError("Message must be a JSON object")
    return message


def peer_uid(sock: socket.socket) -> int | None:
    """Return the Unix UID on the other end, if the platform exposes it."""
    if not hasattr(socket, "SO_PEERCRED"):
        return None
    try:
        raw = sock.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
    except OSError:
        return None
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def connect_socket(path: Path | None = None, timeout: float = 1.0) -> socket.socket:
    """Connect to the daemon socket, raising OSError when it is not there."""
    socket_path = path or get_socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(socket_path))
    except BaseException:
        sock.close()
        raise
    return sock
