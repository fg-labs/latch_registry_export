"""Config model and TOML loader."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated
from typing import Self

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import StringConstraints
from pydantic import ValidationError
from pydantic import model_validator

from latch_registry_export.errors import InvalidConfigError
from latch_registry_export.errors import InvalidTableNameError

SQL_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
RESERVED_PREFIX = "_export_"

ColumnKeys = Annotated[
    tuple[Annotated[str, StringConstraints(min_length=1)], ...], Field(min_length=1)
]
"""A non-empty list of exact Registry column keys."""


class TableConfig(BaseModel):
    """
    A single table to export.

    Attributes:
        id: The Registry table id.
        name: An optional DuckDB table-name override.
        include_columns: Export only these Registry column keys.
        exclude_columns: Export every column except these Registry column keys.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    name: str | None = None
    include_columns: ColumnKeys | None = None
    exclude_columns: ColumnKeys | None = None

    @model_validator(mode="after")
    def _validate_column_selection(self) -> Self:
        """Reject both column lists together, or a column listed twice in one list."""
        if self.include_columns is not None and self.exclude_columns is not None:
            raise ValueError("set include_columns or exclude_columns, not both")
        for field_name, keys in (
            ("include_columns", self.include_columns),
            ("exclude_columns", self.exclude_columns),
        ):
            duplicates = _find_duplicates(keys or ())
            if duplicates:
                raise ValueError(f"duplicate column in {field_name}: {duplicates}")
        return self


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
        InvalidConfigError: Malformed TOML, an invalid `[[tables]]` entry (including a
            bad column selection), an empty table list, duplicate ids, or duplicate
            name overrides.
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
