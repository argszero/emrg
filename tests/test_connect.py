"""Tests for connect module — platform-adaptive IPC helpers."""

import os
from pathlib import Path

from emrg.connect import CONNECT_ID, get_server_path


class TestGetServerPath:
    """Tests for get_server_path — platform-adaptive socket path."""

    def test_unix_returns_socket_path(self, monkeypatch, tmp_path):
        """On Unix, returns path/to/emrgd.sock in config dir."""
        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setattr("emrg.connect.config_dir", lambda: tmp_path)

        result = get_server_path()
        expected = str(tmp_path / f"{CONNECT_ID}.sock")
        assert result == expected

    def test_windows_returns_named_pipe(self, monkeypatch):
        """On Windows, returns the named pipe path."""
        monkeypatch.setattr(os, "name", "nt")

        result = get_server_path()
        assert result == rf"\\.\pipe\{CONNECT_ID}"
        assert CONNECT_ID in result

    def test_connect_id_constant(self):
        """CONNECT_ID is the expected value."""
        assert CONNECT_ID == "emrgd"
