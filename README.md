# latch_registry_export

[![PyPI Release](https://badge.fury.io/py/latch_registry_export.svg)](https://badge.fury.io/py/latch_registry_export)
[![CI](https://github.com/fg-labs/latch_registry_export/actions/workflows/python_package.yml/badge.svg?branch=main)](https://github.com/fg-labs/latch_registry_export/actions/workflows/python_package.yml?query=branch%3Amain)
[![Python Versions](https://img.shields.io/badge/python-3.12_|_3.13_|_3.14-blue)](https://github.com/fg-labs/latch_registry_export)
[![MyPy Checked](http://www.mypy-lang.org/static/mypy_badge.svg)](http://mypy-lang.org/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://docs.astral.sh/ruff/)

Latch Registry export to DuckDB

## Usage

Write a TOML config that lists the Registry tables to export:

```toml
[[tables]]
id = "11730"
name = "samples"   # optional DuckDB table-name override

[[tables]]
id = "12146"
```

Log in to Latch and select the workspace:

```console
latch login
latch workspace
```

Then export the tables to a DuckDB file:

```console
uv run latch_registry_export --config tables.toml --output registry.duckdb
```

Options:
- `--page-size N` — page size for streaming records from the Registry (default 100).
- `--overwrite` — replace an existing output file.
- `--log-level LEVEL` — one of `DEBUG`, `INFO`, `WARNING`, `ERROR` (default `INFO`).
  The tool never logs GraphQL request and response bodies, at any level.

The export writes provenance and data-quality metadata into the output database,
alongside the exported tables:
- `_export_run` — one row with the tool, dependency, and workspace versions for the run.
- `_export_tables` — one row per exported table, with its row count.
- `_export_columns` — one row per exported column, including its foreign-key status.
- `_export_issues` — rows for values that could not convert cleanly (for example,
  an integer that overflows its target type).

The reproducibility target is content-equivalence of the DATA tables, plus
`_export_tables`, `_export_columns`, and `_export_issues`. `_export_run` varies
run-to-run by design (timestamp, dependency versions, page size), so exclude it
when you compare two exports.

## Recommended Installation

Install the Python package and dependency management tool [`uv`](https://docs.astral.sh/uv/getting-started/installation/) using official documentation.

Install the dependencies of the project with:

```console
uv sync --locked
```

To check successful installation, run:

```console
uv run latch_registry_export --help
```

## Development and Testing

See the [contributing guide](./CONTRIBUTING.md) for more information.
