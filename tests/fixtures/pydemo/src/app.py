"""Uses the definitions in models.py, so the edges cross a file boundary."""

from __future__ import annotations

from src.models import DEFAULT_PREFIX, Greeter, LoudGreeter


def build(loud: bool = False) -> Greeter:
    return LoudGreeter(DEFAULT_PREFIX) if loud else Greeter()


def run(name: str) -> str:
    greeter = build(loud=True)
    return greeter.greet(name)
