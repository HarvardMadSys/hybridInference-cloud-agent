"""How an owner narrows and orders a conversation's command executions."""

from __future__ import annotations

from typing import Literal

CommandFilter = Literal["all", "failed", "slow", "background"]
CommandSort = Literal["latest", "slowest"]
