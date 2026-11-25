"""Static ECMP over shortest paths: each switch hashes flows across all
neighbors one hop closer to the destination host's switch."""

import os
import shlex
import subprocess
import tempfile

import networkx
from mininet.node import OVSSwitch

from mininet_batch import static_arp


DEFAULT_PARAMETERS = {}
# Each switch's whole table (clear, groups, flows) is loaded as one atomic
# OpenFlow 1.4 bundle: ovs-vswitchd commits it in a single operation, whereas
# separate flow mods and separate ovs-ofctl calls each cost time that grows
# with the total number of ports (~1.1 s per switch over 4+ calls at 34k).
OFCTL_BUNDLE = "ovs-ofctl -O OpenFlow14 bundle"
PROTOCOLS = "OpenFlow13,OpenFlow14"
# Keeps one ovs-vsctl argument list far below ARG_MAX (~50 bytes per bridge).
BRIDGES_PER_TRANSACTION = 1000


def controller_class():
    return None


def _run(switch, command):
    output = switch.cmd(f"{command}; echo __rc=$?")
    body, _, status = output.rpartition("__rc=")
    if status.strip() != "0":
        raise RuntimeError(f"{switch.name}: {body.strip()}")
    return body


def _configure_bridges(bridges, run=subprocess.run):
    """Set OpenFlow 1.3+1.4, secure fail mode and no controller on every bridge.

    Each ovs-vsctl transaction makes ovs-vswitchd reconfigure all bridges and
    ports before it returns, so one transaction per setting per switch costs
    O(switches * ports). Chaining every bridge into as few transactions as
    possible makes this a single reconfiguration. Runs ovs-vsctl directly
    (argument list, no shell): Mininet node shells use a pty, which is not
    safe for very long command lines.
    """
    for start in range(0, len(bridges), BRIDGES_PER_TRANSACTION):
        args = ["ovs-vsctl"]
        for bridge in bridges[start:start + BRIDGES_PER_TRANSACTION]:
            args += [
                "--", "set", "bridge", bridge, f"protocols={PROTOCOLS}",
                "--", "set-fail-mode", bridge, "secure",
                "--", "del-controller", bridge,
            ]
        result = run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ovs-vsctl failed configuring bridges: {result.stdout.strip()}"
            )


def bundle_lines(groups, flows):
    """Lines of an ``ovs-ofctl bundle`` file that replace a switch's tables.

    groups: {tuple of output ports: group id}; flows: flow specs (match and
    actions). Existing flows and groups are cleared first, then a priority 0
    drop, the select groups and the flows that reference them are added, all
    in order, in one transaction.
    """
    lines = [
        "flow delete",
        "group delete group_id=all",
        "flow add priority=0,actions=drop",
    ]
    for out, group_id in groups.items():
        buckets = ",".join(f"bucket=actions=output:{p}" for p in out)
        lines.append(f"group add group_id={group_id},type=select,{buckets}")
    lines += [f"flow add {flow}" for flow in flows]
    return lines


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

    _configure_bridges([switch.name for switch in switches])

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
                f"priority=100,ip,nw_dst={shlex.quote(host.IP())},"
                f"actions={action}"
            )

    for switch in switches:
        fd, path = tempfile.mkstemp()
        try:
            with os.fdopen(fd, "w") as bundle_file:
                bundle_file.write(
                    "\n".join(
                        bundle_lines(groups[switch.name], flows[switch.name])
                    ) + "\n"
                )
            _run(
                switch,
                f"{OFCTL_BUNDLE} {shlex.quote(switch.name)} "
                f"{shlex.quote(path)}",
            )
        finally:
            os.unlink(path)
    static_arp(net)
    return {
        "deployed": True,
        "deployment_mode": "ecmp",
        "switch_count": len(switches),
        "flow_count": sum(len(f) for f in flows.values()),
        "group_count": sum(
            len(groups_by_ports) for groups_by_ports in groups.values()
        ),
    }
