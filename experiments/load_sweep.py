import csv
import math
import random
from pathlib import Path

from . import iperf


# Switch layer -> link type that attaches its hosts.
HOST_LINK_TYPES = {
    "edge": "edge_leaf",
    "rng": "host_link",
}


def _leaf_hosts(topology):
    leaves = sorted(
        node
        for node, attributes in topology.nodes(data=True)
        if attributes.get("type") == "switch"
        and attributes.get("layer") in HOST_LINK_TYPES
    )
    hosts_by_leaf = {leaf: [] for leaf in leaves}
    for source, target, attributes in topology.edges(data=True):
        if attributes.get("type") not in HOST_LINK_TYPES.values():
            continue
        leaf, host = (
            (source, target)
            if source in hosts_by_leaf
            else (target, source)
        )
        if leaf not in hosts_by_leaf or topology.nodes[host].get("type") != "host":
            raise ValueError("Topology has an invalid host-to-leaf connection")
        hosts_by_leaf[leaf].append(host)
    if len(leaves) < 2 or any(not hosts for hosts in hosts_by_leaf.values()):
        raise ValueError("Load sweep requires at least two leaves with hosts")
    return leaves, hosts_by_leaf


def _select_active_leaves(leaves, active_count, seed):
    return random.Random(seed).sample(leaves, len(leaves))[:active_count]


def _traffic_for_active_leaves(
    topology,
    leaves,
    hosts_by_leaf,
    active_count,
    seed,
    traffic_builder,
    traffic_parameters,
):
    active_leaves = _select_active_leaves(leaves, active_count, seed)
    active_topology = topology.subgraph(active_leaves).copy()
    for leaf in active_leaves:
        active_topology.nodes[leaf]["type"] = "host"
    parameters = {**traffic_parameters, "seed": seed}
    if "flow_count" in parameters:
        parameters["flow_count"] = min(
            parameters["flow_count"], len(active_leaves) * (len(active_leaves) - 1)
        )
    traffic_matrix = traffic_builder(active_topology, **parameters)
    return active_leaves, traffic_matrix


def _host_capacity(topology):
    capacities = {}
    for source, target, attributes in topology.edges(data=True):
        if attributes.get("type") not in HOST_LINK_TYPES.values():
            continue
        host = source if topology.nodes[source].get("type") == "host" else target
        capacities[host] = float(attributes["capacity"])
    if not capacities:
        raise ValueError("Topology has no host links to size the load sweep")
    return capacities


def _flows_for_matrix(traffic_matrix, host_names, hosts_by_leaf, capacities):
    leaf_pairs = list(traffic_matrix.flows())
    if not leaf_pairs:
        raise ValueError("Traffic matrix has no flows for the selected leaves")
    out_degree = {}
    leaf_capacities = {
        leaf: sum(capacities[host] for host in hosts)
        for leaf, hosts in hosts_by_leaf.items()
    }
    for source_leaf, destination_leaf in leaf_pairs:
        if (
            source_leaf not in hosts_by_leaf
            or destination_leaf not in hosts_by_leaf
        ):
            raise ValueError(
                "Traffic flow references a leaf without a host mapping"
            )
        out_degree[source_leaf] = out_degree.get(source_leaf, 0) + 1

    flows = {}
    flow_id = 1
    leaves = sorted(hosts_by_leaf)
    for source_leaf, destination_leaf in leaf_pairs:
        source_hosts = sorted(hosts_by_leaf[source_leaf])
        destination_hosts = sorted(hosts_by_leaf[destination_leaf])
        demand = leaf_capacities[source_leaf] / out_degree[source_leaf]
        source_leaf_offset = leaves.index(source_leaf)
        for host_index, source_host in enumerate(source_hosts):
            destination_host = destination_hosts[
                (host_index + source_leaf_offset) % len(destination_hosts)
            ]
            if source_host not in host_names or destination_host not in host_names:
                raise ValueError("Leaf traffic references an unmapped Mininet host")
            host_demand = (
                demand
                * capacities[source_host]
                / leaf_capacities[source_leaf]
            )
            flows[flow_id] = (
                source_host,
                destination_host,
                host_demand,
            )
            flow_id += 1
    return flows


