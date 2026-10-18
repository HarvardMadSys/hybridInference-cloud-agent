"""Port: an owner's SSH servers, the chats that enable them, and their audit trail."""

from __future__ import annotations

from typing import Any, Protocol


class SshServerRepository(Protocol):
    """Persist SSH servers per owner and their per-chat selection.

    Rows are plain dictionaries. Key material is stored only as ciphertext;
    encrypting and decrypting it is the caller's job. Every write that changes
    what a chat can reach records an event in the same transaction.
    """

    async def create_ssh_server(self, *, user_id: str, values: dict[str, Any]) -> dict[str, Any]:
        """Insert a new pending server; raise ``SshServerConflict`` on a taken name."""
        ...

    async def list_ssh_servers(self, *, user_id: str) -> list[dict[str, Any]]:
        """Every unarchived server of one owner, oldest first."""
        ...

    async def get_ssh_server(self, *, user_id: str, server_id: str) -> dict[str, Any] | None:
        """One unarchived server of this owner, or ``None``."""
        ...

    async def update_ssh_server(
        self,
        *,
        user_id: str,
        server_id: str,
        revision: int,
        values: dict[str, Any],
        event: str,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Apply ``values`` if the row is still at ``revision``; ``None`` if it moved or is gone.

        Raises ``SshServerConflict`` when a rename takes a name in use.
        """
        ...

    async def archive_ssh_server(self, *, user_id: str, server_id: str) -> dict[str, Any] | None:
        """Archive a server and erase its key material; ``None`` if it was not found."""
        ...

    async def record_ssh_check(
        self, *, user_id: str, server_id: str, kind: str, detail: dict[str, Any]
    ) -> None:
        """Record one check against the server (counted by :meth:`count_recent_checks`)."""
        ...

    async def count_recent_checks(self, *, user_id: str, seconds: int) -> int:
        """How many checks this owner ran in the last ``seconds``."""
        ...

    async def list_thread_ssh_bindings(
        self, *, user_id: str, thread_id: str
    ) -> list[dict[str, Any]] | None:
        """This chat's selection with each server's name and status; ``None`` if not the owner's."""
        ...

    async def replace_thread_ssh_bindings(
        self, *, user_id: str, thread_id: str, server_ids: list[str]
    ) -> list[dict[str, Any]] | None:
        """Enable exactly ``server_ids`` for the chat and disable the rest.

        Returns the new selection, or ``None`` when the chat is not the
        owner's. Raises ``SshServerUnavailable`` naming a server that is not
        the owner's ready server, unless it is already on for this chat.
        """
        ...

    async def ssh_group_state(self, *, thread_id: str) -> dict[str, Any] | None:
        """The SSH state of the sandbox group ``thread_id`` belongs to.

        ``None`` when no chat in the group ever selected a server: such a group
        is never sent an SSH state at all. Otherwise a fresh ``revision`` from
        the ordering sequence, taken before anything is read, and one entry
        per member chat with its slot and its usable servers' rows.
        """
        ...

    async def running_groups_for_ssh_change(
        self, *, thread_ids: list[str] | None = None, server_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Running groups an SSH change reaches: each group's host and the chats it is known by."""
        ...
