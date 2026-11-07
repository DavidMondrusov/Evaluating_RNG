
import fnss


def build_topology(k=4):
    """Build a configured FNSS fat-tree topology."""
    if not isinstance(k, int) or isinstance(k, bool) or k < 2 or k % 2:
        raise ValueError("k must be an even integer greater than or equal to 2")

    topology = fnss.fat_tree_topology(k=k)
    fnss.set_capacities_constant(topology, 10, "Mbps")
    fnss.set_delays_constant(topology, 1, "ms")
    fnss.set_buffer_sizes_constant(topology, 50, "packets")
    return topology