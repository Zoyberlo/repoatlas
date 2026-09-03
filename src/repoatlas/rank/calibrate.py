"""Pinning the token estimate to a real tokenizer, once per model.

The estimate in :mod:`tokens` is a constant, and the constant moved by a
third between model generations. Guessing it again is not the answer;
measuring it is, and the measurement is free: the provider's counting
endpoint takes text, names a model, and returns the count that model would
be billed for.

Calibration renders the kinds of text the index actually produces, maps at
several budgets and a few outlines and reference lists, counts each one,
and stores the resulting constant in the index beside the model it was
counted for. Every tool then estimates with that constant, and a budget of
two thousand tokens means two thousand tokens.

Nothing here depends on the provider's SDK. The endpoint is one JSON POST,
and the standard library can make it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..store.database import IndexStore
from .tokens import calibrate_constant, estimate_with

__all__ = [
    "Calibration",
    "CalibrationError",
    "TokenCounter",
    "anthropic_counter",
    "calibrate_store",
    "sample_texts",
]

TokenCounter = Callable[[str], int]
"""Anything that returns the real token count of a string."""

COUNT_ENDPOINT = "https://api.anthropic.com/v1/messages/count_tokens"
API_VERSION = "2023-06-01"

META_KEY = "chars_per_token"
META_MODEL_KEY = "chars_per_token_model"


class CalibrationError(RuntimeError):
    """Calibration could not be completed, and says why."""


@dataclass(frozen=True, slots=True)
class Calibration:
    """What a calibration measured, so the change can be reported."""

    model: str
    chars_per_token: float
    samples: int
    counted_tokens: int
    estimated_before: int
    """What the default estimate said the samples would cost."""

    @property
    def error_before(self) -> float:
        """Relative error of the default estimate, signed; negative is low."""
        if not self.counted_tokens:
            return 0.0
        return (self.estimated_before - self.counted_tokens) / self.counted_tokens

    def as_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "chars_per_token": round(self.chars_per_token, 3),
            "samples": self.samples,
            "counted_tokens": self.counted_tokens,
            "estimated_before": self.estimated_before,
            "error_before": round(self.error_before, 3),
        }


def anthropic_counter(
    model: str, api_key: str, *, endpoint: str = COUNT_ENDPOINT, timeout: float = 30.0
) -> TokenCounter:
    """A counter backed by the provider's endpoint, for one model.

    The endpoint is free and rate limited by requests rather than tokens,
    so a calibration of a dozen samples is a dozen requests and no cost.
    """
    if not api_key:
        raise CalibrationError("an API key is needed to count tokens")

    def count(text: str) -> int:
        payload = json.dumps(
            {"model": model, "messages": [{"role": "user", "content": text}]}
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=payload,
            method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": API_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise CalibrationError(f"count_tokens returned {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CalibrationError(f"could not reach {endpoint}: {exc}") from None
        try:
            return int(body["input_tokens"])
        except (KeyError, TypeError, ValueError):
            raise CalibrationError(f"unexpected reply from count_tokens: {body!r}") from None

    return count


def sample_texts(
    store: IndexStore, *, budgets: Sequence[int] = (500, 2000, 6000), files: int = 3
) -> list[str]:
    """The texts the index really produces, to calibrate against.

    A map at several budgets, a few file outlines, and a reference list
    for the most used symbol. Calibrating on prose would measure the wrong
    thing: the constant differs between prose and code, and these tools
    only ever emit code-shaped text.
    """
    from ..server import tools

    samples: list[str] = []
    for budget in budgets:
        samples.append(tools.repo_map(store, budget=budget))
    for path in sorted(store.languages())[:files]:
        samples.append(tools.file_outline(store, path))
    used = store.most_referenced(limit=1)
    if used:
        samples.append(tools.find_references(store, used[0]))
    return [text for text in samples if text.strip()]


def calibrate_store(
    store: IndexStore,
    model: str,
    counter: TokenCounter,
    *,
    samples: Sequence[str] | None = None,
) -> Calibration:
    """Measure the constant for ``model`` and record it in the store.

    The prompt wrapping adds a handful of tokens to every count, which the
    empty-message baseline removes: what is counted is the text, not the
    envelope it was sent in.
    """
    texts = list(samples) if samples is not None else sample_texts(store)
    if not texts:
        raise CalibrationError("the index produced nothing to calibrate on; is it empty?")
    baseline = counter("x") - 1
    counted: list[tuple[str, int]] = []
    for text in texts:
        counted.append((text, max(1, counter(text) - baseline)))
    constant = calibrate_constant(counted)
    from .tokens import CHARS_PER_TOKEN

    estimated_before = sum(estimate_with(text, CHARS_PER_TOKEN) for text, _ in counted)
    with store.transaction():
        store.set_meta(META_KEY, repr(constant))
        store.set_meta(META_MODEL_KEY, model)
        store.set_meta(f"{META_KEY}:{model}", repr(constant))
    return Calibration(
        model=model,
        chars_per_token=constant,
        samples=len(counted),
        counted_tokens=sum(count for _, count in counted),
        estimated_before=estimated_before,
    )
