"""RNG (flat quasi-random datacenter) topology, logical switch layer only.

Based on "RNG: Flat Datacenter Networks at Scale" (Bernardi et al.). RNG
connects ``n`` routers (ToRs) directly to each other: each router has ``d``
fabric uplinks (breakout lanes), and the wiring is (quasi-)random, which
makes the switch graph an expander. Servers hang off every router.

In production the random wiring is produced physically by passive optical
ShuffleBoxes. This v1 builder skips that layer and emits the logical graph
the ShuffleBoxes would produce: a uniformly random, connected, simple
``d``-regular graph between the ``n`` switches. The paper's analysis treats
RNG graphs as truly random for the same reason (§6-7).

Differences from production RNG that matter when reading results:

* No parallel links or self-loops. ShuffleBoxes can (rarely) bundle two
  lanes of the same router pair, or bridge a router to itself; FNSS and
  ``fnss.to_mininet`` work on simple graphs, so we resample instead.
* No routers with mixed degrees (the paper supports them but omits details).

Node ids are integers: switches ``0..n-1`` then hosts. ``fnss.to_mininet``
relabels sorted nodes, so switch ``i`` becomes ``s{i+1}`` and the ``k``-th
host of switch ``i`` becomes ``h{i*hosts_per_switch+k+1}``.
"""

import math
import random

import fnss
import networkx


DEFAULT_PARAMETERS = {
    "n": 8,
    "d": 3,
    "hosts_per_switch": 1,
    "host_link_mbps": 10,
    "fabric_link_mbps": 10,
    "seed": 1,
}

MAX_ATTEMPTS = 100


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def validate_parameters(
    n,
    d,
    hosts_per_switch,
    host_link_mbps,
    fabric_link_mbps,
    seed,
):
    """Raise ValueError naming the first invalid parameter."""
    if not _is_int(n) or n < 2:
        raise ValueError("n must be an integer >= 2")
    if not _is_int(d) or not 1 <= d < n:
        raise ValueError("d must be an integer with 1 <= d < n")
    if (n * d) % 2:
        raise ValueError("n * d must be even for a d-regular graph")
    if not _is_int(hosts_per_switch) or hosts_per_switch < 1:
        raise ValueError("hosts_per_switch must be a positive integer")
    for name, capacity in (
        ("host_link_mbps", host_link_mbps),
        ("fabric_link_mbps", fabric_link_mbps),
    ):
        if (
            not isinstance(capacity, (int, float))
            or isinstance(capacity, bool)
            or not math.isfinite(capacity)
            or capacity <= 0
        ):
            raise ValueError(f"{name} must be a positive finite number")
    if not _is_int(seed):
        raise ValueError("seed must be an integer")


def random_switch_graph(n, d, seed):
    """Connected simple random d-regular graph on switches 0..n-1.

    Resamples (deterministically from ``seed``) until the graph is connected;
    for d >= 3 the first sample almost always is.
    """
    generator = random.Random(seed)
    for _ in range(MAX_ATTEMPTS):
        graph = networkx.random_regular_graph(
            d, n, seed=generator.randrange(2**32)
        )
        if networkx.is_connected(graph):
            return graph
    raise ValueError(
        f"no connected {d}-regular graph on {n} switches after "
        f"{MAX_ATTEMPTS} attempts; increase d"
    )


def build_topology(
    n=DEFAULT_PARAMETERS["n"],
    d=DEFAULT_PARAMETERS["d"],
    hosts_per_switch=DEFAULT_PARAMETERS["hosts_per_switch"],
    host_link_mbps=DEFAULT_PARAMETERS["host_link_mbps"],
    fabric_link_mbps=DEFAULT_PARAMETERS["fabric_link_mbps"],
    seed=DEFAULT_PARAMETERS["seed"],
):
    """Build a configured FNSS topology of an RNG fabric with its hosts."""
    validate_parameters(
        n, d, hosts_per_switch, host_link_mbps, fabric_link_mbps, seed
    )
    switch_graph = random_switch_graph(n, d, seed)

    topology = fnss.Topology(type="rng", n=n, d=d, seed=seed)
    for switch in range(n):
        topology.add_node(switch, type="switch", layer="rng")
    for u, v in sorted(tuple(sorted(edge)) for edge in switch_graph.edges()):
        topology.add_edge(u, v, type="fabric", capacity=fabric_link_mbps)
    host = n
    for switch in range(n):
        for _ in range(hosts_per_switch):
            topology.add_node(host, type="host", layer="leaf")
            topology.add_edge(
                switch, host, type="host_link", capacity=host_link_mbps
            )
            host += 1

    topology.graph["capacity_unit"] = "Mbps"
    fnss.set_delays_constant(topology, 1, "ms")
    fnss.set_buffer_sizes_constant(topology, 50, "packets")
    return topology
