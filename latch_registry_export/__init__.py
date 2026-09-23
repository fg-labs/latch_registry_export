"""Latch Registry export to DuckDB."""

from importlib.metadata import version

from latch_registry_export.api import ExportReport
from latch_registry_export.api import export
from latch_registry_export.api import load_report
from latch_registry_export.config import TableConfig
from latch_registry_export.config import load_table_configs
from latch_registry_export.errors import ConversionError
from latch_registry_export.errors import DuplicateRecordNameError
from latch_registry_export.errors import EmptyRecordNameError
from latch_registry_export.errors import ExportError
from latch_registry_export.errors import InvalidConfigError
from latch_registry_export.errors import InvalidTableNameError
from latch_registry_export.errors import MissingTableError
from latch_registry_export.errors import OutputExistsError
from latch_registry_export.errors import UnsupportedTypeError

__version__ = version("latch_registry_export")
__all__ = [
    "ConversionError",
    "DuplicateRecordNameError",
    "EmptyRecordNameError",
    "ExportError",
    "ExportReport",
    "InvalidConfigError",
    "InvalidTableNameError",
    "MissingTableError",
    "OutputExistsError",
    "TableConfig",
    "UnsupportedTypeError",
    "export",
    "load_report",
    "load_table_configs",
]
