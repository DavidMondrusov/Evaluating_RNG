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


def leaf_hosts(topology):
    leaves = set()
    hosts_by_leaf = {}
    for source, target, _ in topology.edges(data=True):
        source_host = topology.nodes[source].get("type") == "host"
        target_host = topology.nodes[target].get("type") == "host"
        if source_host == target_host:
            continue
        leaf, host = (target, source) if source_host else (source, target)
        leaves.add(leaf)
        hosts_by_leaf.setdefault(leaf, []).append(host)
    leaves = sorted(leaves)
    if len(leaves) < 2 or any(not nodes for nodes in hosts_by_leaf.values()):
        raise ValueError("Leaf traffic requires at least two leaves with hosts")
    return leaves, hosts_by_leaf


def host_capacity(topology):
    capacities = {}
    for source, target, attributes in topology.edges(data=True):
        if (topology.nodes[source].get("type") == "host") == (
            topology.nodes[target].get("type") == "host"
        ):
            continue
        host = source if topology.nodes[source].get("type") == "host" else target
        capacities[host] = float(attributes["capacity"])
    if not capacities:
        raise ValueError("Topology has no host links")
    return capacities


def leaf_subgraph(topology, leaves):
    subgraph = topology.subgraph(leaves).copy()
    for leaf in leaves:
        subgraph.nodes[leaf]["type"] = "host"
        subgraph.nodes[leaf]["layer"] = "leaf"
    return subgraph


def leaf_multiplier(topology, leaf, hosts_by_leaf):
    if topology.graph.get("host_mode") == "stub":
        return int(topology.graph["hosts_per_leaf"])
    return len(hosts_by_leaf[leaf])


def expand_leaf_flows(matrix, topology, hosts_by_leaf, capacities):
    """Expand leaf-pair flows into host flows at each source leaf's full rate."""
    leaf_pairs = list(matrix.flows())
    out_degree = {}
    for source_leaf, _ in leaf_pairs:
        out_degree[source_leaf] = out_degree.get(source_leaf, 0) + 1
    leaf_capacity = {
        leaf: sum(capacities[host] for host in nodes)
        for leaf, nodes in hosts_by_leaf.items()
    }
    leaf_order = sorted(hosts_by_leaf)
    flows = {}
    flow_id = 1
    for source_leaf, destination_leaf in leaf_pairs:
        source_hosts = sorted(hosts_by_leaf[source_leaf])
        destination_hosts = sorted(hosts_by_leaf[destination_leaf])
        multiplier = leaf_multiplier(topology, source_leaf, hosts_by_leaf)
        demand = (
            leaf_capacity[source_leaf] / out_degree[source_leaf] / multiplier
        )
        offset = leaf_order.index(source_leaf)
        for index in range(multiplier):
            flows[flow_id] = (
                source_hosts[index % len(source_hosts)],
                destination_hosts[
                    (index + offset) % len(destination_hosts)
                ],
                demand,
            )
            flow_id += 1
    return flows


def host_flows(matrix, capacities):
    out_degree = {}
    for source, _ in matrix.flows():
        out_degree[source] = out_degree.get(source, 0) + 1
    return {
        flow_id: (source, destination, capacities[source] / out_degree[source])
        for flow_id, ((source, destination), _) in enumerate(
            matrix.flows().items(), start=1
        )
    }