def _mean(values):
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def _summary(
    active_fraction,
    flows,
    measured,
    active_leaf_count=None,
    host_leaf=None,
):
    offered = sum(demand for _, _, demand in flows.values())
    sent = sum(measured[flow_id][1] for flow_id in flows)
    delivered = sum(measured[flow_id][0] for flow_id in flows)
    if host_leaf is None:
        host_leaf = {}
    leaf_pair_rates = {}
    for flow_id, (source, destination, demand) in flows.items():
        leaf_pair = (
            host_leaf.get(source, source),
            host_leaf.get(destination, destination),
        )
        pair_offered, pair_delivered = leaf_pair_rates.get(leaf_pair, (0, 0))
        leaf_pair_rates[leaf_pair] = (
            pair_offered + demand,
            pair_delivered + measured[flow_id][0],
        )
    minimum_flow_fraction = min(
        min(1.0, pair_delivered / pair_offered)
        for pair_offered, pair_delivered in leaf_pair_rates.values()
    )
    ratio = math.inf if minimum_flow_fraction <= 0 else 1 / minimum_flow_fraction
    aggregate_ratio = math.inf if delivered <= 0 else offered / delivered
    return {
        "active_fraction": active_fraction,
        "active_leaf_count": active_leaf_count,
        "flow_count": len(flows),
        "offered_mbps": offered,
        "sent_mbps": sent,
        "delivered_mbps": delivered,
        "failed_flow_count": sum(
            measured[flow_id][4] is not None for flow_id in flows
        ),
        "minimum_flow_rate_fraction": minimum_flow_fraction,
        "oversubscription_ratio": ratio,
        "aggregate_offered_to_delivered_ratio": aggregate_ratio,
        "packet_loss_percent": 100 * (1 - delivered / sent) if sent else None,
        "mean_idle_rtt_ms": _mean(
            [measured[flow_id][2]["avg_rtt_ms"] for flow_id in flows]
        ),
        "mean_loaded_rtt_ms": _mean(
            [measured[flow_id][3]["avg_rtt_ms"] for flow_id in flows]
        ),
    }


def _display_ratio(value):
    return "inf" if math.isinf(value) else value


def _combine_repetitions(rows):
    ratios = [row["oversubscription_ratio"] for row in rows]
    aggregate_ratios = [
        row["aggregate_offered_to_delivered_ratio"] for row in rows
    ]
    combined = {
        "active_fraction": rows[0]["active_fraction"],
        "active_leaf_count": rows[0]["active_leaf_count"],
        "matrix_repetitions": len(rows),
        "flow_count": round(_mean([row["flow_count"] for row in rows])),
        "offered_mbps": _mean([row["offered_mbps"] for row in rows]),
        "sent_mbps": _mean([row["sent_mbps"] for row in rows]),
        "delivered_mbps": _mean([row["delivered_mbps"] for row in rows]),
        "failed_flow_count": round(
            _mean([row["failed_flow_count"] for row in rows])
        ),
        "minimum_flow_rate_fraction": _mean(
            [row["minimum_flow_rate_fraction"] for row in rows]
        ),
        "oversubscription_ratio": _mean(ratios),
        "min_oversubscription_ratio": min(ratios),
        "max_oversubscription_ratio": max(ratios),
        "aggregate_offered_to_delivered_ratio": _mean(aggregate_ratios),
        "min_aggregate_offered_to_delivered_ratio": min(aggregate_ratios),
        "max_aggregate_offered_to_delivered_ratio": max(aggregate_ratios),
        "packet_loss_percent": _mean(
            [row["packet_loss_percent"] for row in rows]
        ),
        "mean_idle_rtt_ms": _mean(
            [row["mean_idle_rtt_ms"] for row in rows]
        ),
        "mean_loaded_rtt_ms": _mean(
            [row["mean_loaded_rtt_ms"] for row in rows]
        ),
    }
    for key in (
        "oversubscription_ratio",
        "min_oversubscription_ratio",
        "max_oversubscription_ratio",
        "aggregate_offered_to_delivered_ratio",
        "min_aggregate_offered_to_delivered_ratio",
        "max_aggregate_offered_to_delivered_ratio",
    ):
        combined[key] = _display_ratio(combined[key])
    return combined


