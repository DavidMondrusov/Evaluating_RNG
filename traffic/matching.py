import random

from ._support import hosts, matrix, validate


def build_traffic(topology, demand_mbps=1.0, seed=1):
    """Pair every host with one partner and send one flow each way."""
    validate(demand_mbps, seed)
    nodes = hosts(topology)
    random.Random(seed).shuffle(nodes)
    flows = {}
    for source, destination in zip(nodes[::2], nodes[1::2]):
        flows.setdefault(source, {})[destination] = float(demand_mbps)
        flows.setdefault(destination, {})[source] = float(demand_mbps)
    return matrix(flows)
