
import math

import fnss


def build_topology(
    k=4,
    host_link_mbps=10,
    edge_uplink_mbps=10,
    aggregation_uplink_mbps=10,
):
    """Build a configured FNSS fat-tree topology."""
    if not isinstance(k, int) or isinstance(k, bool) or k < 2 or k % 2:
        raise ValueError("k must be an even integer greater than or equal to 2")

    capacities = {
        "edge_leaf": host_link_mbps,
        "aggregation_edge": edge_uplink_mbps,
        "core_aggregation": aggregation_uplink_mbps,
    }
    for link_type, capacity in capacities.items():
        if (
            not isinstance(capacity, (int, float))
            or isinstance(capacity, bool)
            or not math.isfinite(capacity)
            or capacity <= 0
        ):
            raise ValueError(f"{link_type} capacity must be a positive finite number")

    topology = fnss.fat_tree_topology(k=k)
    fnss.set_capacities_constant(topology, host_link_mbps, "Mbps")
    for _, _, attributes in topology.edges(data=True):
        attributes["capacity"] = capacities[attributes["type"]]
    fnss.set_delays_constant(topology, 1, "ms")
    fnss.set_buffer_sizes_constant(topology, 50, "packets")
    return topology