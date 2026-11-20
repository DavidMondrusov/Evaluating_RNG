"""Static ECMP over shortest paths: each switch hashes flows across all
neighbors one hop closer to the destination host's switch."""

import os
import tempfile

import networkx
from mininet.node import OVSSwitch


DEFAULT_PARAMETERS = {}


def controller_class():
    return None


def configure(net, parameters):
    graph = networkx.Graph()
    ports = {}  # (node, neighbor) -> OpenFlow port on node
    for link in net.links:
        a, b = link.intf1, link.intf2
        graph.add_edge(a.node.name, b.node.name)
        ports[a.node.name, b.node.name] = a.node.ports[a]
        ports[b.node.name, a.node.name] = b.node.ports[b]
    switches = [s for s in net.switches if isinstance(s, OVSSwitch)]
    fabric = graph.subgraph(s.name for s in switches)

    flows = {s.name: [] for s in switches}
    groups = {s.name: {} for s in switches}  # port tuple -> group id
    for host in net.hosts:
        (edge,) = [n for n in graph.neighbors(host.name) if n in fabric]
        distance = networkx.single_source_shortest_path_length(fabric, edge)
        for switch in fabric:
            if switch == edge:
                out = [ports[edge, host.name]]
            else:
                out = sorted(
                    ports[switch, n] for n in fabric.neighbors(switch)
                    if distance.get(n) == distance[switch] - 1
                )
            if len(out) == 1:
                action = f"output:{out[0]}"
            else:
                group_id = groups[switch].setdefault(tuple(out), len(groups[switch]) + 1)
                action = f"group:{group_id}"
            flows[switch].append(f"priority=100,ip,nw_dst={host.IP()},actions={action}")

    for switch in switches:
        switch.cmd(f"ovs-vsctl set bridge {switch.name} protocols=OpenFlow13")
        for out, group_id in groups[switch.name].items():
            buckets = ",".join(f"bucket=actions=output:{p}" for p in out)
            switch.cmd(f"ovs-ofctl -O OpenFlow13 add-group {switch.name} "
                       f"group_id={group_id},type=select,{buckets}")
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as flow_file:
            flow_file.write("\n".join(flows[switch.name]) + "\n")
        switch.cmd(f"ovs-ofctl -O OpenFlow13 add-flows {switch.name} {path}")
        os.unlink(path)
    net.staticArp()
    return {"deployed": True, "flow_count": sum(len(f) for f in flows.values())}
