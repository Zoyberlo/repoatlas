"""Definitions the rest of the fixture refers to across files."""

from __future__ import annotations

DEFAULT_PREFIX = "hello"


class Greeter:
    """Greets a name with a fixed prefix."""

    def __init__(self, prefix: str = DEFAULT_PREFIX) -> None:
        self.prefix = prefix

    def greet(self, name: str) -> str:
        return f"{self.prefix} {name}"

    def _shout(self, name: str) -> str:
        return self.greet(name).upper()


class LoudGreeter(Greeter):
    """Overrides a method, so the oracle records an inheritance edge."""

    def greet(self, name: str) -> str:
        return self._shout(name)
