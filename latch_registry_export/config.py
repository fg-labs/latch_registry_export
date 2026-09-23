"""Config model, TOML loader, and canonical config hashing."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from latch_registry_export.errors import InvalidConfigError
from latch_registry_export.errors import InvalidTableNameError

SQL_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
RESERVED_PREFIX = "_export_"


class TableConfig(BaseModel):
    """A single table to export."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    name: str | None = None


def _validate_override(name: str) -> None:
    """Reject a table-name override that is not an already-valid identifier."""
    if name.startswith(RESERVED_PREFIX):
        raise InvalidTableNameError(
            f"table name override {name!r} uses the reserved {RESERVED_PREFIX!r} prefix"
        )
    if not SQL_IDENTIFIER_RE.match(name):
        raise InvalidTableNameError(
            f"table name override {name!r} is not a valid identifier "
            f"(must match {SQL_IDENTIFIER_RE.pattern!r})"
        )


def _find_duplicates(values: Sequence[str]) -> list[str]:
    """Return the values that appear more than once, in first-seen order."""
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates


def load_table_configs(path: Path) -> list[TableConfig]:
    """
    Load and validate the list of tables to export from a TOML file.

    Args:
        path: Path to the TOML config file.

    Returns:
        The parsed, validated table configs in file order.

    Raises:
        InvalidConfigError: Malformed TOML, an invalid `[[tables]]` entry, an empty
            table list, duplicate ids, or duplicate name overrides.
        InvalidTableNameError: A name override is not a valid identifier.
    """
    with path.open("rb") as fh:
        try:
            raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise InvalidConfigError(f"invalid TOML configuration: {exc}") from exc
    entries = raw.get("tables", [])
    if not entries:
        raise InvalidConfigError("config must define at least one [[tables]] entry")

    try:
        configs = [TableConfig(**entry) for entry in entries]
    except ValidationError as exc:
        raise InvalidConfigError(f"invalid [[tables]] entry in config: {exc}") from exc

    ids = [c.id for c in configs]
    duplicate_ids = _find_duplicates(ids)
    if duplicate_ids:
        raise InvalidConfigError(f"duplicate table id in config: {duplicate_ids}")

    overrides = [c.name for c in configs if c.name is not None]
    duplicate_overrides = _find_duplicates(overrides)
    if duplicate_overrides:
        raise InvalidConfigError(f"duplicate table name override in config: {duplicate_overrides}")

    for name in overrides:
        _validate_override(name)

    return configs
