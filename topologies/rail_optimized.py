"""FNSS builder for a two-tier rail-optimized fabric.

Each GPU NIC is a host. Each scalable unit has one leaf per rail, and every
leaf connects to every spine. Use the "ECMP shortest path" routing option.
"""

import fnss


DEFAULT_PARAMETERS = {
    "scalable_units": 2,
    "servers_per_unit": 4,
    "gpus_per_server": 4,
    "spine_count": 4,
}


def validate_parameters(scalable_units, servers_per_unit, gpus_per_server, spine_count):
    for name, value in (
        ("scalable_units", scalable_units),
        ("servers_per_unit", servers_per_unit),
        ("gpus_per_server", gpus_per_server),
        ("spine_count", spine_count),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{name} must be a positive integer")


def build_topology(
    scalable_units=DEFAULT_PARAMETERS["scalable_units"],
    servers_per_unit=DEFAULT_PARAMETERS["servers_per_unit"],
    gpus_per_server=DEFAULT_PARAMETERS["gpus_per_server"],
    spine_count=DEFAULT_PARAMETERS["spine_count"],
):
    validate_parameters(scalable_units, servers_per_unit, gpus_per_server, spine_count)

    topology = fnss.DatacenterTopology()
    next_label = 0

    def add_node(**attributes):
        nonlocal next_label
        topology.add_node(next_label, **attributes)
        next_label += 1
        return next_label - 1

    spines = [add_node(type="switch", layer="spine") for _ in range(spine_count)]
    for unit in range(scalable_units):
        for rail in range(gpus_per_server):
            leaf = add_node(type="switch", layer="leaf", unit=unit, rail=rail)
            for spine in spines:
                topology.add_edge(leaf, spine, type="leaf_spine")
            for server in range(servers_per_unit):
                host = add_node(type="host", unit=unit, server=server, rail=rail)
                topology.add_edge(host, leaf, type="host_leaf")

    fnss.set_capacities_constant(topology, 10, "Mbps")
    fnss.set_delays_constant(topology, 1, "ms")
    fnss.set_buffer_sizes_constant(topology, 50, "packets")
    return topology
