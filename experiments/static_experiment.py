"""Static fabric analysis: no Mininet, no traffic pattern.

Everything here depends only on the topology, so it runs without root and
without a network build. It answers, before spending minutes on net.build():

* per-switch downstream/upstream oversubscription, and the maximum
* ToR-pair path diversity: edge-disjoint path count, shortest-path hops,
  aggregate (max-flow) pair capacity, and per-path bottleneck
* the implied single-path worst case: a ToR's full offered rate divided by
  the capacity of one path, versus divided by the aggregate pair capacity.
  That is the gap single-path hashing (ECMP) leaves on the table and
  multi-path spraying (Spraypoint) can use.
* capacity, fanout and connectivity summary used to sanity-check a fabric

Edge-disjoint paths and pair capacity need one max-flow solve each per pair
(~60 ms per pair on a 128-ToR Clos), so at most ``pair_sample`` pairs are
analyzed; the sample is deterministic for a given ``pair_seed``.
"""

import csv
import itertools
import math
import random
import statistics
from pathlib import Path

import networkx


SWITCH_LINK_TYPES = {
    "edge": ("edge_leaf", "aggregation_edge"),
    "aggregation": ("aggregation_edge", "core_aggregation"),
    "leaf": ("host_leaf", "leaf_spine"),
    "tor": ("host_tor", "tor_middle_block"),
    "middle_block": ("tor_middle_block", "dcni"),
    "rng": ("host_link", "fabric"),
}

DEFAULT_PAIR_SAMPLE = 32
DEFAULT_PAIR_SEED = 1


def _capacity_mbps(attributes, source, target):
    capacity = attributes.get("capacity")
    if (
        not isinstance(capacity, (int, float))
        or isinstance(capacity, bool)
        or not math.isfinite(capacity)
        or capacity <= 0
    ):
        raise ValueError(
            f"Link {source!r} - {target!r} must have a positive finite "
            "capacity in Mbps"
        )
    return float(capacity)


def _is_host(topology, node):
    return topology.nodes[node].get("type") == "host"


def _host_links(topology):
    """Offer rate per ToR: sum of the capacities of its host-facing links."""
    offered = {}
    for source, target, attributes in topology.edges(data=True):
        if _is_host(topology, source) == _is_host(topology, target):
            continue
        tor = target if _is_host(topology, source) else source
        capacity = _capacity_mbps(attributes, source, target)
        offered[tor] = offered.get(tor, 0.0) + capacity
    return offered


def _switch_table(topology):
    """Downstream/upstream capacity per non-spine switch."""
    switches = []
    for node, node_attributes in topology.nodes(data=True):
        if node_attributes.get("type") != "switch":
            continue

        layer = node_attributes.get("layer")
        if layer in ("core", "spine"):
            continue
        if layer not in SWITCH_LINK_TYPES:
            raise ValueError(
                f"Switch {node!r} has unsupported layer {layer!r}"
            )

        downstream_type, upstream_type = SWITCH_LINK_TYPES[layer]
        downstream_capacity = 0.0
        upstream_capacity = 0.0
        for source, target, link_attributes in topology.edges(
            node, data=True
        ):
            link_type = link_attributes.get("type")
            capacity = _capacity_mbps(link_attributes, source, target)
            if link_type == downstream_type:
                downstream_capacity += capacity
            elif link_type == upstream_type:
                upstream_capacity += capacity

        if downstream_capacity <= 0 or upstream_capacity <= 0:
            raise ValueError(
                f"Switch {node!r} is missing downstream or upstream links"
            )

        switches.append(
            {
                "node": node,
                "layer": layer,
                "downstream_capacity_mbps": downstream_capacity,
                "upstream_capacity_mbps": upstream_capacity,
                "oversubscription_ratio": (
                    downstream_capacity / upstream_capacity
                ),
            }
        )

    if not switches:
        raise ValueError(
            "Topology has no edge or aggregation switches to analyze"
        )
    return switches


def _fabric_graph(topology):
    """Undirected capacity graph used for edge-disjoint path search."""
    graph = networkx.Graph()
    for source, target, attributes in topology.edges(data=True):
        graph.add_edge(
            source,
            target,
            capacity=_capacity_mbps(attributes, source, target),
        )
    return graph


def _directed_fabric(graph):
    """Anti-parallel arcs with the same capacity: undirected max-flow."""
    directed = networkx.DiGraph()
    for source, target, attributes in graph.edges(data=True):
        capacity = attributes["capacity"]
        directed.add_edge(source, target, capacity=capacity)
        directed.add_edge(target, source, capacity=capacity)
    return directed


