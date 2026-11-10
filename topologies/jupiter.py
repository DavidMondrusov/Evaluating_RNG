"""FNSS builder for a Jupiter-style direct-connect fabric.

Modeled on Google's Jupiter after its move from Clos to direct connect
(Poutievski et al., "Jupiter Evolving", SIGCOMM 2022):

* The fabric is ``blocks`` aggregation blocks with no spine layer. Each block
  has ``middle_blocks`` middle-block switches, and ``tors_per_block`` ToRs
  that each connect to every middle block of their block. Each ToR serves
  ``hosts_per_tor`` hosts.
* Blocks are joined by a uniform mesh through the optical DCNI layer, which is
  modeled as plain links: middle block ``m`` of every block links to middle
  block ``m`` of every other block. That gives each block pair the same
  number of direct links, and splits the inter-block links into
  ``middle_blocks`` disjoint "colors" (the paper uses four).

Routing follows the paper's demand-oblivious scheme. Inter-block traffic
takes the direct path to the destination block or a single-transit path
through one other block. Two VRFs keep transit loop-free: packets that
arrive on a DCNI-facing port and are not for a local host may only take the
direct link to their destination block. Source traffic is split by path
capacity, so with a uniform mesh every direct and single-transit path gets
the same share. ``transit_weight`` scales the share of each transit path
(0 means direct paths only). Traffic and topology engineering, which adapt
the split and the mesh to measured demand, are not modeled.
"""

import os
import shlex
import tempfile

import fnss


DEFAULT_PARAMETERS = {
    "blocks": 4,
    "middle_blocks": 4,
    "tors_per_block": 2,
    "hosts_per_tor": 4,
    "transit_weight": 1.0,
}

# OVS select-group bucket weights are integers; this sets the resolution.
_WEIGHT_SCALE = 100


