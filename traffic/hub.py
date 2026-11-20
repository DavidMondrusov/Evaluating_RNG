import random

from ._support import hosts, matrix, validate


def build_traffic(topology, demand_mbps=1.0, seed=1):
    """One flow from every non-hub host to a seeded hub host."""
    validate(demand_mbps, seed)
    nodes = hosts(topology)
    hub = random.Random(seed).choice(nodes)
    flows = {
        source: {hub: float(demand_mbps)}
        for source in nodes
        if source != hub
    }
    return matrix(flows)
