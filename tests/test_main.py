"""Tests for emrg/__main__.py argument parsing and `emrg stop` process matching."""
from emrg.__main__ import (
    _build_parser,
    _match_emrg_client,
    _scan_emrg_client_pids,
)


class TestBuildParser:
    def test_no_args(self):
        parser = _build_parser()
        args = parser.parse_args([])
        assert args.command is None
        assert args.init_auto_evolve is False

    def test_init_auto_evolve(self):
        parser = _build_parser()
        args = parser.parse_args(["--init-auto-evolve"])
        assert args.init_auto_evolve is True

    def test_server_command(self):
        parser = _build_parser()
        args = parser.parse_args(["server"])
        assert args.command == "server"
        assert args.server_action is None

    def test_server_stop(self):
        parser = _build_parser()
        args = parser.parse_args(["server", "stop"])
        assert args.command == "server"
        assert args.server_action == "stop"

    def test_server_restart(self):
        parser = _build_parser()
        args = parser.parse_args(["server", "restart"])
        assert args.command == "server"
        assert args.server_action == "restart"

    def test_update_command(self):
        parser = _build_parser()
        args = parser.parse_args(["update"])
        assert args.command == "update"

    def test_rant_with_message(self):
        parser = _build_parser()
        args = parser.parse_args(["rant", "this", "is", "a", "test"])
        assert args.command == "rant"
        assert args.message == ["this", "is", "a", "test"]
        assert args.project is None

    def test_rant_with_project(self):
        parser = _build_parser()
        args = parser.parse_args(["rant", "-p", "emrg", "hello", "world"])
        assert args.command == "rant"
        assert args.project == "emrg"
        assert args.message == ["hello", "world"]

    def test_rant_with_long_project_flag(self):
        parser = _build_parser()
        args = parser.parse_args(["rant", "--project", "emrg", "hello"])
        assert args.command == "rant"
        assert args.project == "emrg"
        assert args.message == ["hello"]

    def test_stop_command(self):
        parser = _build_parser()
        args = parser.parse_args(["stop"])
        assert args.command == "stop"
        assert args.skip_gui is False

    def test_stop_command_skip_gui(self):
        """emrg stop --skip-gui (rant 2026-08-21T12:44:34): the GUI's
        restart-to-apply calls the CLI so the install launcher's PYTHONPATH
        makes `emrg` importable for the packaged app."""
        parser = _build_parser()
        args = parser.parse_args(["stop", "--skip-gui"])
        assert args.command == "stop"
        assert args.skip_gui is True


class TestMatchEmrgClient:
    """`emrg stop` process matching — positive and negative states."""

    def test_matches_tui(self):
        assert _match_emrg_client("python -m emrg")
        assert _match_emrg_client("python -m emrg --init-auto-evolve")

    def test_matches_daemon(self):
        assert _match_emrg_client("python -m emrg.server")
        assert _match_emrg_client("pythonw -m emrg.server")

    def test_matches_macos_gui(self):
        assert _match_emrg_client("/Applications/EMRG.app/Contents/MacOS/EMRG")
        assert _match_emrg_client("EMRG.app/Contents/MacOS/EMRG --no-sandbox")

    def test_does_not_match_lookalikes(self):
        # module-like but different module: must not match
        assert not _match_emrg_client("python -m emrg.serverless")
        assert not _match_emrg_client("python -m emrgx")
        # unrelated processes
        assert not _match_emrg_client("git fetch origin master")
        assert not _match_emrg_client("python -m pytest tests/")
        assert not _match_emrg_client("EMRGX.app/Contents/MacOS/EMRGX")
        # `-m emrg` as substring of a longer flag (e.g. -X something) must not match
        assert not _match_emrg_client("python -X dev main.py -m emrgistry")


class TestScanEmrgClientPids:
    def test_parses_and_excludes_own_pid(self):
        ps_out = (
            "  100 /usr/bin/python -m emrg\n"
            "  200 /usr/bin/python -m emrg.server\n"
            "  300 /usr/bin/python -m pytest\n"
            "  400 /Applications/EMRG.app/Contents/MacOS/EMRG\n"
        )
        pids = _scan_emrg_client_pids(ps_out, own_pid=200)
        # 100 (TUI) + 400 (GUI) matched; 200 excluded as own pid; 300 unrelated
        assert pids == [100, 400]

    def test_empty_and_malformed_lines(self):
        ps_out = "  100 /usr/bin/python -m emrg\n\n   \nnot-a-pid  /bin/ls\n"
        pids = _scan_emrg_client_pids(ps_out, own_pid=9999)
        assert pids == [100]

    def test_no_matches(self):
        assert _scan_emrg_client_pids("  1 /sbin/launchd\n  2 /usr/libexec/foo\n", own_pid=9999) == []