def run(
    net,
    traffic_matrix,
    host_names,
    topology,
    duration_seconds,
    active_fractions,
    matrix_repetitions,
    traffic_name,
    traffic_builder,
    traffic_parameters,
    output_dir,
):
    """Estimate worst-flow oversubscription for active leaf fractions."""
    del traffic_matrix
    output_dir = Path(output_dir)
    leaves, hosts_by_leaf = _leaf_hosts(topology)
    capacities = _host_capacity(topology)
    host_leaf = {
        host: leaf for leaf, hosts in hosts_by_leaf.items() for host in hosts
    }
    fractions = sorted({float(fraction) for fraction in active_fractions})
    active_counts = sorted(
        {
            min(len(leaves), max(2, math.ceil(len(leaves) * fraction)))
            for fraction in fractions
        }
    )
    if traffic_name == "Matching host pairs":
        active_counts = sorted(
            {max(2, active_count - active_count % 2) for active_count in active_counts}
        )
    active_counts = sorted(set(active_counts))
    base_seed = traffic_parameters["seed"]
    log_path = output_dir / "load-sweep.log"
    csv_path = output_dir / "load-sweep.csv"
    columns = (
        "active_fraction",
        "active_leaf_count",
        "matrix_repetitions",
        "flow_count",
        "offered_mbps",
        "sent_mbps",
        "delivered_mbps",
        "failed_flow_count",
        "minimum_flow_rate_fraction",
        "oversubscription_ratio",
        "min_oversubscription_ratio",
        "max_oversubscription_ratio",
        "aggregate_offered_to_delivered_ratio",
        "min_aggregate_offered_to_delivered_ratio",
        "max_aggregate_offered_to_delivered_ratio",
        "packet_loss_percent",
        "mean_idle_rtt_ms",
        "mean_loaded_rtt_ms",
    )
    measurements = []
    with log_path.open("w", encoding="utf-8") as log_file, csv_path.open(
        "w", newline="", encoding="utf-8"
    ) as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=columns)
        writer.writeheader()
        csv_file.flush()
        for active_count in active_counts:
            fraction = active_count / len(leaves)
            rows = []
            for repetition in range(matrix_repetitions):
                seed = base_seed + repetition
                active_leaves, matrix = _traffic_for_active_leaves(
                    topology,
                    leaves,
                    hosts_by_leaf,
                    active_count,
                    seed,
                    traffic_builder,
                    traffic_parameters,
                )
                flows = _flows_for_matrix(
                    matrix,
                    host_names,
                    {
                        leaf: hosts_by_leaf[leaf]
                        for leaf in active_leaves
                    },
                    capacities,
                )
                print(
                    f"*** Load sweep: {fraction:.0%} active leaves "
                    f"({len(active_leaves)}/{len(leaves)}), matrix "
                    f"{repetition + 1}/{matrix_repetitions} "
                    f"({len(flows)} flows)"
                )
                log_file.write(
                    f"--- Active fraction {fraction:.0%}: "
                    f"{len(active_leaves)}/{len(leaves)} leaves, "
                    f"matrix {repetition + 1}/{matrix_repetitions}, "
                    f"{len(flows)} flows ---\n"
                )
                leaf_out_degree = {}
                for source_leaf, _ in matrix.flows():
                    leaf_out_degree[source_leaf] = (
                        leaf_out_degree.get(source_leaf, 0) + 1
                    )
                for source_leaf, destination_leaf in matrix.flows():
                    leaf_offered = sum(
                        capacities[host]
                        for host in hosts_by_leaf[source_leaf]
                    ) / leaf_out_degree[source_leaf]
                    log_file.write(
                        f"leaf_flow={source_leaf}->{destination_leaf}, "
                        f"offered_mbps={leaf_offered}\n"
                    )
                log_file.flush()
                idle_latency = iperf.measure_idle(net, flows, host_names)
                measured = iperf.run_flows_concurrently(
                    net,
                    flows,
                    host_names,
                    duration_seconds,
                    log_file,
                    idle_latency,
                )
                rows.append(
                    _summary(
                        fraction,
                        flows,
                        measured,
                        len(active_leaves),
                        host_leaf,
                    )
                )
            result = _combine_repetitions(rows)
            measurements.append(result)
            writer.writerow(result)
            csv_file.flush()

    return {
        "active_fractions": [count / len(leaves) for count in active_counts],
        "matrix_repetitions": matrix_repetitions,
        "leaf_count": len(leaves),
        "log_file": log_path.name,
        "csv_file": csv_path.name,
        "measurements": measurements,
    }
