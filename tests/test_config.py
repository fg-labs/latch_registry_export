from pathlib import Path

import pytest

from latch_registry_export.config import TableConfig
from latch_registry_export.config import load_table_configs
from latch_registry_export.errors import ExportError
from latch_registry_export.errors import InvalidConfigError


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "tables.toml"
    p.write_text(text)
    return p


def test_load_happy_path(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '[[tables]]\nid = "11730"\nname = "samples"\n\n[[tables]]\nid = "12146"\n',
    )
    configs = load_table_configs(path)
    assert configs == [
        TableConfig(id="11730", name="samples"),
        TableConfig(id="12146", name=None),
    ]


@pytest.mark.parametrize(
    ("toml_text", "match"),
    [
        ("", "at least one"),
        ('[[tables]]\nid = "1"\n\n[[tables]]\nid = "1"\n', "duplicate table id"),
        (
            '[[tables]]\nid = "1"\nname = "x"\n\n[[tables]]\nid = "2"\nname = "x"\n',
            "duplicate .* override",
        ),
        ('[[tables]]\nid = "1"\nname = "My Samples"\n', "not a valid"),
        ('[[tables]]\nid = "1"\nname = "_export_run"\n', "reserved"),
        ('[[tables]]\nid = "1"\nname = "2samples"\n', "not a valid"),
    ],
    ids=[
        "empty-list",
        "duplicate-id",
        "duplicate-override",
        "override-has-space",
        "override-reserved-prefix",
        "override-leading-digit",
    ],
)
def test_load_rejects(tmp_path: Path, toml_text: str, match: str) -> None:
    path = _write(tmp_path, toml_text)
    with pytest.raises(ExportError, match=match):
        load_table_configs(path)


@pytest.mark.parametrize(
    ("toml_text", "match"),
    [
        ("[[tables]]\nid = \n", "invalid TOML"),
        ('[[tables]]\nname = "samples"\n', "invalid .* entry"),
    ],
    ids=["malformed-toml", "entry-missing-id"],
)
def test_load_wraps_upstream_errors_as_invalid_config_error(
    tmp_path: Path, toml_text: str, match: str
) -> None:
    """
    Malformed TOML and an invalid `[[tables]]` entry both raise `InvalidConfigError`.

    The loader must never leak the underlying `tomllib.TOMLDecodeError` or
    pydantic `ValidationError`.
    """
    path = _write(tmp_path, toml_text)
    with pytest.raises(InvalidConfigError, match=match):
        load_table_configs(path)
