"""Typed exceptions for the export pipeline."""


class ExportError(Exception):
    """Base class for all export errors."""


class MissingTableError(ExportError):
    """A link references a table not present in the export config."""


class DuplicateRecordNameError(ExportError):
    """Two live records in one table share a name (violates the name primary key)."""


class EmptyRecordNameError(ExportError):
    """A record has an empty name (cannot serve as the name primary key)."""


class OutputExistsError(ExportError):
    """The output file exists and overwrite was not requested."""


class ConversionError(ExportError):
    """The Latch SDK could not convert a server-valid cell value."""


class InvalidTableNameError(ExportError):
    """A config table-name override is not a valid SQL identifier."""


class InvalidConfigError(ExportError):
    """
    The export config file is structurally invalid.

    Reasons include an empty table list, duplicate ids, or duplicate name overrides.
    """


class UnknownColumnError(ExportError):
    """A config column selection names a missing column, or excludes every data column."""


class UnsupportedTypeError(ExportError):
    """A Registry type has no DuckDB/polars mapping."""
