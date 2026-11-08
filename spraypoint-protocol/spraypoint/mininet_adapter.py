"""Read a live ``Mininet`` network into a :class:`FabricGraph`.

Nothing here depends on how the network was created (``Topo`` subclass,
``fnss.to_mininet``, a NetworkX conversion, or hand-written ``addSwitch``
calls). It walks ``net.links`` and classifies each link by the node types at
its two ends:

* OVS switch <-> OVS switch : fabric link (both OpenFlow port numbers kept)
* host <-> OVS switch       : host port on that switch
* anything else             : ignored (e.g. links to non-OVS switches)
"""

from __future__ import annotations

import logging
from typing import List

from .fabric import FabricGraph, HostAttachment

log = logging.getLogger(__name__)


def _is_ovs(node) -> bool:
    from mininet.node import OVSSwitch
    return isinstance(node, OVSSwitch)


def _is_host(node) -> bool:
    from mininet.node import Host
    return isinstance(node, Host)


def ovs_switches(net) -> List:
    return [sw for sw in net.switches if _is_ovs(sw)]


def graph_from_mininet(net) -> FabricGraph:
    graph = FabricGraph()
    for sw in ovs_switches(net):
        graph.add_switch(sw.name)

    for link in net.links:
        a, b = link.intf1, link.intf2
        na, nb = a.node, b.node
        if _is_ovs(na) and _is_ovs(nb):
            graph.add_fabric_link(na.name, na.ports[a], nb.name, nb.ports[b], a.name, b.name)
        elif _is_ovs(na) and _is_host(nb):
            _add_host(graph, nb, b, na, a)
        elif _is_host(na) and _is_ovs(nb):
            _add_host(graph, na, a, nb, b)
        else:
            log.debug("ignoring link %s <-> %s", a, b)
    return graph


def _add_host(graph: FabricGraph, host, host_intf, switch, switch_intf) -> None:
    ip = host_intf.IP() or host.IP()
    if not ip:
        log.warning("host %s has no IP on %s; skipping", host.name, host_intf)
        return
    graph.add_host(HostAttachment(
        name=host.name, ip=ip, mac=host_intf.MAC(), switch=switch.name,
        port=switch.ports[switch_intf], port_name=switch_intf.name,
    ))


def install_static_arp(net, graph: FabricGraph) -> None:
    """Give every host a static ARP entry for every other host.

    Spraypoint is an L3 scheme: the switches route on ``nw_dst`` and drop
    everything else, including ARP broadcasts (flooding them on a cyclic
    fabric would loop). Static ARP lets hosts in one flat subnet send IP
    directly.
    """
    attached = graph.all_hosts()
    for entry in attached:
        host = net.get(entry.name)
        for other in attached:
            if other.name != entry.name and other.mac:
                host.setARP(other.ip, other.mac)
