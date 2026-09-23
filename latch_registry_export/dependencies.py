"""FK graph resolution: fk_status per link column and a deterministic insert order."""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from latch_registry_export.errors import MissingTableError
from latch_registry_export.schema import TableSchema


class FkStatus(StrEnum):
    """The FK treatment resolved for one link column."""

    ENFORCED = "enforced"
    LIST_NO_FK = "list_no_fk"
    BACK_EDGE = "back_edge"
    SELF_LINK = "self_link"
    # Never produced by `_collect_links`: a non-link column yields no `LinkPlan`
    # at all. Consumers use it as the default for `fk_status_by_column.get(key, FkStatus.NONE)`.
    NONE = "none"


DEGRADED_FK_STATUSES: tuple[FkStatus, ...] = (FkStatus.BACK_EDGE, FkStatus.SELF_LINK)


@dataclass(frozen=True)
class LinkPlan:
    """The resolved FK treatment for one link column."""

    table_sql_name: str
    column_sql_name: str
    target_table_sql_name: str
    fk_status: FkStatus


@dataclass(frozen=True)
class ResolvedPlan:
    """The insert order and per-link FK plan for a set of tables."""

    insert_order: tuple[str, ...]
    link_plans: tuple[LinkPlan, ...]
    fk_status_by_column: Mapping[tuple[str, str], FkStatus]


def _collect_links(
    schemas: Sequence[TableSchema], id_to_sql: dict[str, str]
) -> tuple[list[LinkPlan], dict[str, set[str]]]:
    """
    Build the per-column link plans and the enforced-FK ordering graph.

    Args:
        schemas: The table schemas to export.
        id_to_sql: Map of table_id to sql_name, for all `schemas`.

    Returns:
        `(links, ordering_edges)`, where `ordering_edges` maps a source table's
        sql_name to the sql_names of its enforced-FK parents (provisional: a cycle
        edge is later demoted to `back_edge` by `_break_cycles`).

    Raises:
        MissingTableError: One or more links (scalar or array) target a table not
            in `id_to_sql`. All such links are reported together, not just the
            first one found.
    """
    links: list[LinkPlan] = []
    ordering_edges: dict[str, set[str]] = {s.sql_name: set() for s in schemas}
    missing: list[str] = []

    for schema in schemas:
        for col in schema.columns:
            target_id = col.link_target_table_id
            if target_id is None:
                continue
            if target_id not in id_to_sql:
                missing.append(
                    f"table {schema.table_id} column {col.registry_key!r} links to "
                    f"table {target_id}, which is not in the export config"
                )
                continue
            target_sql = id_to_sql[target_id]
            if col.is_list:
                # Checked before the self-link case: a self-referential ARRAY link is
                # `list_no_fk`, not `self_link`. Intentional and harmless either way,
                # since neither status contributes an ordering edge.
                status: FkStatus = FkStatus.LIST_NO_FK
            elif target_id == schema.table_id:
                status = FkStatus.SELF_LINK
            else:
                status = FkStatus.ENFORCED  # provisional; may become back_edge below
            links.append(LinkPlan(schema.sql_name, col.sql_name, target_sql, status))
            if status == FkStatus.ENFORCED:
                ordering_edges[schema.sql_name].add(target_sql)

    if missing:
        raise MissingTableError(
            f"{len(missing)} link(s) target tables not in the export config:\n" + "\n".join(missing)
        )

    return links, ordering_edges


def _find_cycle(ordering_edges: dict[str, set[str]], sql_to_id: dict[str, str]) -> list[str] | None:
    """
    Find one cycle in `ordering_edges`, deterministically (table_id-ordered DFS).

    All neighbour and seed-node iteration is sorted by `table_id`, so the result is
    independent of schema input order, not just of the `set` iteration order.

    Args:
        ordering_edges: Map of source sql_name to prerequisite parent sql_names.
        sql_to_id: Map of sql_name to table_id, for all tables in `ordering_edges`.

    Returns:
        The cycle as a list of sql_names, or None if the graph is acyclic.
    """
    color: dict[str, int] = {}  # 0=unvisited, 1=in-stack, 2=done
    stack: list[str] = []

    # Recursive DFS: assumes a small graph (registry exports have few tables). A
    # pathologically deep FK chain would raise RecursionError.
    def dfs(node: str) -> list[str] | None:
        color[node] = 1
        stack.append(node)
        for nxt in sorted(ordering_edges[node], key=lambda s: sql_to_id[s]):
            if color.get(nxt, 0) == 0:
                found = dfs(nxt)
                if found is not None:
                    return found
            elif color.get(nxt) == 1:
                return stack[stack.index(nxt) :]
        stack.pop()
        color[node] = 2
        return None

    for start in sorted(ordering_edges, key=lambda s: sql_to_id[s]):
        if color.get(start, 0) == 0:
            found = dfs(start)
            if found is not None:
                return found
    return None


