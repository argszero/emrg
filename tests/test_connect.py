"""Tests for connect module — WebSocket IPC connection layer (Phase 1)."""

from pathlib import Path

from emrg.connect import CONNECT_ID, AuthError, cleanup_server, get_server_path, is_server_running_sync


class TestGetServerPath:
    """Tests for get_server_path — daemon port/token file path."""

    def test_returns_port_file_path(self, monkeypatch, tmp_path):
        """Returns ~/.emrg/emrgd.port in the config dir."""
        monkeypatch.setattr("emrg.connect.config_dir", lambda: tmp_path)

        result = get_server_path()
        expected = str(tmp_path / f"{CONNECT_ID}.port")
        assert result == expected

    def test_connect_id_constant(self):
        """CONNECT_ID is the expected value."""
        assert CONNECT_ID == "emrgd"


class TestAuthError:
    def test_is_exception(self):
        assert issubclass(AuthError, Exception)


class TestCleanupServer:
    def test_removes_port_file(self, monkeypatch, tmp_path):
        """Removes the port file if present."""
        monkeypatch.setattr("emrg.connect.config_dir", lambda: tmp_path)
        port_file = tmp_path / f"{CONNECT_ID}.port"
        port_file.write_text("49152\ntoken", encoding="utf-8")

        cleanup_server()

        assert not port_file.exists()

    def test_noop_when_absent(self, monkeypatch, tmp_path):
        """Does nothing when the port file doesn't exist."""
        monkeypatch.setattr("emrg.connect.config_dir", lambda: tmp_path)

        cleanup_server()  # must not raise

    def test_leaves_other_files(self, monkeypatch, tmp_path):
        """Only removes the port file, not other config files."""
        monkeypatch.setattr("emrg.connect.config_dir", lambda: tmp_path)
        other = tmp_path / "config.toml"
        other.write_text("x", encoding="utf-8")
        port_file = tmp_path / f"{CONNECT_ID}.port"
        port_file.write_text("1\nt", encoding="utf-8")

        cleanup_server()

        assert other.exists()
        assert not port_file.exists()


class TestIsServerRunningSync:
    def test_false_when_port_file_missing(self, monkeypatch, tmp_path):
        """Returns False when the port file doesn't exist (daemon not started)."""
        monkeypatch.setattr("emrg.connect.config_dir", lambda: tmp_path)

        assert is_server_running_sync() is False

    def test_false_when_port_file_corrupt(self, monkeypatch, tmp_path):
        """Returns False when the port file is unparseable."""
        monkeypatch.setattr("emrg.connect.config_dir", lambda: tmp_path)
        (tmp_path / f"{CONNECT_ID}.port").write_text("garbage", encoding="utf-8")

        assert is_server_running_sync() is False

    def test_false_when_connection_refused(self, monkeypatch, tmp_path):
        """Returns False when nothing listens on the port."""
        monkeypatch.setattr("emrg.connect.config_dir", lambda: tmp_path)
        (tmp_path / f"{CONNECT_ID}.port").write_text("1\nno-token", encoding="utf-8")

        assert is_server_running_sync(timeout=0.1) is False
