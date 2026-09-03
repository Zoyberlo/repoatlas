"""Estimating how many tokens a piece of text will cost.

There is no offline tokeniser for Claude, so any local figure is an
estimate. The estimate here is characters divided by a constant, with
newlines counted separately because a map is mostly short lines and each
one ends in a token of its own.

The constant is the whole problem. Source code runs shorter per token than
prose: identifiers split into several pieces and punctuation is dense.
Worse, the constant is not stable across model generations. Anthropic's
token-counting documentation states that the tokenizer introduced with
Opus 4.7, and shared by the Fable and Mythos families, produces about
thirty percent more tokens for the same text than the models before it.
A budget fitted with the old constant overshoots by a third on the models
most likely to read it.

So the default here is the old code figure divided by that growth, which
is right for current models and conservative for older ones, and
:func:`calibrate_constant` replaces it with a measurement. The counting
endpoint is free and counts under the tokenizer of whichever model is
named, so one call per model pins the number instead of guessing it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

__all__ = [
    "CHARS_PER_TOKEN",
    "CHARS_PER_TOKEN_BEFORE_OPUS_47",
    "TOKENIZER_GROWTH_FROM_OPUS_47",
    "TokenEstimator",
    "calibrate_constant",
    "estimate_tokens",
    "estimate_with",
    "make_estimator",
]

CHARS_PER_TOKEN_BEFORE_OPUS_47 = 3.4
"""Characters per token of source code on models before Opus 4.7.

Prose ran nearer four. tiktoken undercounts Claude by fifteen to twenty
percent on prose and more on code, so a tokeniser from another vendor was
never a shortcut to a better number.
"""

TOKENIZER_GROWTH_FROM_OPUS_47 = 1.3
"""How many more tokens the Opus 4.7 tokenizer produces for the same text.

"Approximately 30 percent", per the token-counting documentation, with the
exact figure depending on the content. Applied as a divisor to the older
constant until a calibration replaces both.
"""

CHARS_PER_TOKEN = round(CHARS_PER_TOKEN_BEFORE_OPUS_47 / TOKENIZER_GROWTH_FROM_OPUS_47, 2)
"""The default: characters per token on current models, before calibration."""


class TokenEstimator(Protocol):
    """Anything that can say what a string will cost."""

    def __call__(self, text: str) -> int:
        ...


def estimate_with(text: str, chars_per_token: float) -> int:
    """The estimate, with an explicit constant.

    Newlines almost always take a token of their own, and a map is mostly
    short lines, so counting them separately is closer than dividing the
    whole string. The trailing one is the token every non-empty text costs
    to exist at all.
    """
    if not text:
        return 0
    lines = text.count("\n")
    body = len(text) - lines
    return int(body / chars_per_token) + lines + 1


def estimate_tokens(text: str) -> int:
    """Estimate the token cost of ``text`` with the default constant."""
    return estimate_with(text, CHARS_PER_TOKEN)


def make_estimator(chars_per_token: float) -> TokenEstimator:
    """Build an estimator with a calibrated constant."""
    if chars_per_token <= 0:
        raise ValueError("characters per token must be positive")

    def estimate(text: str) -> int:
        return estimate_with(text, chars_per_token)

    return estimate


def calibrate_constant(samples: Iterable[tuple[str, int]]) -> float:
    """Solve for the constant that makes the estimate match real counts.

    Each sample pairs a text with the token count the provider reported
    for it. The estimate is ``body / c + lines + 1``, so summing over the
    samples and solving for ``c`` gives the one constant that makes the
    total estimate equal the total count. Pooling rather than averaging
    per-sample ratios lets long samples count for more, which is right:
    the budget errors that matter are on long texts.
    """
    body_total = 0
    token_total = 0
    for text, counted in samples:
        if not text or counted <= 0:
            continue
        lines = text.count("\n")
        body_total += len(text) - lines
        token_total += counted - lines - 1
    if body_total <= 0 or token_total <= 0:
        raise ValueError("calibration needs at least one non-empty sample with a positive count")
    return body_total / token_total