def _display_ratio(numerator, denominator):
    if denominator is None or denominator <= 0:
        return "inf"
    return numerator / denominator


def _spread(values):
    values = [value for value in values if value is not None]
    if not values:
        return {"min": None, "median": None, "mean": None, "max": None}
    return {
        "min": min(values),
        "median": statistics.median(values),
        "mean": statistics.fmean(values),
        "max": max(values),
    }


def _pair_row(graph, directed, source, target, offered_mbps):
    hops = []
    bottlenecks = []
    try:
        paths = list(networkx.edge_disjoint_paths(graph, source, target))
    except networkx.NetworkXNoPath:
        paths = []
    for path in paths:
        hops.append(len(path) - 1)
        bottlenecks.append(
            min(graph[u][v]["capacity"] for u, v in zip(path, path[1:]))
        )

    if paths:
        capacity, _flow = networkx.maximum_flow(directed, source, target)
    else:
        capacity = 0.0

    weakest = min(bottlenecks) if bottlenecks else None
    strongest = max(bottlenecks) if bottlenecks else None
    return {
        "source": str(source),
        "destination": str(target),
        "source_offered_mbps": offered_mbps,
        "edge_disjoint_paths": len(paths),
        "shortest_path_hops": min(hops) if hops else None,
        "pair_capacity_mbps": capacity,
        "single_path_capacity_min_mbps": weakest,
        "single_path_capacity_max_mbps": strongest,
        "aggregate_oversubscription_ratio": _display_ratio(
            offered_mbps, capacity
        ),
        "single_path_oversubscription_ratio": _display_ratio(
            offered_mbps, weakest
        ),
    }


def _pair_rank(row):
    return (row["pair_capacity_mbps"], row["edge_disjoint_paths"])


def _pair_table(topology, graph, directed, tor_offer, pair_sample, pair_seed):
    """Analyze every ToR pair, or a deterministic sample of them."""
    tors = sorted(tor_offer)
    pairs = list(itertools.combinations(tors, 2))
    total_pairs = len(pairs)
    if not total_pairs:
        return [], 0, False
    sampled = total_pairs > pair_sample
    if sampled:
        pairs = random.Random(pair_seed).sample(pairs, pair_sample)
        pairs.sort()

    rows = [
        _pair_row(graph, directed, source, target, tor_offer[source])
        for source, target in pairs
    ]
    return rows, total_pairs, sampled


def _topology_summary(topology, graph, tor_offer):
    nodes_by_type = {}
    nodes_by_layer = {}
    for _, attributes in topology.nodes(data=True):
        node_type = str(attributes.get("type"))
        nodes_by_type[node_type] = nodes_by_type.get(node_type, 0) + 1
        layer = str(attributes.get("layer"))
        nodes_by_layer[layer] = nodes_by_layer.get(layer, 0) + 1

    link_types = {}
    capacities = []
    for source, target, attributes in topology.edges(data=True):
        link_type = str(attributes.get("type"))
        link_types[link_type] = link_types.get(link_type, 0) + 1
        capacities.append(_capacity_mbps(attributes, source, target))

    host_capacity = sum(tor_offer.values())
    fabric_capacity = sum(capacities) - host_capacity
    hosts_per_tor = {
        tor: sum(
            1
            for neighbour in topology.neighbors(tor)
            if _is_host(topology, neighbour)
        )
        for tor in tor_offer
    }
    return {
        "node_count": topology.number_of_nodes(),
        "link_count": topology.number_of_edges(),
        "nodes_by_type": dict(sorted(nodes_by_type.items())),
        "nodes_by_layer": dict(sorted(nodes_by_layer.items())),
        "links_by_type": dict(sorted(link_types.items())),
        "tor_count": len(tor_offer),
        "host_capacity_mbps": host_capacity,
        "fabric_link_capacity_mbps": fabric_capacity,
        "host_to_fabric_capacity_ratio": _display_ratio(
            host_capacity, fabric_capacity
        ),
        "link_capacity_mbps": {
            **_spread(capacities),
            "distinct_values": len(set(capacities)),
        },
        "hosts_per_tor": _spread(list(hosts_per_tor.values())),
        "connected_components": networkx.number_connected_components(graph),
        "is_connected": networkx.is_connected(graph),
    }