def validate_parameters(blocks, middle_blocks, tors_per_block, hosts_per_tor, transit_weight):
    """Raise ValueError if a parameter is out of range."""
    for name, value, minimum in (
        ("blocks", blocks, 2),
        ("middle_blocks", middle_blocks, 1),
        ("tors_per_block", tors_per_block, 1),
        ("hosts_per_tor", hosts_per_tor, 1),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    if (
        not isinstance(transit_weight, (int, float))
        or isinstance(transit_weight, bool)
        or not 0 <= transit_weight <= 100
    ):
        raise ValueError("transit_weight must be a number between 0 and 100")


def build_topology(
    blocks=DEFAULT_PARAMETERS["blocks"],
    middle_blocks=DEFAULT_PARAMETERS["middle_blocks"],
    tors_per_block=DEFAULT_PARAMETERS["tors_per_block"],
    hosts_per_tor=DEFAULT_PARAMETERS["hosts_per_tor"],
    transit_weight=DEFAULT_PARAMETERS["transit_weight"],
):
    """Build a configured FNSS Jupiter-style direct-connect topology."""
    validate_parameters(blocks, middle_blocks, tors_per_block, hosts_per_tor, transit_weight)

    topology = fnss.DatacenterTopology()
    topology.name = "jupiter_direct_connect_topology"
    topology.graph["type"] = "jupiter_direct_connect"
    topology.graph.update(
        blocks=blocks,
        middle_blocks=middle_blocks,
        tors_per_block=tors_per_block,
        hosts_per_tor=hosts_per_tor,
        transit_weight=float(transit_weight),
    )

    # Integer labels keep the node sort order in fnss.to_mininet well defined.
    next_label = 0

    def add_node(**attributes):
        nonlocal next_label
        label = next_label
        next_label += 1
        topology.add_node(label, **attributes)
        return label

    middle = {}  # (block, middle block index) -> node
    for block in range(blocks):
        for index in range(middle_blocks):
            middle[block, index] = add_node(
                type="switch", layer="middle_block", block=block, middle_block=index
            )
        for tor_index in range(tors_per_block):
            tor = add_node(type="switch", layer="tor", block=block, tor=tor_index)
            for index in range(middle_blocks):
                topology.add_edge(tor, middle[block, index], type="tor_middle_block")
            for host_index in range(hosts_per_tor):
                host = add_node(
                    type="host", layer="host", block=block, tor=tor_index, host=host_index
                )
                topology.add_edge(host, tor, type="host_tor")

    for first in range(blocks):
        for second in range(first + 1, blocks):
            for index in range(middle_blocks):
                topology.add_edge(
                    middle[first, index], middle[second, index], type="dcni", color=index
                )

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


def _ofctl(switch, arguments):
    output = switch.cmd(f"ovs-ofctl -O OpenFlow13 {arguments}; echo __rc=$?")
    body, _, status = output.rpartition("__rc=")
    if status.strip() != "0":
        raise RuntimeError(f"ovs-ofctl {arguments} failed on {switch.name}: {body.strip()}")


def _add_flows(switch, flows):
    """Load many flows with one ovs-ofctl call, via a temporary flow file."""
    if not flows:
        return
    descriptor, path = tempfile.mkstemp(prefix=f"jupiter-{switch.name}-", suffix=".flows")
    try:
        with os.fdopen(descriptor, "w") as flow_file:
            flow_file.write("\n".join(flows) + "\n")
        _ofctl(switch, f"add-flows {shlex.quote(switch.name)} {shlex.quote(path)}")
    finally:
        os.unlink(path)


def install_routing(net, topology):
    """Install static direct/single-transit forwarding on a started Mininet network.

    ToRs deliver local hosts and hash everything else across their block's
    middle blocks. Each middle block uses three tables:

    * table 0: deliver hosts in its own block; send packets from DCNI ports
      to the transit VRF (table 2), and everything else to the source VRF
      (table 1).
    * table 1 (source VRF): weighted select group over the direct link to the
      destination block and the links to every other block (transit).
    * table 2 (transit VRF): the direct link to the destination block only.

    Hosts get static ARP entries because switches drop everything that does
    not match a route.
    """
    names = _mininet_names(topology)
    nodes = topology.nodes
    transit_weight = topology.graph.get("transit_weight", DEFAULT_PARAMETERS["transit_weight"])

    ports = {}  # (Mininet node, Mininet neighbor) -> OpenFlow port on the node
    for link in net.links:
        first, second = link.intf1, link.intf2
        ports[(first.node.name, second.node.name)] = first.node.ports[first]
        ports[(second.node.name, first.node.name)] = second.node.ports[second]

    def port(node, neighbor):
        return ports[(names[node], names[neighbor])]

    def neighbors(node, layer):
        return sorted(n for n in topology.neighbors(node) if nodes[n].get("layer") == layer)

    hosts = [n for n, a in nodes(data=True) if a.get("type") == "host"]
    host_ip = {n: net.get(names[n]).IP() for n in hosts}
    host_tor = {n: neighbors(n, "tor")[0] for n in hosts}
    switches = sorted(n for n, a in nodes(data=True) if a.get("type") == "switch")

    for node in switches:
        switch = net.get(names[node])
        bridge = shlex.quote(switch.name)
        switch.cmd(
            f"ovs-vsctl set bridge {bridge} protocols=OpenFlow13 stp_enable=false "
            f"-- set-fail-mode {bridge} secure -- del-controller {bridge}"
        )
        _ofctl(switch, f"del-flows {bridge}")
        _ofctl(switch, f"del-groups {bridge}")

    for node in switches:
        switch = net.get(names[node])
        bridge = shlex.quote(switch.name)
        attributes = nodes[node]
        flows = []

        if attributes["layer"] == "tor":
            uplinks = [port(node, m) for m in neighbors(node, "middle_block")]
            buckets = ",".join(f"bucket=actions=output:{p}" for p in uplinks)
            _ofctl(switch, f"add-group {bridge} group_id=1,type=select,{buckets}")
            for host in neighbors(node, "host"):
                flows.append(f"priority=300,ip,nw_dst={host_ip[host]},actions=output:{port(node, host)}")
            flows.append("priority=100,ip,actions=group:1")
            _add_flows(switch, flows)
            continue

        # Middle block.
        block = attributes["block"]
        remote = {}  # destination block -> port of the direct link to it
        for peer in neighbors(node, "middle_block"):
            remote[nodes[peer]["block"]] = port(node, peer)
        local_hosts = [h for h in hosts if nodes[h]["block"] == block]

        for host in local_hosts:
            flows.append(
                f"priority=300,ip,nw_dst={host_ip[host]},"
                f"actions=output:{port(node, host_tor[host])}"
            )
        for dcni_port in sorted(remote.values()):
            flows.append(f"priority=200,in_port={dcni_port},ip,actions=goto_table:2")
        flows.append("priority=100,ip,actions=goto_table:1")

        group_ids = {}
        for destination_block, direct_port in sorted(remote.items()):
            buckets = [f"bucket=weight:{_WEIGHT_SCALE},actions=output:{direct_port}"]
            transit = round(transit_weight * _WEIGHT_SCALE)
            if transit > 0:
                buckets += [
                    f"bucket=weight:{transit},actions=output:{p}"
                    for b, p in sorted(remote.items()) if b != destination_block
                ]
            group_id = len(group_ids) + 1
            group_ids[destination_block] = group_id
            _ofctl(switch, f"add-group {bridge} group_id={group_id},type=select," + ",".join(buckets))

        for host in hosts:
            destination_block = nodes[host]["block"]
            if destination_block == block:
                continue
            flows.append(
                f"table=1,priority=100,ip,nw_dst={host_ip[host]},"
                f"actions=group:{group_ids[destination_block]}"
            )
            flows.append(
                f"table=2,priority=100,ip,nw_dst={host_ip[host]},"
                f"actions=output:{remote[destination_block]}"
            )
        _add_flows(switch, flows)

    net.staticArp()