def _break_cycles(
    ordering_edges: dict[str, set[str]], sql_to_id: dict[str, str]
) -> tuple[dict[str, set[str]], set[tuple[str, str]]]:
    """
    Remove enforced edges, on a copy, until the graph is acyclic.

    Each cycle is broken by removing the edge whose `(source_id, target_id)` sorts
    last lexicographically, so the choice is deterministic and independent of
    schema input order.

    Args:
        ordering_edges: Map of source sql_name to prerequisite parent sql_names.
            Not mutated; a copy is edited and returned.
        sql_to_id: Map of sql_name to table_id, for all tables in `ordering_edges`.

    Returns:
        `(acyclic_edges, demoted)`: the acyclic copy of `ordering_edges`, and the
        `(source_sql, target_sql)` edges removed to break cycles.
    """
    acyclic_edges = {node: set(parents) for node, parents in ordering_edges.items()}
    demoted: set[tuple[str, str]] = set()
    while True:
        cycle = _find_cycle(acyclic_edges, sql_to_id)
        if cycle is None:
            return acyclic_edges, demoted
        # `cycle` is a path returned from a DFS stack, so consecutive elements
        # (including the closing edge back to cycle[0]) are edge-connected by
        # construction: `cycle_edges` is never empty, so `max()` is always safe.
        cycle_edges = [(cycle[i], cycle[(i + 1) % len(cycle)]) for i in range(len(cycle))]
        worst = max(cycle_edges, key=lambda e: (sql_to_id[e[0]], sql_to_id[e[1]]))
        acyclic_edges[worst[0]].discard(worst[1])
        demoted.add(worst)


def _apply_demotions(links: Iterable[LinkPlan], demoted: set[tuple[str, str]]) -> list[LinkPlan]:
    """
    Downgrade the demoted enforced links to `back_edge`.

    Args:
        links: The provisional per-column link plans.
        demoted: The `(source_sql, target_sql)` edges removed by `_break_cycles`.

    Returns:
        `links`, with each demoted `enforced` link replaced by a `back_edge` one.
    """
    resolved: list[LinkPlan] = []
    for lp in links:
        edge = (lp.table_sql_name, lp.target_table_sql_name)
        if lp.fk_status == FkStatus.ENFORCED and edge in demoted:
            resolved.append(
                LinkPlan(
                    lp.table_sql_name,
                    lp.column_sql_name,
                    lp.target_table_sql_name,
                    FkStatus.BACK_EDGE,
                )
            )
        else:
            resolved.append(lp)
    return resolved


def _topological_order(ordering_edges: dict[str, set[str]], sql_to_id: dict[str, str]) -> list[str]:
    """
    Compute a deterministic, parents-first topological order.

    Args:
        ordering_edges: The (now acyclic) map of source sql_name to prerequisite
            parent sql_names.
        sql_to_id: Map of sql_name to table_id, for all tables in `ordering_edges`.

    Returns:
        The table sql_names in topological order.
    """
    order: list[str] = []
    visited: set[str] = set()

    # Recursive DFS: assumes a small graph (registry exports have few tables). A
    # pathologically deep FK chain would raise RecursionError.
    def visit(node: str) -> None:
        if node in visited:
            return
        for parent in sorted(ordering_edges[node], key=lambda s: sql_to_id[s]):
            visit(parent)
        visited.add(node)
        order.append(node)

    # All ordering (here and in `_find_cycle`/`_break_cycles`) is keyed on table_id,
    # not sql_name, so results are independent of schema input order.
    for node in sorted(ordering_edges, key=lambda s: sql_to_id[s]):
        visit(node)
    return order


def resolve_dependencies(schemas: Sequence[TableSchema]) -> ResolvedPlan:
    """
    Compute fk_status per link column and a deterministic topological insert order.

    Args:
        schemas: The table schemas to export.

    Returns:
        A `ResolvedPlan` with deterministic `insert_order` and per-column fk_status.

    Raises:
        MissingTableError: A link (scalar or array) targets a table not in `schemas`.
    """
    id_to_sql = {s.table_id: s.sql_name for s in schemas}
    sql_to_id = {s.sql_name: s.table_id for s in schemas}

    links, ordering_edges = _collect_links(schemas, id_to_sql)
    acyclic_edges, demoted = _break_cycles(ordering_edges, sql_to_id)
    resolved_links = _apply_demotions(links, demoted)
    resolved_links.sort(key=lambda lp: (lp.table_sql_name, lp.column_sql_name))
    order = _topological_order(acyclic_edges, sql_to_id)

    fk_status_by_column = {
        (lp.table_sql_name, lp.column_sql_name): lp.fk_status for lp in resolved_links
    }
    return ResolvedPlan(
        insert_order=tuple(order),
        link_plans=tuple(resolved_links),
        fk_status_by_column=fk_status_by_column,
    )
