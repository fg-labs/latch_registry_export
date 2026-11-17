# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Exports Latch Registry tables to one DuckDB file. Library API in `__init__.py`; defopt
subcommands `export` and `generate-config` registered in `main.py:_tools`.

## Commands

```console
uv run poe fix-and-check-all   # before every commit
uv run poe check-all           # what CI runs (Python 3.12-3.14)
```

Dependabot `cooldown` (14 days) must stay >= uv `exclude-newer` (1 week), or Dependabot
proposes versions that cannot lock.

## Architecture

`api.export()` pipeline:

1. `config.py` validates the TOML `[[tables]]` entries.
2. `schema_builder.py` fetches Registry columns and builds the `schema.py` IR. Each table
   gets a leading `name` primary key; `_export_` is reserved for metadata tables.
3. `dependencies.py` gives each link column an `FkStatus`: `enforced`, `list_no_fk`,
   `self_link`, or `back_edge` (demoted to break a cycle). Only `enforced` links become
   `FOREIGN KEY`s and set the insert order.
4. `writer.py` streams records in insert order. Dangling `enforced` links are set to NULL
   inline; other dangling links are kept and found by an anti-join after all tables load.
   Both go to `_export_issues`.
5. `api.py` writes to a temp file, then `os.replace`s the output.

The output must be content-equivalent across runs, not byte-identical. Every table except
`_export_run` must be deterministic, so sort any new ordering logic by a stable key.

Keep the `gql` logger capped at WARNING (`main.setup_logging`): it logs full response bodies.

## Tests

Offline tests stub the Registry with `monkeypatch.setattr` on `api.build_schemas`,
`writer.fetch_table_records`, and `api._workspace_meta`.

`requires_latch_registry` tests run only when the selected Latch workspace is the Fulcrum
workspace (`tests/constants.py`). They read fixed fixture tables there.

## Git Workflow

### Commit Granularity

Commit after completing one of:
- A single function/method implementation
- One refactoring step (rename, extract, move)
- A bug fix with its regression test
- A documentation update

**Size guidelines:**
- Per commit: 100–300 lines preferred, 400 max
- Per PR: No hard limit, but consider splitting if >800 lines or >5 unrelated files

**Good commit scope examples:**
- `Add FastaIndex.validate() method`
- `Rename species_map → species_to_ref_fasta_map`
- `Fix off-by-one in BED coordinate parsing`

### Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/) for commit messages and PR
titles+bodies. Common types: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`.

```
<type>: <imperative description> (<72 chars total)

Detailed body explaining:
- What changed
- Why (link issues with "Closes #123" or "Related to #456")
- Any non-obvious implementation choices
```

### Commit Rules
- Run `uv run poe fix-and-check-all` before each commit; all checks must pass
- No merge commits
- Do not rebase without explicit user approval
- **Never mix formatting and functional changes.** If unavoidable, isolate formatting into separate commits at start or end of branch.

### Branch Hygiene
- Use `.gitignore` liberally
- Never commit: IDE files, personal test files, local debug data, commented-out code

## Coding Conventions

### Organization
- Extract logic into small–medium functions with clear inputs/outputs
- Scope variables tightly; limit visibility to where needed
- Use block comments for visual separation when function extraction isn't practical

### Naming
- Meaningful names, even if long: `species_to_ref_fasta_map` not `species_map`
- Short names only for tight scope (loop indices, single-line lambdas)
- Signal behavior in function names: `to_y()`, `is_valid()` → returns value; `update_x()` → side effect

## Testing

### Principles
- Generate test data programmatically; avoid committing test data files
- Test behavior, not implementation—tests should survive refactoring
- Cover: expected behavior, error conditions, boundary cases
- Scale rigor to code longevity: thorough for shared code, lighter for one-off scripts

### Coverage Expectations
- New public functions: at least one happy-path test + one error case
- Bug fixes: add a regression test that would have caught the bug
- Performance-critical code: include benchmark or explain in PR why not needed

## Documentation Maintenance

When modifying code, update as needed:
- [ ] Docstrings (if signature or behavior changed)
- [ ] README.md (if usage patterns changed)
- [ ] Migration notes (if breaking change)

## Python-Specific

### Pragmatism
- Balance functional, OOP, and imperative—use what's clearest
- When in doubt, prefer pure functions and immutable data
- Know your utility libraries; contribute upstream rather than writing one-offs

### Style
- Heavier use of classes and type annotations than typical Python
- Prefer `@dataclass(frozen=True)` and Pydantic models with `frozen=True`

### Functions
- Functions should have **either** returns **or** side effects, not both
- Exceptions: logging, caching (where side effect is performance-only)

### Documentation
- Google-style docstrings with `Args:`, `Returns:`, `Yields:`, and `Raises:` blocks
- Docstrings are required on all public functions/classes
- Code comments should explain non-obvious choices and complex logic

### Typing
- **Required:** Type annotations on all function parameters and returns
- **Parameters:** Accept the most general type practical (e.g., `Iterable` over `List`)
- **Returns:** Return the most specific type without exposing implementation details
- Annotate locals when: they become return values, or called function lacks hints
- Use type aliases or `NewType` for complex structures
- Avoid `Any`—prefer type alias or `TypeVar`
- Avoid `cast()` and `type: ignore`—prefer alternatives, but when unavoidable (e.g., incorrect upstream stubs), document the reason inline.

