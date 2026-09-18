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

    def test_sid_full_in_title_form(self):
        """Full session id is shown in the title form (rant 2026-08-13T16:14:12).

        Host request: when a session name exists, the status bar must show the
        complete session id (previously truncated to 8 chars, inconsistent with
        the no-title form which always showed the full id).
        """
        from emrg.client.app import _format_status_left

        out = _format_status_left("main", "s_260727abcdef", "")
        assert "main (s_260727abcdef)" in out


class TestEffectiveVisionIsVisible:
    """The status bar shows the EFFECTIVE image capability, not the declaration.

    Rant 2026-09-17T16:53:02: the daemon decides vision by a priority rule
    (the model entry's own key, else the top-level `[llm] vision` default) and
    that value moves on every `/model` switch — while the only thing a host
    could see was `config.toml`'s static declaration, so the way to learn the
    truth was to send an image and read the refusal. Both directions are pinned
    here because the silent failure is symmetric: a vision model degraded to
    text and a text-only model handed an image are indistinguishable from the
    outside, and neither announces itself.
    """

    def test_the_effective_vision_is_shown_when_it_is_true(self):
        from emrg.client.app import _format_status_left

        out = _format_status_left("main", "s_260727", "gpt-4o", True)
        assert out.endswith("[gpt-4o img]"), out

    def test_the_effective_vision_is_shown_when_it_is_false(self):
        """The other direction, and the one the host actually hit: a value of
        False must be as visible as True — an absent marker would read as
        "unknown", which is exactly the ambiguity being removed."""
        from emrg.client.app import _format_status_left

        out = _format_status_left("main", "s_260727", "deepseek-chat", False)
        assert out.endswith("[deepseek-chat no-img]"), out

    def test_an_unreported_vision_leaves_the_segment_unchanged(self):
        """`None` = this server never said (an older daemon): no marker, and the
        segment is byte-identical to what it printed before this change."""
        from emrg.client.app import _format_status_left

        out = _format_status_left("main", "s_260727", "deepseek-v4-flash", None)
        assert out.endswith("[deepseek-v4-flash]"), out
        assert out == _format_status_left("main", "s_260727", "deepseek-v4-flash")

    def test_no_model_means_no_vision_marker_either(self):
        """A capability with no model to attach to is not a capability: an empty
        model segment must not gain a marker (and must not print brackets)."""
        from emrg.client.app import _format_status_left

        out = _format_status_left("main", "s_260727", "", True)
        assert "[" not in out, out
