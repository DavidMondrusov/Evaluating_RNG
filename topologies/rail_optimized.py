"""FNSS builder for a two-tier rail-optimized GPU fabric.

Layout (NVIDIA DGX SuperPOD-style):

* The cluster is split into ``scalable_units``. Each unit holds
  ``servers_per_unit`` servers with ``gpus_per_server`` GPUs each, and every
  GPU has its own NIC. Each NIC is a host in the topology.
* GPU ``r`` of every server belongs to rail ``r``. Each scalable unit has one
  leaf switch per rail, so all same-rail GPUs in a unit are one hop apart.
* Every leaf connects to every one of the ``spine_count`` spine switches.
  Traffic between rails or units goes through a spine.

The intra-server scale-up domain (NVLink/NVSwitch) is not modeled, so
cross-rail traffic inside a server goes through the network fabric.

Routing is the fabric's own: ECMP over all shortest paths, the way leaf/spine
rail fabrics are run in practice (e.g. BGP with multipath). It is installed
as static OpenFlow 1.3 rules by ``install_routing``; a leaf hashes non-local
traffic across every spine with a ``select`` group, and a spine forwards to
the destination's leaf.
"""

import shlex
from collections import deque

import fnss


DEFAULT_PARAMETERS = {
    "scalable_units": 2,
    "servers_per_unit": 4,
    "gpus_per_server": 4,
    "spine_count": 4,
}


def validate_parameters(scalable_units, servers_per_unit, gpus_per_server, spine_count):
    """Raise ValueError unless every parameter is a positive integer."""
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
    """Build a configured FNSS rail-optimized leaf/spine topology."""
    validate_parameters(scalable_units, servers_per_unit, gpus_per_server, spine_count)

    topology = fnss.DatacenterTopology()
    topology.name = "rail_optimized_topology"
    topology.graph["type"] = "rail_optimized"
    topology.graph["scalable_units"] = scalable_units
    topology.graph["servers_per_unit"] = servers_per_unit
    topology.graph["gpus_per_server"] = gpus_per_server
    topology.graph["spine_count"] = spine_count

    # Integer labels keep the node sort order in fnss.to_mininet well defined.
    next_label = 0

    def add_node(**attributes):
        nonlocal next_label
        label = next_label
        next_label += 1
        topology.add_node(label, **attributes)
        return label

    spines = [
        add_node(type="switch", layer="spine", spine=index)
        for index in range(spine_count)
    ]
    for unit in range(scalable_units):
        for rail in range(gpus_per_server):
            leaf = add_node(type="switch", layer="leaf", unit=unit, rail=rail)
            for spine in spines:
                topology.add_edge(leaf, spine, type="leaf_spine")
            for server in range(servers_per_unit):
                host = add_node(
                    type="host",
                    layer="host",
                    unit=unit,
                    server=server,
                    rail=rail,
                )
                topology.add_edge(host, leaf, type="host_leaf")

    fnss.set_capacities_constant(topology, 10, "Mbps")
    fnss.set_delays_constant(topology, 1, "ms")
    fnss.set_buffer_sizes_constant(topology, 50, "packets")
    return topology


def _mininet_names(topology):
    """FNSS node -> Mininet name, as assigned by fnss.to_mininet(relabel_nodes=True)."""
    names = {}
    for kind, prefix in (("host", "h"), ("switch", "s")):
        nodes = sorted(
            node for node, attributes in topology.nodes(data=True)
            if attributes.get("type") == kind
        )
        names.update(
            (node, f"{prefix}{index}") for index, node in enumerate(nodes, start=1)
        )
    return names


def _switch_distances(topology, switches, target):
    """Hop counts from every switch to ``target`` over switch-to-switch links."""
    distances = {target: 0}
    queue = deque([target])
    while queue:
        node = queue.popleft()
        for neighbor in topology.neighbors(node):
            if neighbor in switches and neighbor not in distances:
                distances[neighbor] = distances[node] + 1
                queue.append(neighbor)
    return distances


def _ofctl(switch, arguments):
    output = switch.cmd(f"ovs-ofctl -O OpenFlow13 {arguments}; echo __rc=$?")
    body, _, status = output.rpartition("__rc=")
    if status.strip() != "0":
        raise RuntimeError(f"ovs-ofctl {arguments} failed on {switch.name}: {body.strip()}")


def install_routing(net, topology):
    """Install static ECMP shortest-path forwarding on a started Mininet network.

    Every switch forwards each destination host's /32 to all neighbors that
    are one hop closer to the host's leaf, through an OpenFlow ``select``
    group when there is more than one. Hosts get static ARP entries because
    switches drop everything that does not match a route (ARP flooding would
    loop on a multi-spine fabric).
    """
    names = _mininet_names(topology)
    switches = {
        node for node, attributes in topology.nodes(data=True)
        if attributes.get("type") == "switch"
    }
    hosts = [
        node for node, attributes in topology.nodes(data=True)
        if attributes.get("type") == "host"
    ]

    # OpenFlow port on Mininet node ``a`` that leads to Mininet node ``b``.
    ports = {}
    for link in net.links:
        first, second = link.intf1, link.intf2
        ports[(first.node.name, second.node.name)] = first.node.ports[first]
        ports[(second.node.name, first.node.name)] = second.node.ports[second]

    for switch_node in sorted(switches):
        switch = net.get(names[switch_node])
        bridge = shlex.quote(switch.name)
        switch.cmd(
            f"ovs-vsctl set bridge {bridge} protocols=OpenFlow13 stp_enable=false "
            f"-- set-fail-mode {bridge} secure -- del-controller {bridge}"
        )
        _ofctl(switch, f"del-flows {bridge}")
        _ofctl(switch, f"del-groups {bridge}")

    groups = {}  # Mininet switch name -> {output port tuple: group id}
    for host_node in hosts:
        host = net.get(names[host_node])
        (leaf,) = [node for node in topology.neighbors(host_node) if node in switches]
        distances = _switch_distances(topology, switches, leaf)
        for switch_node in switches:
            switch = net.get(names[switch_node])
            if switch_node == leaf:
                out_ports = [ports[(switch.name, host.name)]]
            elif switch_node not in distances:
                continue
            else:
                out_ports = sorted(
                    ports[(switch.name, names[neighbor])]
                    for neighbor in topology.neighbors(switch_node)
                    if distances.get(neighbor) == distances[switch_node] - 1
                )
            switch_groups = groups.setdefault(switch.name, {})
            if len(out_ports) == 1:
                action = f"output:{out_ports[0]}"
            else:
                key = tuple(out_ports)
                if key not in switch_groups:
                    group_id = len(switch_groups) + 1
                    switch_groups[key] = group_id
                    buckets = ",".join(f"bucket=actions=output:{port}" for port in key)
                    _ofctl(
                        switch,
                        f"add-group {shlex.quote(switch.name)} "
                        f"group_id={group_id},type=select,{buckets}",
                    )
                action = f"group:{switch_groups[key]}"
            _ofctl(
                switch,
                f"add-flow {shlex.quote(switch.name)} "
                f"priority=100,ip,nw_dst={host.IP()}/32,actions={action}",
            )

    net.staticArp()
