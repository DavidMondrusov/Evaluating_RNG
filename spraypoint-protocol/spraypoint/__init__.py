"""Spraypoint routing for Mininet/Open vSwitch networks (OpenFlow 1.3).

Pure-protocol modules (no Mininet import needed):
    params   - SpraypointParams, compute_levels_count
    fabric   - FabricGraph, HostAttachment
    levels   - compute_levels, LevelAssignment, check_loop_free
    rules    - RuleCompiler, SwitchRules, Flow, Group

Mininet/OVS modules (imported lazily):
    mininet_adapter - graph_from_mininet, install_static_arp
    ovs             - ovs-vsctl / ovs-ofctl driver
    deployer        - SpraypointDeployer
"""

from .fabric import FabricGraph, HostAttachment
from .levels import LevelAssignment, check_loop_free, compute_levels
from .params import SpraypointParams, compute_levels_count
from .rules import RuleCompiler, SwitchRules

__all__ = [
    "FabricGraph", "HostAttachment", "LevelAssignment", "RuleCompiler",
    "SpraypointDeployer", "SpraypointParams", "SwitchRules",
    "check_loop_free", "compute_levels", "compute_levels_count",
]


def __getattr__(name):
    if name == "SpraypointDeployer":
        from .deployer import SpraypointDeployer
        return SpraypointDeployer
    raise AttributeError(name)
