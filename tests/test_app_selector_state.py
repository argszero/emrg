"""Unit tests for app.SelectorState — validates the consolidated selector state.

These tests directly address rant #31: "nonlocal selector widget" — ensuring that
the SelectorState class works correctly with its three fields (active, widget, pending)
and that adding a new selector doesn't require remembering individual nonlocal variables.
"""

import pytest


class TestSelectorState:
    def test_defaults(self):
        """All fields default to False/None."""
        from emrg.client.app import SelectorState
        s = SelectorState()
        assert s.active is False
        assert s.widget is None
        assert s.pending is False

    def test_independent_instances(self):
        """Each instance is independent — setting one doesn't affect another."""
        from emrg.client.app import SelectorState
        session_sel = SelectorState()
        project_sel = SelectorState()
        model_sel = SelectorState()

        session_sel.active = True
        session_sel.pending = True

        assert project_sel.active is False
        assert project_sel.pending is False
        assert model_sel.active is False
        assert model_sel.pending is False

    def test_active_widget_pending_lifecycle(self):
        """Simulate a selector lifecycle: activate → navigate → confirm → deactivate."""
        from emrg.client.app import SelectorState

        class MockWidget:
            def __init__(self):
                self.selected = "foo"

        sel = SelectorState()

        # Activate
        sel.active = True
        sel.widget = MockWidget()
        sel.pending = False
        assert sel.active and sel.widget is not None and not sel.pending

        # Confirm selection
        result = sel.widget.selected
        sel.active = False
        sel.widget = None

        assert sel.active is False
        assert sel.widget is None
        assert result == "foo"

    def test_three_selectors_different_widget_types(self):
        """Verify that widget field accepts different widget types."""
        from emrg.client.app import SelectorState

        class SessionSelector:
            pass

        class ProjectSelector:
            pass

        session_sel = SelectorState()
        session_sel.widget = SessionSelector()

        project_sel = SelectorState()
        project_sel.widget = ProjectSelector()

        # Each widget type is independent
        assert isinstance(session_sel.widget, SessionSelector)
        assert isinstance(project_sel.widget, ProjectSelector)

    def test_pending_flag_independent_from_active(self):
        """pending and active are independent — a selector can be pending without being active."""
        from emrg.client.app import SelectorState

        sel = SelectorState()
        sel.pending = True
        assert sel.active is False  # pending doesn't imply active

        sel.active = True
        sel.pending = False
        assert sel.active is True  # active doesn't imply pending


class TestSelectorStateNonlocalPattern:
    """Tests that the SelectorState pattern eliminates the need for individual nonlocal variables.

    Rant #31: "put selector_active, selector_widget... into a SelectorState class so
    handle_key only needs nonlocal selector_state instead of 3 nonlocal lines for 9 variables."
    """

    def test_state_mutation_without_rebinding(self):
        """Prove that mutating SelectorState attributes doesn't require nonlocal rebinding.

        This is the key reason the refactoring works: we can mutate the object's
        attributes without needing `nonlocal sel.active, sel.widget, sel.pending` —
        we only need `nonlocal sel` if we rebind the variable itself.
        """
        from emrg.client.app import SelectorState

        sel = SelectorState()

        def outer():
            def inner():
                # No nonlocal needed here — we're mutating attributes
                sel.active = True
                sel.widget = "test"
                sel.pending = True

            inner()
            assert sel.active is True
            assert sel.widget == "test"
            assert sel.pending is True

        outer()
