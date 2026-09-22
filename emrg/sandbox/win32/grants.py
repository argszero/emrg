"""One write SID's provider-lifetime grant materialization.

Ported from the blueprint's ``grant.ts`` (``sandbox-windows-acl``, dsh
0.1.6-alpha.2).

The blueprint holds two kinds of grant and the difference is load-bearing:

* **standing** — the workspace root's ACE.  It is the cross-session reuse
  cache: ``dispose`` skips revoking it, because revoking would force the next
  provision to re-propagate the whole tree;
* **revocable** — a session's private temp directory's ACE.  It is removed on
  dispose, so an inheritable ACE never outlives the directory it names.

A path is recorded **before** it is granted: ``grant_write`` can throw after a
successful apply (a ``LocalFree`` failure), and the fail-closed caller must
still revoke it — and revoking an ungranted path is a no-op merge.
"""

from __future__ import annotations

import ctypes

from emrg.sandbox.win32.acl import grant_write, revoke_write
from emrg.sandbox.win32.ffi import (
    Win32Bindings,
    alloc_ptr_slot,
    decode_ptr,
    is_null_ptr,
    local_free,
    throw_last_error,
    win32,
)


class AclWriteGrant:
    """One write SID, its parsed pointer, and the directories carrying its ACE.

    Create with :meth:`create`; :meth:`dispose` revokes the revocable paths and
    frees the SID.  Every failure throws — the blueprint's fail-closed rule: a
    grant that could not be materialized must not look like one that was.
    """

    __slots__ = ("write_sid", "_api", "_sid_ptr", "_revocable", "_standing")

    def __init__(self, api: Win32Bindings, sid_ptr: int, write_sid: str) -> None:
        self.write_sid = write_sid
        self._api = api
        self._sid_ptr = sid_ptr
        self._revocable: list[str] = []
        self._standing: list[str] = []

    @classmethod
    def create(cls, write_sid: str, api: Win32Bindings | None = None) -> AclWriteGrant:
        """Parse the SID string, opening the binding table lazily.

        :param write_sid: the workspace (``S-1-4-x-y``) or temp
            (``S-1-4-x-y-1``) capability SID string.
        :param api: an already-resolved binding table (tests).
        :returns: the ready grant (no ACEs yet).
        :raises Win32Error: when the SID could not be parsed.
        """
        bindings = api if api is not None else win32()
        sid_slot = alloc_ptr_slot()
        converted = int(bindings.advapi32.ConvertStringSidToSidW(write_sid, ctypes.byref(sid_slot)))
        if converted == 0:
            throw_last_error(bindings, "ConvertStringSidToSidW", write_sid)
        sid_ptr = decode_ptr(sid_slot)
        if is_null_ptr(sid_ptr):
            throw_last_error(bindings, "ConvertStringSidToSidW", f"null SID for {write_sid}")
        return cls(bindings, sid_ptr, write_sid)

    @property
    def sid_ptr(self) -> int:
        """The parsed SID pointer this grant's ACEs name.

        :returns: the pointer.
        """
        return self._sid_ptr

    @property
    def paths(self) -> list[str]:
        """Every directory currently carrying the grant, in grant order.

        :returns: the standing paths followed by the revocable ones.
        """
        return [*self._standing, *self._revocable]

    def add(self, path: str, standing: bool = False) -> None:
        """Grant the write ACE on one directory.

        Idempotent at the ACL layer: an already-standing exact ACE skips the
        eager full-tree re-propagation.

        :param path: the directory whose DACL gains the grant.
        :param standing: whether the ACE outlives this grant (the workspace
            reuse cache); ``False`` means "revoked on dispose" — the
            temp-directory lifecycle.
        :raises Win32Error: when the grant failed.
        """
        (self._standing if standing else self._revocable).append(path)
        grant_write(self._api, path, self._sid_ptr)

    def dispose(self) -> None:
        """Revoke every revocable grant, keep the standing ones, free the SID.

        :raises RuntimeError: when one or more cleanup operations failed, with
            every failure attached.
        """
        failures: list[BaseException] = []
        for path in self._revocable:
            try:
                revoke_write(self._api, path, self._sid_ptr)
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                failures.append(exc)
        self._revocable = []
        try:
            freed = local_free(self._api, self._sid_ptr)
            if not is_null_ptr(freed):
                throw_last_error(self._api, "LocalFree", "write SID")
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            failures.append(exc)
        if failures:
            error = RuntimeError(
                f"AclWriteGrant dispose completed with {len(failures)} cleanup failure(s)"
            )
            error.failures = failures  # type: ignore[attr-defined]
            raise error
