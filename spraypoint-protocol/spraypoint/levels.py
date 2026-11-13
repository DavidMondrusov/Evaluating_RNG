"""Destination-specific level partitioning and pointing next hops.

For a destination switch ``t`` every switch is placed in exactly one of:
``t`` itself, WP_0(t), ..., WP_l(t), IR(t) or OR(t). Each non-destination
switch then gets a set of at most ``h`` next-hop neighbors (its pointing
rule). All "random" choices go through :func:`deterministic_sample`, keyed by
the shared hash key, so every switch computes the same pointing graph.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set

from .fabric import FabricGraph
from .params import SpraypointParams

# Level labels used in LevelAssignment.level_of
DEST = "t"
IR = "IR"
OR = "OR"


def wp_label(m: int) -> str:
    return f"WP{m}"


def deterministic_sample(candidates: Iterable[str], k: int, *salt: str) -> List[str]:
    """Pick ``k`` items "at random" from ``candidates``, reproducibly.

    Candidates are ranked by SHA-256 of the salt (shared key, destination,
    choosing node, purpose) plus the candidate name. Any switch evaluating
    the same inputs gets the same answer.
    """
    prefix = "|".join(salt)

    def rank(c: str) -> str:
        return hashlib.sha256(f"{prefix}|{c}".encode()).hexdigest()

    ordered = sorted(set(candidates), key=rank)
    return sorted(ordered[:k])


@dataclass
class LevelAssignment:
    """Levels and pointing next hops toward one destination switch."""

    destination: str
    waypoints: List[Set[str]]                 # waypoints[m] = WP_m(t)
    selections: List[Dict[str, List[str]]]    # selections[m-1][u] = nodes u in WP_{m-1} picked for WP_m
    inner_ring: Set[str]
    outer_ring: Set[str]
    next_hops: Dict[str, List[str]] = field(default_factory=dict)
    distance_to_ir: Dict[str, int] = field(default_factory=dict)

    @property
    def l(self) -> int:
        return len(self.waypoints) - 1

    def level_of(self, node: str) -> str:
        if node == self.destination:
            return DEST
        for m, level in enumerate(self.waypoints):
            if node in level:
                return wp_label(m)
        if node in self.inner_ring:
            return IR
        if node in self.outer_ring:
            return OR
        return "unreachable"

    def table(self) -> Dict[str, List[str]]:
        """Level label -> sorted node list, in protocol order."""
        rows = {DEST: [self.destination]}
        for m, level in enumerate(self.waypoints):
            rows[wp_label(m)] = sorted(level, key=_natural_key)
        rows[IR] = sorted(self.inner_ring, key=_natural_key)
        rows[OR] = sorted(self.outer_ring, key=_natural_key)
        return rows

    def path_tree(self, start: str) -> Set[str]:
        """Every switch a packet can visit after reaching ``start``."""
        seen: Set[str] = set()
        stack = [start]
        while stack:
            u = stack.pop()
            if u in seen:
                continue
            seen.add(u)
            stack.extend(self.next_hops.get(u, []))
        return seen


def _natural_key(name: str):
    import re
    return [int(tok) if tok.isdigit() else tok for tok in re.split(r"(\d+)", name)]


def compute_levels(graph: FabricGraph, destination: str, params: SpraypointParams,
                   waypoint_overrides: Optional[Mapping[str, Sequence[str]]] = None,
                   ) -> LevelAssignment:
    """Partition the fabric relative to ``destination`` and compute next hops.

    ``waypoint_overrides`` maps a waypoint node to the neighbors it selects
    for the next level, replacing the hashed choice for that node. This lets
    a test pin the selections drawn in a paper figure. Overridden choices
    must still be valid candidates (neighbors not already in a prior level).
    """
    if destination not in graph.ports:
        raise KeyError(f"unknown destination switch {destination!r}")
    overrides = dict(waypoint_overrides or {})
    key = params.hash_key
    l = params.resolve_l(len(graph.ports), max(1, round(graph.average_degree())))

    assigned: Set[str] = {destination}
    wp0 = set(graph.neighbors(destination))
    assigned |= wp0
    waypoints: List[Set[str]] = [wp0]
    selections: List[Dict[str, List[str]]] = []

    # WP_m for m in [1, l]
    for m in range(1, l + 1):
        prev = waypoints[m - 1]
        level: Set[str] = set()
        chosen: Dict[str, List[str]] = {}
        for u in sorted(prev):
            candidates = [v for v in graph.neighbors(u) if v not in assigned]
            if u in overrides:
                picks = sorted(overrides[u])
                bad = [v for v in picks if v not in candidates]
                if bad:
                    raise ValueError(
                        f"waypoint override for {u!r} toward {destination!r} names "
                        f"{bad}, which are not eligible (candidates: {candidates})")
            else:
                picks = deterministic_sample(candidates, params.p, key, destination, u, f"wp{m}")
            chosen[u] = picks
            level.update(picks)
        if not level:
            break  # no unassigned neighbors left; the effective l is m-1
        selections.append(chosen)
        waypoints.append(level)
        assigned |= level

    top = waypoints[-1]
    inner = {v for u in top for v in graph.neighbors(u) if v not in assigned}
    assigned |= inner
    outer = set(graph.ports) - assigned

    la = LevelAssignment(destination, waypoints, selections, inner, outer)
    _compute_next_hops(graph, la, params)
    return la


def _compute_next_hops(graph: FabricGraph, la: LevelAssignment,
                       params: SpraypointParams) -> None:
    t, key, h = la.destination, params.hash_key, params.h
    nh = la.next_hops

    for u in la.waypoints[0]:
        nh[u] = [t]
    for m in range(1, len(la.waypoints)):
        lower = la.waypoints[m - 1]
        for u in la.waypoints[m]:
            cands = [v for v in graph.neighbors(u) if v in lower]
            nh[u] = deterministic_sample(cands, h, key, t, u, "nh")
    top = la.waypoints[-1]
    for u in la.inner_ring:
        cands = [v for v in graph.neighbors(u) if v in top]
        nh[u] = deterministic_sample(cands, h, key, t, u, "nh")

    if not la.outer_ring:
        return
    # OR: move along shortest paths toward IR. Prefer paths that stay inside
    # IR ∪ OR; fall back to the full graph for OR nodes only reachable
    # through waypoint levels (those still descend toward t, so no loops).
    ring = la.inner_ring | la.outer_ring
    dist = graph.bfs_distances(la.inner_ring, allowed=ring)
    full = graph.bfs_distances(la.inner_ring)
    for u in la.outer_ring:
        table = dist if u in dist else full
        if u not in table:
            continue  # disconnected from the destination
        d = table[u]
        cands = [v for v in graph.neighbors(u) if table.get(v) == d - 1]
        nh[u] = deterministic_sample(cands, h, key, t, u, "nh")
        la.distance_to_ir[u] = d


def check_loop_free(la: LevelAssignment) -> None:
    """Raise if the pointing graph toward ``la.destination`` has a cycle."""
    state: Dict[str, int] = {}  # 1 = on stack, 2 = done

    def visit(u: str, trail: List[str]) -> None:
        if state.get(u) == 2:
            return
        if state.get(u) == 1:
            raise AssertionError(f"forwarding loop toward {la.destination}: "
                                 + " -> ".join(trail + [u]))
        state[u] = 1
        for v in la.next_hops.get(u, []):
            visit(v, trail + [u])
        state[u] = 2

    for u in la.next_hops:
        visit(u, [])
