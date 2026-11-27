"""Two-tier leaf-spine Clos modeled on Azure's datacenter fabric.

Scaled-down but keeps the properties that matter for load experiments:
uniform link capacity, 1:1 leaf oversubscription by default, one ECMP path
per spine, deep switch buffers, and low per-link delay.
"""

import math

import fnss


DEFAULT_PARAMETERS = {
    "leaves": 4,
    "spines": 4,
    "hosts_per_leaf": 4,
    "host_link_mbps": 10,
    "leaf_oversubscription": 1.0,
    "delay_ms": 0.05,
    "buffer_packets": 1000,
    "host_mode": "server",
}


def validate_parameters(parameters):
    for name in ("leaves", "spines", "hosts_per_leaf"):
        value = parameters[name]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    for name in ("host_link_mbps", "leaf_oversubscription"):
        value = parameters[name]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be a positive finite number")
    delay = parameters["delay_ms"]
    if (
        not isinstance(delay, (int, float))
        or isinstance(delay, bool)
        or not math.isfinite(delay)
        or delay <= 0
    ):
        raise ValueError("delay_ms must be a positive finite number")
    buffer_packets = parameters["buffer_packets"]
    if (
        not isinstance(buffer_packets, int)
        or isinstance(buffer_packets, bool)
        or buffer_packets < 1
    ):
        raise ValueError("buffer_packets must be a positive integer")
    if parameters["host_mode"] not in ("server", "stub"):
        raise ValueError("host_mode must be 'server' or 'stub'")


def build_topology(
    leaves=4,
    spines=4,
    hosts_per_leaf=4,
    host_link_mbps=10,
    leaf_oversubscription=1.0,
    delay_ms=0.05,
    buffer_packets=1000,
    host_mode="server",
):
    """Build an FNSS leaf-spine fabric with ECMP paths between every leaf."""
    validate_parameters(
        {
            "leaves": leaves,
            "spines": spines,
            "hosts_per_leaf": hosts_per_leaf,
            "host_link_mbps": host_link_mbps,
            "leaf_oversubscription": leaf_oversubscription,
            "delay_ms": delay_ms,
            "buffer_packets": buffer_packets,
            "host_mode": host_mode,
        }
    )

    topology = fnss.Topology()
    for index in range(1, leaves + 1):
        topology.add_node(f"leaf{index}", type="switch", layer="edge")
    for index in range(1, spines + 1):
        topology.add_node(f"spine{index}", type="switch", layer="core")

    if host_mode == "server":
        host_index = 0
        for leaf in range(1, leaves + 1):
            for _ in range(hosts_per_leaf):
                host_index += 1
                host = f"host{host_index}"
                topology.add_node(host, type="host", layer="leaf")
                topology.add_edge(f"leaf{leaf}", host, type="edge_leaf")
    else:
        for leaf in range(1, leaves + 1):
            host = f"host{leaf}"
            topology.add_node(host, type="host", layer="leaf")
            topology.add_edge(f"leaf{leaf}", host, type="edge_leaf")

    for leaf in range(1, leaves + 1):
        for spine in range(1, spines + 1):
            topology.add_edge(
                f"leaf{leaf}",
                f"spine{spine}",
                type="aggregation_edge",
            )

    fnss.set_capacities_constant(topology, host_link_mbps, "Mbps")
    if host_mode == "stub":
        for _, _, attributes in topology.edges(data=True):
            if attributes["type"] == "edge_leaf":
                attributes["capacity"] = hosts_per_leaf * host_link_mbps
    spine_link_mbps = (
        hosts_per_leaf * host_link_mbps
        / (leaf_oversubscription * spines)
    )
    for _, _, attributes in topology.edges(data=True):
        if attributes["type"] == "aggregation_edge":
            attributes["capacity"] = spine_link_mbps
    fnss.set_delays_constant(topology, delay_ms, "ms")
    fnss.set_buffer_sizes_constant(topology, buffer_packets, "packets")
    topology.graph["hosts_per_leaf"] = hosts_per_leaf
    topology.graph["host_link_mbps"] = float(host_link_mbps)
    topology.graph["host_mode"] = host_mode
    return topology
