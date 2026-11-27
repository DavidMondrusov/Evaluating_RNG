import csv
import math
import random
from contextlib import nullcontext
from pathlib import Path

from traffic import _support

from . import iperf

_leaf_hosts = _support.leaf_hosts
_host_capacity = _support.host_capacity


class _ServerMode:
    graph = {"host_mode": "server"}


def _flows_for_matrix(traffic_matrix, host_names, hosts_by_leaf, capacities):
    del host_names
    return _support.expand_leaf_flows(
        traffic_matrix, _ServerMode(), hosts_by_leaf, capacities
    )


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
    traffic_scope="tor",
):
    active_leaves = _select_active_leaves(leaves, active_count, seed)
    if traffic_scope == "tor":
        active_topology = _support.leaf_subgraph(topology, active_leaves)
    else:
        active_hosts = [
            host for leaf in active_leaves for host in hosts_by_leaf[leaf]
        ]
        active_topology = topology.subgraph(active_hosts).copy()
    parameters = {**traffic_parameters, "seed": seed}
    if "flow_count" in parameters:
        parameters["flow_count"] = min(
            parameters["flow_count"], len(active_leaves) * (len(active_leaves) - 1)
        )
    traffic_matrix = traffic_builder(active_topology, **parameters)
    return active_leaves, traffic_matrix


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
    pair_rates = {}
    for flow_id, (source, destination, demand) in flows.items():
        leaf_pair = (
            host_leaf.get(source, source),
            host_leaf.get(destination, destination),
        )
        pair_offered, pair_delivered, pair_complete = pair_rates.get(
            leaf_pair, (0.0, 0.0, True)
        )
        pair_rates[leaf_pair] = (
            pair_offered + demand,
            pair_delivered + measured[flow_id][0],
            pair_complete and measured[flow_id][4] is None,
        )
    complete_pairs = [
        (pair_offered, pair_delivered)
        for pair_offered, pair_delivered, complete in pair_rates.values()
        if complete and pair_offered > 0
    ]
    minimum_flow_fraction = (
        min(
            min(1.0, pair_delivered / pair_offered)
            for pair_offered, pair_delivered in complete_pairs
        )
        if complete_pairs
        else None
    )
    ratio = (
        1 / minimum_flow_fraction
        if minimum_flow_fraction is not None and minimum_flow_fraction > 0
        else None
    )
    measured_flow_count = sum(
        measured[flow_id][4] is None for flow_id in flows
    )
    measured_offered = sum(
        demand
        for flow_id, (_, _, demand) in flows.items()
        if measured[flow_id][4] is None
    )
    measured_delivered = sum(
        measured[flow_id][0]
        for flow_id in flows
        if measured[flow_id][4] is None
    )
    aggregate_ratio = (
        measured_offered / measured_delivered
        if measured_delivered > 0
        else None
    )
    return {
        "active_fraction": active_fraction,
        "active_leaf_count": active_leaf_count,
        "flow_count": len(flows),
        "offered_mbps": offered,
        "sent_mbps": sent,
        "delivered_mbps": delivered,
        "failed_flow_count": len(flows) - measured_flow_count,
        "measured_flow_count": measured_flow_count,
        "valid": (
            measured_flow_count == len(flows)
            and ratio is not None
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


def _combine_repetitions(rows):
    valid_rows = [row for row in rows if row["valid"]]
    ratio_rows = [
        row for row in rows if row["oversubscription_ratio"] is not None
    ]
    ratios = [row["oversubscription_ratio"] for row in ratio_rows]
    aggregate_ratios = [
        row["aggregate_offered_to_delivered_ratio"] for row in ratio_rows
    ]
    return {
        "active_fraction": rows[0]["active_fraction"],
        "active_leaf_count": rows[0]["active_leaf_count"],
        "matrix_repetitions": len(rows),
        "flow_count": round(_mean([row["flow_count"] for row in rows])),
        "offered_mbps": _mean([row["offered_mbps"] for row in rows]),
        "sent_mbps": _mean([row["sent_mbps"] for row in rows]),
        "delivered_mbps": _mean([row["delivered_mbps"] for row in rows]),
        "failed_flow_count": max(
            [row["failed_flow_count"] for row in rows]
        ),
        "measured_flow_count": round(
            _mean([row["measured_flow_count"] for row in rows])
        ),
        "valid": len(valid_rows) == len(rows),
        "minimum_flow_rate_fraction": _mean(
            [row["minimum_flow_rate_fraction"] for row in ratio_rows]
        ),
        "oversubscription_ratio": _mean(ratios),
        "min_oversubscription_ratio": min(ratios) if ratios else None,
        "max_oversubscription_ratio": max(ratios) if ratios else None,
        "aggregate_offered_to_delivered_ratio": _mean(aggregate_ratios),
        "min_aggregate_offered_to_delivered_ratio": (
            min(aggregate_ratios) if aggregate_ratios else None
        ),
        "max_aggregate_offered_to_delivered_ratio": (
            max(aggregate_ratios) if aggregate_ratios else None
        ),
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
    traffic_scope="tor",
    status=None,
):
    """Estimate worst-flow oversubscription for active leaf fractions."""
    del traffic_matrix
    output_dir = Path(output_dir)
    phase = status.phase if status else (lambda *args, **kwargs: nullcontext())
    leaves, hosts_by_leaf = _support.leaf_hosts(topology)
    capacities = _support.host_capacity(topology)
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
        "oversubscription_ratio",
        "min_oversubscription_ratio",
        "max_oversubscription_ratio",
        "aggregate_offered_to_delivered_ratio",
        "min_aggregate_offered_to_delivered_ratio",
        "max_aggregate_offered_to_delivered_ratio",
        "minimum_flow_rate_fraction",
        "delivered_mbps",
        "offered_mbps",
        "sent_mbps",
        "packet_loss_percent",
        "mean_loaded_rtt_ms",
        "mean_idle_rtt_ms",
        "active_leaf_count",
        "flow_count",
        "measured_flow_count",
        "failed_flow_count",
        "valid",
        "matrix_repetitions",
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
                with phase(
                    "sweep matrix",
                    active=active_count,
                    repetition=repetition + 1,
                ):
                    active_leaves, matrix = _traffic_for_active_leaves(
                        topology,
                        leaves,
                        hosts_by_leaf,
                        active_count,
                        seed,
                        traffic_builder,
                        traffic_parameters,
                        traffic_scope,
                    )
                    if traffic_scope == "tor":
                        flows = _support.expand_leaf_flows(
                            matrix,
                            topology,
                            {
                                leaf: hosts_by_leaf[leaf]
                                for leaf in active_leaves
                            },
                            capacities,
                        )
                    else:
                        flows = _support.host_flows(matrix, capacities)
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
                out_degree = {}
                for source, _ in matrix.flows():
                    out_degree[source] = out_degree.get(source, 0) + 1
                pair_kind = "leaf" if traffic_scope == "tor" else "host"
                for source, destination in matrix.flows():
                    if traffic_scope == "tor":
                        offered = sum(
                            capacities[host]
                            for host in hosts_by_leaf[source]
                        ) / out_degree[source]
                    else:
                        offered = capacities[source] / out_degree[source]
                    log_file.write(
                        f"{pair_kind}_flow={source}->{destination}, "
                        f"offered_mbps={offered}\n"
                    )
                log_file.flush()
                with phase("idle ping", flows=len(flows)):
                    idle_latency = iperf.measure_idle(net, flows, host_names)
                with phase(
                    "iperf", flows=len(flows), duration=duration_seconds
                ):
                    run_kwargs = {"status": status} if status else {}
                    measured = iperf.run_flows_concurrently(
                        net,
                        flows,
                        host_names,
                        duration_seconds,
                        log_file,
                        idle_latency,
                        **run_kwargs,
                    )
                with phase("summary"):
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
            if not result["valid"]:
                if result["oversubscription_ratio"] is None:
                    message = (
                        f"*** Load sweep: no usable ratio at {fraction:.0%} "
                        f"({result['failed_flow_count']} failed flows); "
                        "ratio columns left empty"
                    )
                else:
                    message = (
                        f"*** Load sweep: partial measurement at "
                        f"{fraction:.0%} ({result['failed_flow_count']} "
                        "failed flows); ratios use repetitions with a "
                        "usable ratio"
                    )
                print(message)
                log_file.write(message + "\n")
                log_file.flush()
            measurements.append(result)
            writer.writerow(result)
            csv_file.flush()

    return {
        "active_fractions": [count / len(leaves) for count in active_counts],
        "matrix_repetitions": matrix_repetitions,
        "traffic_scope": traffic_scope,
        "leaf_count": len(leaves),
        "log_file": log_path.name,
        "csv_file": csv_path.name,
        "measurements": measurements,
    }
