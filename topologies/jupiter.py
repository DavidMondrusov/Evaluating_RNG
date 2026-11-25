""" see Jupiter Evolving, SIGCOMM 2022

Aggregation blocks have middle-block switches; each ToR connects to every
middle block in its block. Middle block m of each block links to middle
block m of every other block (a uniform mesh, no spine). It should use the ECMP shortest path
routing option (but this doesn't simulate TE on the real network, ##TODO##)
"""

import fnss


DEFAULT_PARAMETERS = {
    "blocks": 4,
    "middle_blocks": 4,
    "tors_per_block": 2,
    "hosts_per_tor": 4,
}


def validate_parameters(blocks, middle_blocks, tors_per_block, hosts_per_tor):
    for name, value, minimum in (
        ("blocks", blocks, 2),
        ("middle_blocks", middle_blocks, 1),
        ("tors_per_block", tors_per_block, 1),
        ("hosts_per_tor", hosts_per_tor, 1),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")


def build_topology(
    blocks=DEFAULT_PARAMETERS["blocks"],
    middle_blocks=DEFAULT_PARAMETERS["middle_blocks"],
    tors_per_block=DEFAULT_PARAMETERS["tors_per_block"],
    hosts_per_tor=DEFAULT_PARAMETERS["hosts_per_tor"],
):
    validate_parameters(blocks, middle_blocks, tors_per_block, hosts_per_tor)

    topology = fnss.DatacenterTopology()
    next_label = 0

    def add_node(**attributes):
        nonlocal next_label
        topology.add_node(next_label, **attributes)
        next_label += 1
        return next_label - 1

    middle = {}
    for block in range(blocks):
        for index in range(middle_blocks):
            middle[block, index] = add_node(type="switch", layer="middle_block", block=block)
        for _ in range(tors_per_block):
            tor = add_node(type="switch", layer="tor", block=block)
            for index in range(middle_blocks):
                topology.add_edge(tor, middle[block, index], type="tor_middle_block")
            for _ in range(hosts_per_tor):
                host = add_node(type="host", block=block)
                topology.add_edge(host, tor, type="host_tor")

    for first in range(blocks):
        for second in range(first + 1, blocks):
            for index in range(middle_blocks):
                topology.add_edge(middle[first, index], middle[second, index], type="dcni")

    fnss.set_capacities_constant(topology, 10, "Mbps")
    fnss.set_delays_constant(topology, 1, "ms")
    fnss.set_buffer_sizes_constant(topology, 50, "packets")
    return topology
