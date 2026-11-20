import math

import fnss


def hosts(topology):
    nodes = sorted(
        node for node, attributes in topology.nodes(data=True)
        if attributes.get("type") == "host"
    )
    if len(nodes) < 2:
        raise ValueError("Traffic patterns require at least two hosts")
    return nodes


def validate(demand_mbps, seed):
    if (
        not isinstance(demand_mbps, (int, float))
        or isinstance(demand_mbps, bool)
        or not math.isfinite(demand_mbps)
        or demand_mbps <= 0
    ):
        raise ValueError("demand_mbps must be a positive finite number")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("seed must be an integer")


def matrix(flows):
    return fnss.TrafficMatrix(volume_unit="Mbps", flows=flows)
