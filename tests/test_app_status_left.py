"""Unit tests for app._format_status_left — status bar left section.

Rant 2026-08-13T14:11:03: TUI status bar left segment must show the current
EMRG version number (e.g. ``v0.2.30 emrg-main (s_260727) [deepseek-v4-flash]``).
The formatter is module-level so it is unit-testable.
"""

import pytest


class TestFormatStatusLeft:
    def test_version_prefix(self):
        """Left status starts with v<version>."""
        from emrg.client.app import _format_status_left
        import emrg

        out = _format_status_left("main", "s_260727", "deepseek-v4-flash")
        assert out.startswith(f"v{emrg.__version__} ")

    def test_title_sid_model_layout(self):
        """Title (short sid) + [model] follows the version prefix."""
        from emrg.client.app import _format_status_left

        out = _format_status_left("main", "s_260727", "deepseek-v4-flash")
        assert "main (s_260727)" in out
        assert out.endswith("[deepseek-v4-flash]")

    def test_no_title_uses_sid(self):
        """Without a title, the raw short sid is shown."""
        from emrg.client.app import _format_status_left

        out = _format_status_left("", "s_260727", "")
        assert out.endswith("s_260727")

    def test_no_model_omits_brackets(self):
        """Without a model, no [..] section appears."""
        from emrg.client.app import _format_status_left

        out = _format_status_left("main", "s_260727", "")
        assert "[" not in out

    def test_sid_truncated_to_8_chars(self):
        """Long session ids are truncated to 8 chars in the title form."""
        from emrg.client.app import _format_status_left

        out = _format_status_left("main", "s_260727abcdef", "")
        assert "main (s_260727)" in out
