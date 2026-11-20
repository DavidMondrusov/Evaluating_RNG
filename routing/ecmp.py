"""Loop-free ECMP for a Clos fabric.

Approximates a routed leaf-spine: static ARP (ARP suppression), controllers
detached, and per-destination select groups across equal-cost next hops.
No TTL decrement or per-hop MAC rewrite.
"""

import shlex
from collections import deque


DEFAULT_PARAMETERS = {}
OFCTL = "ovs-ofctl -O OpenFlow13"


def controller_class():
    return None


def _run(switch, command):
    output = switch.cmd(f"{command}; echo __rc=$?")
    body, _, status = output.rpartition("__rc=")
    if status.strip() != "0":
        raise RuntimeError(f"{switch.name}: {body.strip()}")
    return body


def _switch_adjacency(net):
    switches = set(net.switches)
    adjacency = {switch: {} for switch in net.switches}
    for switch in net.switches:
        for port, intf in switch.intfs.items():
            link = intf.link
            if link is None:
                continue
            peer = link.intf1 if link.intf2 is intf else link.intf2
            if peer.node in switches:
                adjacency[switch][peer.node] = port
    return adjacency


def _host_attachments(net):
    attachments = {}
    for host in net.hosts:
        intf = host.defaultIntf()
        peer = intf.link.intf1 if intf.link.intf2 is intf else intf.link.intf2
        port = peer.node.ports[peer]
        attachments[host] = (peer.node, port)
    return attachments


def _distances(adjacency, root):
    distances = {root: 0}
    queue = deque([root])
    while queue:
        node = queue.popleft()
        for neighbour in adjacency[node]:
            if neighbour not in distances:
                distances[neighbour] = distances[node] + 1
                queue.append(neighbour)
    return distances


def configure(net, parameters):
    adjacency = _switch_adjacency(net)
    attachments = _host_attachments(net)
    net.staticArp()

    for switch in net.switches:
        bridge = shlex.quote(switch.name)
        _run(
            switch,
            f"ovs-vsctl set bridge {bridge} "
            "protocols=OpenFlow10,OpenFlow13 stp_enable=false "
            f"-- set-fail-mode {bridge} secure",
        )
        _run(switch, f"ovs-vsctl del-controller {bridge}")
        _run(switch, f"{OFCTL} del-flows {bridge}")
        _run(switch, f"{OFCTL} del-groups {bridge}")
        _run(switch, f"{OFCTL} add-flow {bridge} priority=0,actions=drop")

    flows = 0
    groups = 0
    for group_id, host in enumerate(
        sorted(net.hosts, key=lambda node: node.name), start=1
    ):
        mac = host.MAC()
        attachment_switch, attachment_port = attachments[host]
        distances = _distances(adjacency, attachment_switch)
        for switch in net.switches:
            bridge = shlex.quote(switch.name)
            if switch is attachment_switch:
                flow = (
                    f"priority=100,dl_dst={mac},"
                    f"actions=output:{attachment_port}"
                )
            elif switch in distances:
                next_hops = [
                    port
                    for neighbour, port in adjacency[switch].items()
                    if distances.get(neighbour) == distances[switch] - 1
                ]
                if not next_hops:
                    continue
                if len(next_hops) == 1:
                    flow = (
                        f"priority=100,dl_dst={mac},"
                        f"actions=output:{next_hops[0]}"
                    )
                else:
                    buckets = ",".join(
                        f"bucket=actions=output:{port}"
                        for port in next_hops
                    )
                    spec = f"group_id={group_id},type=select,{buckets}"
                    _run(
                        switch,
                        f"{OFCTL} add-group {bridge} {shlex.quote(spec)}",
                    )
                    groups += 1
                    flow = f"priority=100,dl_dst={mac},actions=group:{group_id}"
            else:
                continue
            _run(switch, f"{OFCTL} add-flow {bridge} {shlex.quote(flow)}")
            flows += 1

    return {
        "deployed": True,
        "deployment_mode": "ecmp",
        "switch_count": len(net.switches),
        "flow_count": flows,
        "group_count": groups,
    }
