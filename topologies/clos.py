
import math

import fnss


def validate_parameters(parameters):
    k = parameters["k"]
    if not isinstance(k, int) or isinstance(k, bool) or k < 2 or k % 2:
        raise ValueError("k must be an even integer greater than or equal to 2")
    for name in (
        "host_link_mbps",
        "edge_uplink_mbps",
        "aggregation_uplink_mbps",
    ):
        capacity = parameters[name]
        if (
            not isinstance(capacity, (int, float))
            or isinstance(capacity, bool)
            or not math.isfinite(capacity)
            or capacity <= 0
        ):
            raise ValueError(f"{name} must be a positive finite number")


def build_topology(
    k=4,
    host_link_mbps=10,
    edge_uplink_mbps=10,
    aggregation_uplink_mbps=10,
):
    """Build a configured FNSS fat-tree topology."""
    validate_parameters(
        {
            "k": k,
            "host_link_mbps": host_link_mbps,
            "edge_uplink_mbps": edge_uplink_mbps,
            "aggregation_uplink_mbps": aggregation_uplink_mbps,
        }
    )

    capacities = {
        "edge_leaf": host_link_mbps,
        "aggregation_edge": edge_uplink_mbps,
        "core_aggregation": aggregation_uplink_mbps,
    }

    topology = fnss.fat_tree_topology(k=k)
    fnss.set_capacities_constant(topology, host_link_mbps, "Mbps")
    for _, _, attributes in topology.edges(data=True):
        attributes["capacity"] = capacities[attributes["type"]]
    fnss.set_delays_constant(topology, 1, "ms")
    fnss.set_buffer_sizes_constant(topology, 50, "packets")
    topology.graph["hosts_per_leaf"] = k // 2
    topology.graph["host_mode"] = "server"
    return topology