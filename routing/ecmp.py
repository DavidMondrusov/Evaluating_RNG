"""Static ECMP over shortest paths: each switch hashes flows across all
neighbors one hop closer to the destination host's switch."""

import networkx
from mininet.node import OVSSwitch

from . import ovs_batch


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

    flows = {s.name: ["priority=0,actions=drop"] for s in switches}
    groups = {s.name: {} for s in switches}  # port tuple -> group id

    for host in net.hosts:
        (edge,) = [n for n in graph.neighbors(host.name) if n in fabric]
        distance = networkx.single_source_shortest_path_length(fabric, edge)
        for switch in fabric:
            if switch not in distance:
                continue
            if switch == edge:
                out = [ports[edge, host.name]]
            else:
                out = sorted(
                    ports[switch, n] for n in fabric.neighbors(switch)
                    if distance.get(n) == distance[switch] - 1
                )
            if not out:
                raise ValueError(
                    f"No shortest-path next hop from switch {switch!r} "
                    f"to host {host.name!r}"
                )
            if len(out) == 1:
                action = f"output:{out[0]}"
            else:
                group_id = groups[switch].setdefault(
                    tuple(out), len(groups[switch]) + 1
                )
                action = f"group:{group_id}"
            flows[switch].append(
                f"priority=100,ip,nw_dst={host.IP()},"
                f"actions={action}"
            )

    ovs_batch.install({
        name: (
            [
                f"group_id={group_id},type=select,"
                + ",".join(f"bucket=actions=output:{p}" for p in out)
                for out, group_id in groups[name].items()
            ],
            flows[name],
        )
        for name in flows
    })
    ovs_batch.install_static_arp(net)
    return {
        "deployed": True,
        "deployment_mode": "ecmp",
        "switch_count": len(switches),
        "flow_count": sum(len(f) - 1 for f in flows.values()),  # minus drop rule
        "group_count": sum(
            len(groups_by_ports) for groups_by_ports in groups.values()
        ),
    }
