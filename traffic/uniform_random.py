import math
import random

import fnss


def build_traffic(topology, flow_count=8, demand_mbps=1.0, seed=1):
    """Build a reproducible FNSS matrix of unique, uniformly sampled host pairs."""
    if (
        not isinstance(flow_count, int)
        or isinstance(flow_count, bool)
        or flow_count < 1
    ):
        raise ValueError("flow_count must be a positive integer")
    if (
        not isinstance(demand_mbps, (int, float))
        or not math.isfinite(demand_mbps)
        or demand_mbps <= 0
    ):
        raise ValueError("demand_mbps must be a positive finite number")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("seed must be an integer")

    hosts = sorted(
        node for node, attributes in topology.nodes(data=True)
        if attributes.get("type") == "host"
    )
    possible_pairs = [
        (source, destination)
        for source in hosts
        for destination in hosts
        if source != destination
    ]
    if flow_count > len(possible_pairs):
        raise ValueError(
            f"flow_count ({flow_count}) exceeds the {len(possible_pairs)} "
            "available directed host pairs"
        )

    generator = random.Random(seed)
    flows = {}
    for source, destination in generator.sample(possible_pairs, flow_count):
        flows.setdefault(source, {})[destination] = float(demand_mbps)
    return fnss.TrafficMatrix(volume_unit="Mbps", flows=flows)
