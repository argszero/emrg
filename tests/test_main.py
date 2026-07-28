"""Tests for emrg/__main__.py argument parsing."""
from emrg.__main__ import _build_parser


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
