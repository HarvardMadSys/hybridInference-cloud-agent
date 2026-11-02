"""The shapes usage totals share: which metrics there are, and their sums.

A sum keeps every metric no row reported as null, and counts those rows in
:class:`UsageUnknownRows`: a partial sum is useful, but it is not a claim that
every model call was accounted for.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

USAGE_METRICS = ("tokens_in", "tokens_out", "cache_read", "cache_write", "reasoning", "spent_usd")
NonnegativeInt = Annotated[StrictInt, Field(ge=0)]
#: The width a derived amount may occupy. No agent reports a cost, so every
#: amount is null today; the bound stays so that a sum is never rounded.
DERIVED_AMOUNT_CHARS = 384
_AMOUNT_PATTERN = r"^[0-9]+(?:\.[0-9]+)?$"
DerivedAmount = Annotated[
    StrictStr, Field(pattern=_AMOUNT_PATTERN, max_length=DERIVED_AMOUNT_CHARS)
]


class _UsageModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class UsageSums(_UsageModel):
    """Known metric sums over many turns; a metric no turn reported stays null.

    ``calls`` is null when nothing in the sum counted model calls: an agent's
    own usage report says how many tokens it used, not how many requests made
    them.
    """

    calls: NonnegativeInt | None = None
    tokens_in: NonnegativeInt | None
    tokens_out: NonnegativeInt | None
    cache_read: NonnegativeInt | None
    cache_write: NonnegativeInt | None
    reasoning: NonnegativeInt | None
    spent_usd: DerivedAmount | None


class UsageUnknownRows(_UsageModel):
    """How many reporting rows lack each metric; never a fabricated zero."""

    tokens_in: NonnegativeInt
    tokens_out: NonnegativeInt
    cache_read: NonnegativeInt
    cache_write: NonnegativeInt
    reasoning: NonnegativeInt
    spent_usd: NonnegativeInt
