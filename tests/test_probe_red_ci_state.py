"""Throwaway probe: does mergeStateStatus report a red check when no check is required?"""


def test_deliberately_failing_probe():
    assert False, "probe: this failure exists only to make CI red"