def analyze(
    topology, pair_sample=DEFAULT_PAIR_SAMPLE, pair_seed=DEFAULT_PAIR_SEED
):
    """Return every static metric for ``topology``."""
    if (
        not isinstance(pair_sample, int)
        or isinstance(pair_sample, bool)
        or pair_sample < 1
    ):
        raise ValueError("pair_sample must be a positive integer")
    if not isinstance(pair_seed, int) or isinstance(pair_seed, bool):
        raise ValueError("pair_seed must be an integer")

    switches = _switch_table(topology)
    host_offer = _host_links(topology)
    graph = _fabric_graph(topology)
    directed = _directed_fabric(graph)

    table_by_node = {switch["node"]: switch for switch in switches}
    tor_offer = {
        node: host_offer[node] for node in host_offer if node in table_by_node
    }
    if not tor_offer:
        # No host links (e.g. a bare switch graph): treat every non-spine
        # switch as a traffic node and use its downstream capacity.
        tor_offer = {
            node: switch["downstream_capacity_mbps"]
            for node, switch in table_by_node.items()
        }

    pairs, pair_count, sampled = _pair_table(
        topology, graph, directed, tor_offer, pair_sample, pair_seed
    )
    worst_pair = min(pairs, key=_pair_rank) if pairs else None
    best_pair = max(pairs, key=_pair_rank) if pairs else None
    maximum_ratio = max(
        switch["oversubscription_ratio"] for switch in switches
    )
    public_switches = [
        {**switch, "node": str(switch["node"])} for switch in switches
    ]
    return {
        "ratio_definition": (
            "sum of downstream link capacities divided by sum of upstream "
            "link capacities for each edge and aggregation switch"
        ),
        "capacity_unit": "Mbps",
        "maximum_oversubscription_ratio": maximum_ratio,
        "max_oversubscription_switch": next(
            str(switch["node"])
            for switch in switches
            if switch["oversubscription_ratio"] == maximum_ratio
        ),
        "switch_count": len(switches),
        "switches": public_switches,
        "topology_summary": _topology_summary(topology, graph, tor_offer),
        "pair_analysis": {
            "analyzed_pairs": len(pairs),
            "total_pairs": pair_count,
            "sampled": sampled,
            "sample_seed": pair_seed,
            "pair_sample_limit": pair_sample,
            "note": None if pairs else "topology has fewer than two ToRs",
            "definition": (
                "per ToR pair: edge-disjoint paths (Menger), aggregate "
                "capacity (max flow with real link capacities), and the "
                "bottleneck of the weakest and strongest single path"
            ),
            "edge_disjoint_paths": _spread(
                [row["edge_disjoint_paths"] for row in pairs]
            ),
            "shortest_path_hops": _spread(
                row["shortest_path_hops"] for row in pairs
            ),
            "pair_capacity_mbps": _spread(
                row["pair_capacity_mbps"] for row in pairs
            ),
            "single_path_capacity_min_mbps": _spread(
                row["single_path_capacity_min_mbps"] for row in pairs
            ),
            "single_path_capacity_max_mbps": _spread(
                row["single_path_capacity_max_mbps"] for row in pairs
            ),
            "worst_pair": worst_pair,
            "best_pair": best_pair,
        },
        "tor_pairs": pairs,
    }


def run(
    topology,
    output_dir,
    pair_sample=DEFAULT_PAIR_SAMPLE,
    pair_seed=DEFAULT_PAIR_SEED,
):
    """Write switch and ToR-pair CSV tables; return the aggregate metrics."""
    result = analyze(topology, pair_sample=pair_sample, pair_seed=pair_seed)
    output_dir = Path(output_dir)

    switch_csv = output_dir / "oversubscription.csv"
    switch_columns = (
        "node",
        "layer",
        "downstream_capacity_mbps",
        "upstream_capacity_mbps",
        "oversubscription_ratio",
    )
    with switch_csv.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=switch_columns)
        writer.writeheader()
        writer.writerows(result["switches"])

    pair_csv = output_dir / "tor-pairs.csv"
    pair_columns = (
        "source",
        "destination",
        "source_offered_mbps",
        "edge_disjoint_paths",
        "shortest_path_hops",
        "pair_capacity_mbps",
        "single_path_capacity_min_mbps",
        "single_path_capacity_max_mbps",
        "aggregate_oversubscription_ratio",
        "single_path_oversubscription_ratio",
    )
    with pair_csv.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=pair_columns)
        writer.writeheader()
        writer.writerows(result["tor_pairs"])

    return {
        key: value for key, value in result.items() if key != "tor_pairs"
    } | {
        "csv_file": switch_csv.name,
        "pair_csv_file": pair_csv.name,
    }
