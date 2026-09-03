"""Persisting an index, and keeping it current.

A single SQLite file. Reading it is the common case, by a wide margin, so
it has to open cold in milliseconds and answer a symbol lookup without
loading the whole graph.

Updating parses only the files whose contents changed, then re-resolves
every reference. That asymmetry is deliberate and measured: parsing costs
roughly sixty times what resolution does, and patching edges selectively
would leave stale ones pointing into files that just moved.
"""

from __future__ import annotations

from .database import (
    FileRecord,
    IndexStore,
    StoreError,
    content_digest,
    toolchain_version,
)
from .incremental import (
    ChangeSet,
    UpdateResult,
    current_toolchain,
    detect_changes,
    update_store,
)
from .schema import SCHEMA_VERSION

__all__ = [
    "SCHEMA_VERSION",
    "ChangeSet",
    "FileRecord",
    "IndexStore",
    "StoreError",
    "UpdateResult",
    "content_digest",
    "current_toolchain",
    "detect_changes",
    "toolchain_version",
    "update_store",
]
