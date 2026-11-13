"""Figure 3 of the Spraypoint overview: Mininet builder plus the paper's expectations.

The switch-level edges come from ``init_topology_nodes.py``. Every switch gets
one host ``h_<switch>``. Every interface is named ``<source>-<target>``:
the end of a link that sits on node ``a`` and leads to node ``b`` is ``a-b``
(``v2-w3`` on v2 toward w3, ``v2-h_v2`` toward v2's host, ``h_v2-v2`` inside
the host).
"""

from __future__ import annotations

from .init_topology_nodes import edges

# Waypoint picks drawn in the figure, for destination t with p=2:
# each WP0 node selects two neighbors that are not t / WP0.
PAPER_WAYPOINTS_T = {
    "v1": ["w1", "w2"],
    "v2": ["w3", "w4"],
    "v3": ["w5", "w6"],
    "v4": ["w7", "w8"],
}

# Level table from the overview (destination t).
PAPER_LEVELS_T = {
    "t": ["t"],
    "WP0": ["v1", "v2", "v3", "v4"],
    "WP1": ["w1", "w2", "w3", "w4", "w5", "w6", "w7", "w8"],
    "IR": [f"r{i}" for i in range(1, 16)],
    "OR": ["o1", "o2", "o3"],
}


def switch_names():
    order = []
    for u, v in edges:
        for n in (u, v):
            if n not in order:
                order.append(n)
    return order


def host_name(switch: str) -> str:
    return f"h_{switch}"


def intf_name(source: str, target: str) -> str:
    """Interface on ``source`` that leads to ``target``."""
    name = f"{source}-{target}"
    if len(name) > 15:  # Linux IFNAMSIZ - 1
        raise ValueError(f"interface name {name!r} exceeds 15 characters")
    return name


def subnets():
    """Each switch owns 10.<index>.0.0/16; its host is 10.<index>.0.1."""
    return {sw: f"10.{i}.0.0/16" for i, sw in enumerate(switch_names(), start=1)}


def build_network(bw_mbps=None):
    """Return an unstarted Mininet network for Figure 3 (no controller, OF1.3 OVS)."""
    from functools import partial

    from mininet.link import Link, TCLink
    from mininet.net import Mininet
    from mininet.node import OVSSwitch

    switch_cls = partial(OVSSwitch, protocols="OpenFlow13", failMode="secure")
    link_cls = TCLink if bw_mbps else Link
    net = Mininet(topo=None, switch=switch_cls, controller=None, link=link_cls,
                  autoSetMacs=False, build=False)
    link_opts = {"bw": bw_mbps} if bw_mbps else {}

    names = switch_names()
    for i, sw in enumerate(names, start=1):
        # Names like "t" or "v1" do not encode a datapath id Mininet can parse.
        net.addSwitch(sw, dpid=f"{i:016x}")
    for i, sw in enumerate(names, start=1):
        host = net.addHost(host_name(sw), ip=f"10.{i}.0.1/8")
        net.addLink(host, sw, intfName1=intf_name(host.name, sw),
                    intfName2=intf_name(sw, host.name), **link_opts)
    for u, v in edges:
        net.addLink(u, v, intfName1=intf_name(u, v), intfName2=intf_name(v, u), **link_opts)
    net.build()
    return net
