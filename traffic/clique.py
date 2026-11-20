import random

from ._support import hosts, matrix, validate


def build_traffic(topology, demand_mbps=1.0, seed=1):
    """One flow across every host pair, direction chosen by the seed."""
    validate(demand_mbps, seed)
    nodes = hosts(topology)
    generator = random.Random(seed)
    flows = {}
    for index, first in enumerate(nodes):
        for second in nodes[index + 1:]:
            source, destination = (
                (second, first) if generator.random() < 0.5 else (first, second)
            )
            flows.setdefault(source, {})[destination] = float(demand_mbps)
    return matrix(flows)
