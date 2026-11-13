"""High-level API: deploy Spraypoint on a running Mininet network."""

from __future__ import annotations

import logging
from typing import Dict, List, Mapping, Optional, Sequence

from . import ovs
from .fabric import FabricGraph
from .levels import LevelAssignment
from .mininet_adapter import graph_from_mininet, install_static_arp, ovs_switches
from .params import SpraypointParams
from .rules import RuleCompiler, SwitchRules

log = logging.getLogger(__name__)


class SpraypointDeployer:
    """Install Spraypoint rules on every OVS switch of ``net``.

    Example::

        net.start()
        dep = SpraypointDeployer(net, SpraypointParams(p=2, h=1, l=1))
        dep.deploy_full_mesh()            # or dep.deploy_pair("h_v2", "h_t")
        print(dep.dump("v2"))

    The network must already be started. Switches are switched to
    ``fail_mode=secure`` with OpenFlow 1.3 and their controllers detached
    (pass ``detach_controllers=False`` to keep them), so only Spraypoint rules
    forward traffic.
    """

    def __init__(self, net, params: SpraypointParams,
                 waypoint_overrides: Optional[Mapping[str, Mapping[str, Sequence[str]]]] = None,
                 subnets: Optional[Mapping[str, object]] = None,
                 static_arp: bool = True,
                 detach_controllers: bool = True,
                 dec_ttl: bool = True) -> None:
        self.net = net
        self.params = params
        self.static_arp = static_arp
        self.detach_controllers = detach_controllers
        self.graph: FabricGraph = graph_from_mininet(net)
        self.compiler = RuleCompiler(self.graph, params, waypoint_overrides, subnets, dec_ttl)
        self._switches = {sw.name: sw for sw in ovs_switches(net)}
        self.installed: Dict[str, SwitchRules] = {}

    # ----------------------------------------------------------- deploy
    def deploy_full_mesh(self) -> Dict[str, SwitchRules]:
        """Rules toward every host on every switch."""
        return self._apply(self.compiler.compile_full_mesh())

    def deploy_pair(self, source_host: str, target_host: str,
                    bidirectional: bool = True) -> Dict[str, SwitchRules]:
        """Rules for one host pair only (both directions by default).

        Switches outside the pair's forwarding trees are left with just the
        table-miss drop, so the dumped tables read like the paper walkthrough.
        """
        return self._apply(self.compiler.compile_pair(source_host, target_host, bidirectional))

    def clear(self) -> None:
        for sw in self._switches.values():
            ovs.clear_switch(sw)
        self.installed = {}

    def _apply(self, rules: Dict[str, SwitchRules]) -> Dict[str, SwitchRules]:
        for name, sw in self._switches.items():
            ovs.prepare_switch(sw, self.detach_controllers)
            if name in rules:
                ovs.install(sw, rules[name])
        if self.static_arp:
            install_static_arp(self.net, self.graph)
        self.installed = rules
        total_f = sum(len(r.flows) for r in rules.values())
        total_g = sum(len(r.groups) for r in rules.values())
        log.info("installed %d flows and %d groups on %d switches", total_f, total_g, len(rules))
        return rules

    # ----------------------------------------------------------- inspect
    def levels(self, destination: str) -> LevelAssignment:
        """Level partition toward a switch (or a host's switch)."""
        return self.compiler.levels(self.graph.switch_of(destination))

    def dump(self, switch: str, destination: Optional[str] = None) -> str:
        """``dump-groups`` + ``dump-flows`` for a switch, optionally filtered by destination."""
        sw = self._switches[switch]
        cookie = None
        if destination is not None:
            cookie = self.compiler.cookie(self.graph.switch_of(destination))
        return (f"== {switch} groups ==\n{ovs.dump_groups(sw).strip()}\n"
                f"== {switch} flows ==\n{ovs.dump_flows(sw, cookie).strip()}\n")

    def group_stats(self, switch: str) -> str:
        return ovs.dump_group_stats(self._switches[switch])


def plan_script(rules: Dict[str, SwitchRules]) -> List[str]:
    """Shell commands equivalent to a deployment (useful for dry runs)."""
    return ovs.render_script(rules[sw] for sw in sorted(rules))
