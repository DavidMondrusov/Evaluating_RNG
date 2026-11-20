import csv
from pathlib import Path

from . import iperf


def _host_capacity(topology):
    capacities = {}
    for source, target, attributes in topology.edges(data=True):
        if attributes.get("type") != "edge_leaf":
            continue
        host = source if topology.nodes[source].get("type") == "host" else target
        capacities[host] = float(attributes["capacity"])
    if not capacities:
        raise ValueError("Topology has no host links to size the load sweep")
    return capacities


def _mean(values):
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def _summary(fraction, flows, measured):
    offered = sum(demand for _, _, demand in flows.values())
    sent = sum(measured[flow_id][1] for flow_id in flows)
    delivered = sum(measured[flow_id][0] for flow_id in flows)
    return {
        "load_fraction": fraction,
        "flow_count": len(flows),
        "offered_mbps": offered,
        "sent_mbps": sent,
        "delivered_mbps": delivered,
        "oversubscription_ratio": sent / delivered if delivered else None,
        "packet_loss_percent": 100 * (1 - delivered / sent) if sent else None,
        "mean_idle_rtt_ms": _mean(
            [measured[flow_id][2]["avg_rtt_ms"] for flow_id in flows]
        ),
        "mean_loaded_rtt_ms": _mean(
            [measured[flow_id][3]["avg_rtt_ms"] for flow_id in flows]
        ),
    }


def run(
    net,
    traffic_matrix,
    host_names,
    topology,
    duration_seconds,
    load_fractions,
    output_dir,
):
    """Sweep offered load from idle to full host-link rate and report ratios."""
    output_dir = Path(output_dir)
    capacities = _host_capacity(topology)
    pairs = {
        flow_id: (source, destination)
        for flow_id, (source, destination) in enumerate(
            traffic_matrix.flows().keys(), start=1
        )
    }
    if not pairs:
        raise ValueError("Load sweep requires at least one traffic flow")
    out_degree = {}
    for source, destination in pairs.values():
        if source not in host_names or destination not in host_names:
            raise ValueError(
                f"Traffic flow {source!r} -> {destination!r} references "
                "a node without a Mininet host mapping"
            )
        if source not in capacities:
            raise ValueError(f"Host {source!r} has no host link capacity")
        out_degree[source] = out_degree.get(source, 0) + 1

    fractions = sorted({float(fraction) for fraction in load_fractions})
    log_path = output_dir / "load-sweep.log"
    csv_path = output_dir / "load-sweep.csv"
    measurements = []
    idle_latency = None
    with log_path.open("w", encoding="utf-8") as log_file:
        for fraction in fractions:
            if idle_latency is None:
                idle_latency = iperf.measure_idle(net, pairs, host_names)
            if fraction == 0:
                measurements.append(
                    {
                        "load_fraction": 0.0,
                        "flow_count": len(pairs),
                        "offered_mbps": 0.0,
                        "sent_mbps": 0.0,
                        "delivered_mbps": 0.0,
                        "oversubscription_ratio": None,
                        "packet_loss_percent": None,
                        "mean_idle_rtt_ms": _mean(
                            [
                                latency["avg_rtt_ms"]
                                for latency in idle_latency.values()
                            ]
                        ),
                        "mean_loaded_rtt_ms": None,
                    }
                )
                continue
            flows = {
                flow_id: (
                    source,
                    destination,
                    fraction
                    * capacities[source]
                    / out_degree[source],
                )
                for flow_id, (source, destination) in pairs.items()
            }
            print(
                f"*** Load sweep: {fraction:.0%} of host-link rate "
                f"({len(flows)} flows)"
            )
            measured = iperf.run_flows_concurrently(
                net, flows, host_names, duration_seconds, log_file, idle_latency
            )
            measurements.append(_summary(fraction, flows, measured))

    columns = (
        "load_fraction",
        "flow_count",
        "offered_mbps",
        "sent_mbps",
        "delivered_mbps",
        "oversubscription_ratio",
        "packet_loss_percent",
        "mean_idle_rtt_ms",
        "mean_loaded_rtt_ms",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(measurements)

    return {
        "flow_count": len(pairs),
        "load_fractions": fractions,
        "log_file": log_path.name,
        "csv_file": csv_path.name,
        "measurements": measurements,
    }
