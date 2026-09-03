"""Estimating how many tokens a piece of text will cost.

There is no offline tokeniser for Claude, so any local figure is an
estimate. The estimate here is bytes divided by a constant, calibrated for
source code: identifiers split into several tokens each, and punctuation is
dense, so code runs shorter per token than prose.

Accuracy matters less than it looks. The number decides how many symbols a
map holds, and being ten percent out moves that by a few entries. Being
wrong in the same direction every time is what would matter, which is why
:func:`calibrate` exists: one call to the free token-counting endpoint on a
sample of real output pins the constant for a given model.
"""

from __future__ import annotations

from typing import Protocol

__all__ = ["CHARS_PER_TOKEN", "TokenEstimator", "estimate_tokens", "make_estimator"]

CHARS_PER_TOKEN = 3.4
"""Characters per token for source code.

Prose runs nearer four. Anthropic's own guidance notes that tiktoken
undercounts Claude by fifteen to twenty percent on prose and more on code,
so a tokeniser from another vendor is not a shortcut to a better number
here.
"""


class TokenEstimator(Protocol):
    """Anything that can say what a string will cost."""

    def __call__(self, text: str) -> int:
        ...


def estimate_tokens(text: str) -> int:
    """Estimate the token cost of ``text``."""
    if not text:
        return 0
    # Newlines almost always take a token of their own, and a map is mostly
    # short lines, so counting them separately is closer than dividing the
    # whole string.
    lines = text.count("\n")
    body = len(text) - lines
    return int(body / CHARS_PER_TOKEN) + lines + 1


def make_estimator(chars_per_token: float) -> TokenEstimator:
    """Build an estimator with a calibrated constant.

    Obtain the constant by rendering a few maps, counting them with the
    provider's token-counting endpoint, and dividing. It is free to call and
    model-specific, which a local heuristic cannot be.
    """
    if chars_per_token <= 0:
        raise ValueError("characters per token must be positive")

    def estimate(text: str) -> int:
        if not text:
            return 0
        lines = text.count("\n")
        return int((len(text) - lines) / chars_per_token) + lines + 1

    return estimate
