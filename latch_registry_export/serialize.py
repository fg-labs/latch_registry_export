"""
Pure per-cell serialization of Registry values to polars-friendly primitives.

Union columns (Registry type `union`) are not supported: `schema.py` raises
`UnsupportedTypeError` for them, so no union value reaches this module.

A `Record` cell serializes to the record's already-primed (cached) name. An
unresolved (`None`) name yields `NULL`; the caller is responsible for recording
a dangling-link issue when that happens, since this module does not add one.
"""

from __future__ import annotations

import datetime as _dt
import enum
from dataclasses import dataclass
from dataclasses import replace
from typing import TYPE_CHECKING

from latch_registry_export.schema import ColumnSchema

if TYPE_CHECKING:
    # Only for type annotations below; runtime code imports these lazily.
    from latch.registry.types import InvalidValue

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
_UTC = _dt.timezone.utc


@dataclass(frozen=True)
class Issue:
    """
    A per-cell data issue, destined for the `_export_issues` table.

    The writer stamps `record_name` onto the row; it is not tracked here.
    """

    column_sql_name: str
    registry_key: str
    issue_type: str
    raw_value: str | None
    detail: str | None


def to_naive_utc(value: _dt.datetime) -> _dt.datetime:
    """
    Return `value` as a naive UTC datetime (machine-timezone-independent).

    Args:
        value: An aware or naive datetime.

    Returns:
        The same instant in UTC with `tzinfo` dropped. A naive input is assumed
        to already be UTC and is returned with its fields unchanged.
    """
    if value.tzinfo is not None:
        return value.astimezone(_UTC).replace(tzinfo=None)
    return value


def _issue(col: ColumnSchema, issue_type: str, raw_value: str | None, detail: str | None) -> Issue:
    return Issue(
        column_sql_name=col.sql_name,
        registry_key=col.registry_key,
        issue_type=issue_type,
        raw_value=raw_value,
        detail=detail,
    )


def _invalid_value_issue(value: InvalidValue, col: ColumnSchema) -> Issue:
    """Build the `Issue` for a Latch `InvalidValue`, classifying it by cause."""
    raw_value = value.raw_value
    issue_type = "missing_required" if (raw_value == "" and not col.nullable) else "invalid_value"
    return _issue(col, issue_type, raw_value, None)


def _serialize_scalar(value: object, col: ColumnSchema) -> tuple[object, list[Issue]]:
    """
    Serialize one non-`None`/`EmptyCell`/`InvalidValue` value, list element or scalar.

    Args:
        value: A scalar Registry value (bool, int, float, str, datetime, date,
            Enum, Record, LatchFile/LatchDir, or any other passthrough type).
        col: The column's IR schema, used only for `Issue` context on overflow.

    Returns:
        `(primitive, issues)` per the value -> primitive mapping table.
    """
    from latch.registry.record import Record
    from latch.types.directory import LatchDir
    from latch.types.file import LatchFile

    if isinstance(value, bool):
        return value, []
    if isinstance(value, int):
        if INT64_MIN <= value <= INT64_MAX:
            return value, []
        return None, [_issue(col, "invalid_value", str(value), "int64 overflow")]
    if isinstance(value, _dt.datetime):
        return to_naive_utc(value), []
    if isinstance(value, _dt.date):
        return value, []
    if isinstance(value, enum.Enum):
        return value.name, []
    if isinstance(value, Record):
        return value.get_name(load_if_missing=False), []
    if isinstance(value, (LatchFile, LatchDir)):
        return str(value.path), []
    return value, []


def _with_index_detail(issue: Issue, idx: int) -> Issue:
    """Return `issue` with its `detail` prefixed by the list index, preserving the reason."""
    detail = f"index={idx}: {issue.detail}" if issue.detail else f"index={idx}"
    return replace(issue, detail=detail)


def _serialize_cell(value: object, col: ColumnSchema) -> tuple[object, list[Issue]]:
    """
    Serialize one non-list-shaped value.

    `None`/`EmptyCell` -> NULL, `InvalidValue` -> a recorded issue, anything else
    -> `_serialize_scalar`. Shared by `serialize()`'s scalar path and, once per
    element, its list path.
    """
    from latch.registry.types import InvalidValue
    from latch.registry.upstream_types.values import EmptyCell

    if value is None or isinstance(value, EmptyCell):
        return None, []
    if isinstance(value, InvalidValue):
        return None, [_invalid_value_issue(value, col)]
    return _serialize_scalar(value, col)


def serialize(value: object, col: ColumnSchema) -> tuple[object, list[Issue]]:
    """
    Serialize a single Registry cell value to a polars-friendly primitive.

    Args:
        value: The parsed Registry value: `None`, `EmptyCell`, `InvalidValue`,
            a scalar (bool/int/float/str/datetime/date/Enum/Record/LatchFile/
            LatchDir), or (for a list column) a `list` of any of the above.
        col: The column's IR schema.

    Returns:
        `(primitive, issues)` — `primitive` is `None` on a missing or bad cell;
        `issues` lists any problems to record in the sidecar issues table.
    """
    from latch.registry.types import InvalidValue
    from latch.registry.upstream_types.values import EmptyCell

    if value is None or isinstance(value, (EmptyCell, InvalidValue)):
        return _serialize_cell(value, col)

    if col.is_list:
        if not isinstance(value, list):
            return None, [_issue(col, "invalid_value", repr(value), "expected list")]

        out: list[object] = []
        issues: list[Issue] = []
        for idx, element in enumerate(value):
            elem_value, elem_issues = _serialize_cell(element, col)
            out.append(elem_value)
            issues.extend(_with_index_detail(issue, idx) for issue in elem_issues)
        return out, issues

    if isinstance(value, list):
        return None, [_issue(col, "invalid_value", repr(value), "unexpected list")]

    return _serialize_cell(value, col)
