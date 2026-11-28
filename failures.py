"""Failure injection for experiment runs.

A run with a failure configured measures its experiments three times on the
same network: before the failure (intact), right after it while every switch
still uses its old routes (stale), and after routing has been recomputed
around the failure (rerouted).
"""

import csv
import random
from collections import Counter

# Topology and routing combinations whose recovery after a failure we model.
# ECMP recomputes shortest paths, as BGP data center fabrics do (RFC 7938).
# Spraypoint is a link-state protocol, so every router recomputes its routes
# from the new topology (RNG paper).
SUPPORTED = {
    ("Clos fat-tree", "ECMP shortest path"),
    ("RNG random graph", "Spraypoint"),
}
MODES = ("node", "link")
PHASES = ("intact", "stale", "rerouted")
# A flow that still delivers traffic, but less than this fraction of its
# intact rate, is counted as degraded.
DEGRADED_FRACTION = 0.9


def _of_type(topology, kind):
    return sorted(n for n, a in topology.nodes(data=True) if a.get("type") == kind)


def _edge(u, v):
    return tuple(sorted((u, v), key=str))


def mininet_names(topology):
    """Map each topology node to its Mininet name (h1, h2, ..., s1, s2, ...).

    This uses the same numbering as fnss.to_mininet.
    """
    names = {}
    for kind, prefix in (("host", "h"), ("switch", "s")):
        for index, node in enumerate(_of_type(topology, kind), start=1):
            names[node] = f"{prefix}{index}"
    return names


def choose(topology, mode, count, seed):
    """Randomly pick what fails, and return (failed switches, failed links).

    In node mode, `count` switches fail along with all of their links. In link
    mode, `count` links between two switches fail. The same seed always picks
    the same elements of the same topology.
    """
    switches = _of_type(topology, "switch")
    if mode == "node":
        candidates = switches
    else:
        fabric = set(switches)
        candidates = sorted(
            {_edge(u, v) for u, v in topology.edges() if u in fabric and v in fabric},
            key=str,
        )
    if count > len(candidates):
        raise ValueError(
            f"Cannot fail {count} {mode}s: the topology has only {len(candidates)}"
        )
    chosen = random.Random(seed).sample(candidates, count)
    if mode == "link":
        return [], sorted(chosen, key=str)
    nodes = sorted(chosen, key=str)
    links = {_edge(node, neighbor) for node in nodes for neighbor in topology.neighbors(node)}
    return nodes, sorted(links, key=str)


def lost_hosts(topology, failed_switches):
    """Return the hosts attached to a failed switch; they lose all connectivity."""
    return {
        neighbor
        for switch in failed_switches
        for neighbor in topology.neighbors(switch)
        if topology.nodes[neighbor].get("type") == "host"
    }


def surviving(topology, failed_switches, failed_links):
    """Return a copy of the topology with the failed parts and lost hosts removed."""
    remaining = topology.copy()
    remaining.remove_edges_from(failed_links)
    remaining.remove_nodes_from(
        [*failed_switches, *lost_hosts(topology, failed_switches)]
    )
    return remaining


def apply(net, topology, failed_links):
    """Take the failed links down in the running Mininet network."""
    names = mininet_names(topology)
    for u, v in failed_links:
        net.configLinkStatus(names[u], names[v], "down")


def _rate(flow):
    return 0.0 if flow["flow_failed"] else float(flow["server_received_rate"] or 0.0)


def flow_class(intact, failed, lost):
    """Describe how a failure changed one flow.

    `intact` and `failed` are the flow's results before and after the failure,
    and `lost` holds the Mininet names of hosts that lost their switch.
    """
    if _rate(intact) <= 0:
        return "failed_intact"
    if intact["source_host"] in lost or intact["destination_host"] in lost:
        return "endpoint_lost"
    after = _rate(failed)
    if after <= 0:
        return "fabric_dead"
    if after < DEGRADED_FRACTION * _rate(intact):
        return "degraded"
    return "unaffected"


def compare_flows(phase_flows, lost, csv_path):
    """Write each flow's rate and class in every phase to a CSV file.

    `phase_flows` maps each phase name to that phase's per-flow results.
    Returns, for the stale and rerouted phases, how many flows fell into each
    class.
    """
    by_id = {
        phase: {flow["flow_id"]: flow for flow in flows}
        for phase, flows in phase_flows.items()
    }
    counts = {phase: Counter() for phase in PHASES[1:]}
    columns = ["flow_id", "source_host", "destination_host", "offered_mbps"]
    for phase in PHASES:
        columns.append(f"{phase}_mbps")
        if phase != "intact":
            columns.append(f"{phase}_class")
    with open(csv_path, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=columns)
        writer.writeheader()
        for flow_id, intact in sorted(by_id["intact"].items()):
            row = {
                "flow_id": flow_id,
                "source_host": intact["source_host"],
                "destination_host": intact["destination_host"],
                "offered_mbps": intact["offered_mbps"],
                "intact_mbps": _rate(intact),
            }
            for phase in PHASES[1:]:
                failed = by_id[phase][flow_id]
                row[f"{phase}_mbps"] = _rate(failed)
                row[f"{phase}_class"] = flow_class(intact, failed, lost)
                counts[phase][row[f"{phase}_class"]] += 1
            writer.writerow(row)
    return {phase: dict(counter) for phase, counter in counts.items()}
