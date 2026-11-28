import importlib.util
import ipaddress
import sys
from pathlib import Path

from mininet_batch import link_is_up


PROTOCOL_ROOT = Path(__file__).resolve().parent.parent / "spraypoint-protocol"
DEFAULT_PARAMETERS = {
    "p": 2,
    "h": 1,
    "l": None,
    "hash_key": "spraypoint",
}


def controller_class():
    return None


def load_protocol():
    if not PROTOCOL_ROOT.is_dir():
        raise FileNotFoundError(
            f"Spraypoint protocol directory not found: {PROTOCOL_ROOT}"
        )
    protocol_path = str(PROTOCOL_ROOT)
    if protocol_path not in sys.path:
        sys.path.insert(0, protocol_path)

    from spraypoint import SpraypointDeployer, SpraypointParams

    return SpraypointDeployer, SpraypointParams


def fabric_from_topology(topology):
    """Offline Spraypoint FabricGraph for an FNSS topology, without Mininet.

    Mirrors what ``fnss.to_mininet(topology, relabel_nodes=True)`` plus
    ``Mininet(...)`` would create: sorted switches become s1.., sorted hosts
    become h1.. with Mininet's default IPs (10.0.0.1, ...), and each switch
    numbers its ports from 1 in ``topology.edges()`` order. Useful for dry
    runs and tests; a live deployment reads the real ports from ``net``.
    """
    load_protocol()
    from spraypoint import FabricGraph, HostAttachment

    def sorted_nodes(kind):
        return sorted(
            node for node, attributes in topology.nodes(data=True)
            if attributes.get("type") == kind
        )

    switch_names = {
        node: f"s{index}"
        for index, node in enumerate(sorted_nodes("switch"), start=1)
    }
    host_names = {
        node: f"h{index}"
        for index, node in enumerate(sorted_nodes("host"), start=1)
    }
    base_ip = ipaddress.ip_address("10.0.0.0")

    fabric = FabricGraph()
    for name in switch_names.values():
        fabric.add_switch(name)
    next_port = dict.fromkeys(switch_names.values(), 1)
    for u, v in topology.edges():
        if u in switch_names and v in switch_names:
            su, sv = switch_names[u], switch_names[v]
            fabric.add_fabric_link(su, next_port[su], sv, next_port[sv])
            next_port[su] += 1
            next_port[sv] += 1
            continue
        switch, host = (u, v) if u in switch_names else (v, u)
        if switch not in switch_names or host not in host_names:
            continue
        sw, name = switch_names[switch], host_names[host]
        index = int(name[1:])
        fabric.add_host(HostAttachment(
            name=name, ip=str(base_ip + index), mac=None,
            switch=sw, port=next_port[sw],
        ))
        next_port[sw] += 1
    return fabric


def load_simulator():
    """Flow-table packet simulator from the protocol's offline tests.

    Follows every spray/ECMP branch of compiled rules; used for dry runs.
    """
    load_protocol()
    path = PROTOCOL_ROOT / "tests" / "test_spraypoint.py"
    spec = importlib.util.spec_from_file_location("spraypoint_simulator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Simulator


def configure(net, parameters):
    SpraypointDeployer, SpraypointParams = load_protocol()

    params = SpraypointParams(**parameters)
    deployer = SpraypointDeployer(net, params)
    # Remove links that are down from Spraypoint's view of the network, so it
    # computes routes from the surviving links, as its link-state protocol does.
    for link in net.links:
        if not link_is_up(link):
            a, b = link.intf1.node.name, link.intf2.node.name
            deployer.graph.ports.get(a, {}).pop(b, None)
            deployer.graph.ports.get(b, {}).pop(a, None)
    installed = deployer.deploy_full_mesh()
    return {
        "deployed": True,
        "switch_count": len(installed),
        "flow_count": sum(len(rules.flows) for rules in installed.values()),
        "group_count": sum(len(rules.groups) for rules in installed.values()),
        "deployment_mode": "full_mesh",
    }
