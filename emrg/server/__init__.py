"""EMRG server subpackage — the daemon is the life entity."""

def __getattr__(name):
    if name in ("EmrgServer", "run_server"):
        from emrg.server.daemon import EmrgServer, run_server
        return locals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = ["EmrgServer", "run_server"]
