"""Spraypoint path-length distribution: static, no Mininet and no traffic.

Reproduces the RNG paper's Fig. 7. The paper fixes a destination ToR and
considers the distribution of path lengths seen by a packet sprayed uniformly
at random from a uniform random source ToR (Theorem E.4), so the unit is a
fabric hop between switches, not a host-to-host path.

For one destination the pointing graph is acyclic and strictly descends
toward the destination (``check_loop_free`` enforces it), so the hop-count
distribution from every switch is one dynamic-programming pass:

    L(t) = {0: 1}
    L(u) = 1 + mean over v in next_hops[u] of L(v)

A source sprays its packet to one of its fabric neighbors (ECMP), and every
switch afterwards picks one of its h pointing next hops, so for source s
with fabric neighbors N(s):

    L(s) = 1 + mean over v in N(s) of L(v)

Switches with no pointing next hop have no Spraypoint path. The paper assumes
an expander where that cannot happen; on small fabrics where ``p * d`` is
comparable to n the level assignment can leave switches in the outer ring
with an empty inner ring. Those pairs are reported as unreachable instead of
being dropped from the sample silently.
"""

import csv
from collections import defaultdict
from pathlib import Path

from routing.spraypoint import (
    DEFAULT_PARAMETERS,
    fabric_from_topology,
    load_protocol,
)


def _protocol():
    """Import the offline protocol helpers after putting them on sys.path."""
    load_protocol()
    from spraypoint import SpraypointParams
    from spraypoint.levels import check_loop_free, compute_levels

    return SpraypointParams, compute_levels, check_loop_free


def _tor_switches(fabric):
    """Switches with at least one attached host, in the fabric's port order."""
    return sorted(
        switch for switch in fabric.ports if fabric.hosts.get(switch)
    )


def _hop_distribution(node, next_hops, memo):
    """Hop counts from ``node`` to the destination, as {hops: probability}."""
    if node in memo:
        return memo[node]
    successors = next_hops.get(node) or []
    if not successors:
        memo[node] = {}
        return memo[node]

    merged = defaultdict(float)
    for successor in successors:
        tail = _hop_distribution(successor, next_hops, memo)
        for hops, probability in tail.items():
            merged[hops + 1] += probability / len(successors)
    memo[node] = dict(merged)
    return memo[node]


def _source_distribution(source, fabric, next_hops, memo):
    """Spray from ``source``; return (distribution, dropped_packet_fraction).

    The distribution is conditional on the packet reaching the destination.
    ``dropped_packet_fraction`` is the share of the source's spray branches
    that have no pointing path, i.e. packets Spraypoint would drop.
    """
    neighbors = fabric.neighbors(source)
    if not neighbors:
        return {}, 1.0

    merged = defaultdict(float)
    for neighbor in neighbors:
        tail = _hop_distribution(neighbor, next_hops, memo)
        for hops, probability in tail.items():
            merged[hops + 1] += probability / len(neighbors)

    live = sum(merged.values())
    if live <= 0:
        return {}, 1.0
    return {hops: probability / live for hops, probability in merged.items()}, (
        1.0 - live
    )


def _weighted_median(distribution):
    if not distribution:
        return None
    cumulative = 0.0
    for hops in sorted(distribution):
        cumulative += distribution[hops]
        if cumulative >= 0.5:
            return hops
    return max(distribution)


def _spread(values):
    if not values:
        return {"min": None, "median": None, "mean": None, "max": None}
    ordered = sorted(values)
    return {
        "min": ordered[0],
        "median": ordered[len(ordered) // 2]
        if len(ordered) % 2
        else (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2,
        "mean": sum(ordered) / len(ordered),
        "max": ordered[-1],
    }


def analyze(topology, parameters=None):
    """Return the Spraypoint path-length distribution for ``topology``."""
    SpraypointParams, compute_levels, check_loop_free = _protocol()
    fabric = fabric_from_topology(topology)
    tors = _tor_switches(fabric)
    if len(tors) < 2:
        raise ValueError(
            "Spraypoint path length analysis needs at least two ToR switches"
        )

    params = SpraypointParams(**{**DEFAULT_PARAMETERS, **(parameters or {})})

    pairs = 0
    unreachable_pairs = 0
    dropped_fraction = 0.0
    aggregate = defaultdict(float)
    pair_means = []

    for destination in tors:
        levels = compute_levels(fabric, destination, params)
        check_loop_free(levels)
        memo = {destination: {0: 1.0}}

        for source in tors:
            if source == destination:
                continue
            pairs += 1
            distribution, dropped = _source_distribution(
                source, fabric, levels.next_hops, memo
            )
            dropped_fraction += dropped
            if not distribution:
                unreachable_pairs += 1
                continue

            pair_means.append(
                sum(hops * probability for hops, probability in distribution.items())
            )
            for hops, probability in distribution.items():
                aggregate[hops] += probability

    total = sum(aggregate.values())
    hop_distribution = []
    if total > 0:
        cumulative = 0.0
        for hops in sorted(aggregate):
            fraction = aggregate[hops] / total
            cumulative += fraction
            hop_distribution.append(
                {
                    "hops": hops,
                    "path_fraction": fraction,
                    "cumulative_fraction": cumulative,
                }
            )
    distribution_by_hop = {
        entry["hops"]: entry["path_fraction"] for entry in hop_distribution
    }
    mean_hops = (
        sum(hops * fraction for hops, fraction in distribution_by_hop.items())
        if distribution_by_hop
        else None
    )
    return {
        "definition": (
            "sprayed path lengths between ToR switches: fix a destination, "
            "spray from a uniform source to one fabric neighbour, then follow "
            "Spraypoint's h pointing next hops; aggregated over uniformly "
            "sampled source/destination ToR pairs"
        ),
        "hop_unit": "fabric hops between ToR switches",
        "tor_count": len(tors),
        "source_destination_pairs": pairs,
        "unreachable_pairs": unreachable_pairs,
        "unreachable_fraction": unreachable_pairs / pairs if pairs else None,
        "dropped_spray_fraction": dropped_fraction / pairs if pairs else None,
        "hop_distribution": hop_distribution,
        "mean_hops": mean_hops,
        "median_hops": _weighted_median(distribution_by_hop),
        "max_hops": max(distribution_by_hop) if distribution_by_hop else None,
        "pair_mean_hops": _spread(pair_means),
    }


def run(topology, output_dir, routing):
    """Analyze and write ``path-length-distribution.csv`` for one run."""
    if routing.get("name") != "Spraypoint":
        raise ValueError(
            "Spraypoint path length distribution requires routing.name to be "
            "'Spraypoint'"
        )
    result = analyze(topology, routing.get("parameters"))
    result["routing"] = {
        "name": routing["name"],
        "parameters": dict(routing.get("parameters") or {}),
    }

    csv_path = Path(output_dir) / "path-length-distribution.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=("hops", "path_fraction", "cumulative_fraction"),
        )
        writer.writeheader()
        writer.writerows(result["hop_distribution"])

    result["csv_file"] = csv_path.name
    return result
