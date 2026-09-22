"""The Win32 package: the ACL restricted-token backend, ported from dsh.

The blueprint is ``packages/sandbox/sandbox-windows-acl/`` in deepseek-harness
(commit ``ddefc45fbc``, dsh 0.1.6-alpha.2), layered on
``packages/subprocess/win32-process/``.  File for file:

===========================  ==========================================
blueprint                    here
===========================  ==========================================
``src/win32-abi.ts``         :mod:`~emrg.sandbox.win32.abi`
``src/ffi.ts``               :mod:`~emrg.sandbox.win32.ffi`
``src/workspace-sid.ts``     :mod:`~emrg.sandbox.win32.sid`
``src/path-boundary.ts``     :mod:`~emrg.sandbox.win32.sid`
``src/acl.ts``               :mod:`~emrg.sandbox.win32.acl`
``src/grant.ts``             :mod:`~emrg.sandbox.win32.grants`
``src/token.ts``             :mod:`~emrg.sandbox.win32.token`
``src/spawn.ts``             :mod:`~emrg.sandbox.win32.spawn`
``src/index.ts``             :mod:`~emrg.sandbox.win32.sandbox`
``src/runner.ts``            :mod:`~emrg.sandbox.win32.runner`
``sandbox-local``'s rung     :mod:`emrg.sandbox.providers.win32`
===========================  ==========================================

The one structural difference is the FFI layer: the blueprint marshals through
``koffi``, this port through :mod:`ctypes`; the ``process.ts`` half of
``win32-process`` is not reproduced because only the inherited-stdio variant is
used here (see :mod:`~emrg.sandbox.win32.spawn`).
"""
