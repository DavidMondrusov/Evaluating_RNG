"""Compile Spraypoint level assignments into OpenFlow 1.3 flows and groups.

Per switch the pipeline is a single table with these priorities:

    300  ip,nw_dst=<local host>/32                -> output:<host port>    (delivery)
    200  in_port=<host port>,ip,nw_dst=<dst>      -> group:1               (spraying, "server-facing VRF")
    110  in_port=<P>,ip,nw_dst=<dst>              -> in_port / group:N     (pointing, bounce back out P)
    100  ip,nw_dst=<dst>                          -> output / group:N      (pointing, "fabric-facing VRF")
      0  (table miss)                             -> drop

OpenFlow silently drops ``output:P`` when P is the packet's ingress port, but
Spraypoint relies on exactly that move: v2 sprays to w3, and w3 points back
to v2 over the same link. Every pointing rule whose next-hop ports could also
be the ingress port therefore gets a priority-110 twin per such port that
uses the ``in_port`` action (or a group whose bucket for P is ``in_port``).

The ``in_port`` match on the host port is what separates the two VRFs: a
packet only gets sprayed when it comes from a local host. A packet that
comes back to its source switch over the fabric hits the pointing rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from .fabric import FabricGraph, HostAttachment
from .levels import LevelAssignment, check_loop_free, compute_levels
from .params import SpraypointParams

PRIO_DELIVER = 300
PRIO_SPRAY = 200
PRIO_BOUNCE = 110
PRIO_POINT = 100
PRIO_MISS = 0

SPRAY_GROUP_ID = 1
FIRST_POINT_GROUP_ID = 100


@dataclass(frozen=True)
class Group:
    group_id: int
    ports: Tuple[int, ...]
    purpose: str  # "spray" or "point"
    ingress: Optional[int] = None  # bucket for this port sends back out in_port

    def bucket_action(self, port: int) -> str:
        return "in_port" if port == self.ingress else f"output:{port}"

    def to_ofctl(self) -> str:
        buckets = ",".join(f"bucket=actions={self.bucket_action(p)}" for p in self.ports)
        return f"group_id={self.group_id},type=select,{buckets}"


@dataclass(frozen=True)
class Flow:
    priority: int
    match: str
    actions: str
    cookie: int = 0
    comment: str = ""

    def to_ofctl(self) -> str:
        parts = [f"cookie={self.cookie:#x}", f"priority={self.priority}"]
        if self.match:
            parts.append(self.match)
        parts.append(f"actions={self.actions}")
        return ",".join(parts)


@dataclass
class SwitchRules:
    switch: str
    groups: Dict[int, Group] = field(default_factory=dict)
    flows: List[Flow] = field(default_factory=list)
    _point_groups: Dict[Tuple[Tuple[int, ...], Optional[int]], int] = field(default_factory=dict, repr=False)
    _flow_keys: Set[Tuple[int, str]] = field(default_factory=set, repr=False)

    def add_flow(self, flow: Flow) -> None:
        key = (flow.priority, flow.match)
        if key in self._flow_keys:
            return
        self._flow_keys.add(key)
        self.flows.append(flow)

    def spray_group(self, ports: Sequence[int]) -> int:
        if SPRAY_GROUP_ID not in self.groups:
            self.groups[SPRAY_GROUP_ID] = Group(SPRAY_GROUP_ID, tuple(sorted(ports)), "spray")
        return SPRAY_GROUP_ID

    def point_group(self, ports: Sequence[int], ingress: Optional[int] = None) -> int:
        """Shared select group per distinct (next-hop port set, ingress) pair."""
        key = (tuple(sorted(ports)), ingress)
        if key not in self._point_groups:
            gid = FIRST_POINT_GROUP_ID + len(self._point_groups)
            self._point_groups[key] = gid
            self.groups[gid] = Group(gid, key[0], "point", ingress)
        return self._point_groups[key]


DestMatch = Union[str, Sequence[str]]


class RuleCompiler:
    """Turns a :class:`FabricGraph` plus parameters into per-switch rules.

    waypoint_overrides: ``{destination_switch: {wp_node: [selected, ...]}}``
        pins waypoint selections for specific destinations (see
        :func:`spraypoint.levels.compute_levels`).
    subnets: ``{switch: "10.3.0.0/16" | [...]}`` optional destination
        prefixes for each switch's hosts. Without it, every host is matched by
        its own /32.
    dec_ttl: decrement TTL on fabric hops as a safety net against loops.
    """

    def __init__(self, graph: FabricGraph, params: SpraypointParams,
                 waypoint_overrides: Optional[Mapping[str, Mapping[str, Sequence[str]]]] = None,
                 subnets: Optional[Mapping[str, DestMatch]] = None,
                 dec_ttl: bool = True) -> None:
        self.graph = graph
        self.params = params
        self.waypoint_overrides = dict(waypoint_overrides or {})
        self.subnets = dict(subnets or {})
        self.dec_ttl = dec_ttl
        self._levels: Dict[str, LevelAssignment] = {}
        self._cookies = {sw: i for i, sw in enumerate(graph.switches, start=1)}

    # ----------------------------------------------------------- levels
    def levels(self, destination: str) -> LevelAssignment:
        if destination not in self._levels:
            la = compute_levels(self.graph, destination, self.params,
                                self.waypoint_overrides.get(destination))
            check_loop_free(la)
            self._levels[destination] = la
        return self._levels[destination]

    def cookie(self, destination: str) -> int:
        """Cookie tagging every rule that serves ``destination`` (for dump-flows filters)."""
        return self._cookies[destination]

    def destination_matches(self, destination: str) -> List[str]:
        if destination in self.subnets:
            value = self.subnets[destination]
            return [value] if isinstance(value, str) else list(value)
        return [f"{h.ip}/32" for h in self.graph.hosts.get(destination, [])]

    # ----------------------------------------------------------- modes
    def compile_full_mesh(self) -> Dict[str, SwitchRules]:
        """Rules on every switch for every destination switch that has hosts."""
        rules = self._empty()
        for t in self.graph.switches:
            matches = self.destination_matches(t)
            if not matches:
                continue
            for h in self.graph.hosts[t]:
                self._deliver(rules[t], h)
            for s in self.graph.switches:
                if s == t:
                    continue
                self._point(rules[s], s, t, matches)
                for hp in self.graph.host_ports(s):
                    self._spray(rules[s], s, hp, t, matches)
        return rules

    def compile_pair(self, source_host: str, target_host: str,
                     bidirectional: bool = True) -> Dict[str, SwitchRules]:
        """Only the rules needed for ``source_host -> target_host``.

        Pointing rules go on every switch the sprayed packet can reach (the
        union of pointing trees rooted at each fabric neighbor of the source
        switch). With ``bidirectional`` the reverse direction is added too,
        which ``ping``/TCP need for replies.
        """
        rules = self._empty()
        src = self.graph.host(source_host)
        dst = self.graph.host(target_host)
        self._pair_direction(rules, src, dst)
        if bidirectional:
            self._pair_direction(rules, dst, src)
        # Drop switches that only hold the table miss; the deployer still wipes
        # them, and fail_mode=secure drops anything they receive.
        return {sw: r for sw, r in rules.items() if len(r.flows) > 1 or r.groups}

    def pair_switches(self, source_host: str, target_host: str) -> Set[str]:
        """Switches that a packet from source to target can traverse."""
        s = self.graph.host(source_host).switch
        t = self.graph.host(target_host).switch
        if s == t:
            return {s}
        la = self.levels(t)
        reached = {s}
        for v in self.graph.neighbors(s):
            reached |= la.path_tree(v)
        return reached

    # ----------------------------------------------------------- helpers
    def _empty(self) -> Dict[str, SwitchRules]:
        rules = {sw: SwitchRules(sw) for sw in self.graph.switches}
        for r in rules.values():
            r.add_flow(Flow(PRIO_MISS, "", "drop", 0, "table miss"))
        return rules

    def _pair_direction(self, rules: Dict[str, SwitchRules],
                        src: HostAttachment, dst: HostAttachment) -> None:
        s, t = src.switch, dst.switch
        matches = [f"{dst.ip}/32"]
        self._deliver(rules[t], dst)
        if s == t:
            return
        self._spray(rules[s], s, src.port, t, matches)
        for u in self.pair_switches(src.name, dst.name):
            if u != t:
                self._point(rules[u], u, t, matches)

    def _deliver(self, r: SwitchRules, host: HostAttachment) -> None:
        r.add_flow(Flow(PRIO_DELIVER, f"ip,nw_dst={host.ip}/32", f"output:{host.port}",
                        self.cookie(host.switch), f"deliver to {host.name}"))

    def _spray(self, r: SwitchRules, s: str, host_port: int, t: str,
               matches: Iterable[str]) -> None:
        fabric = self.graph.fabric_ports(s)
        if not fabric:
            return
        gid = r.spray_group(fabric)
        for m in matches:
            r.add_flow(Flow(PRIO_SPRAY, f"in_port={host_port},ip,nw_dst={m}", f"group:{gid}",
                            self.cookie(t), f"spray toward {t}"))

    def _point(self, r: SwitchRules, u: str, t: str, matches: Iterable[str]) -> None:
        la = self.levels(t)
        hops = la.next_hops.get(u)
        if not hops:
            return  # disconnected from t
        ports = sorted(p for v in hops for p in self.graph.ports_to(u, v))
        prefix = "dec_ttl," if self.dec_ttl else ""
        out = f"output:{ports[0]}" if len(ports) == 1 else f"group:{r.point_group(ports)}"
        comment = f"{la.level_of(u)} -> {','.join(hops)} toward {t}"
        for m in matches:
            r.add_flow(Flow(PRIO_POINT, f"ip,nw_dst={m}", prefix + out, self.cookie(t), comment))
            # A packet that arrived on one of the next-hop ports (e.g. the
            # source sprayed it here and we point straight back) must leave
            # through the in_port action, or OVS drops it.
            for p in ports:
                bounce = "in_port" if len(ports) == 1 else f"group:{r.point_group(ports, p)}"
                r.add_flow(Flow(PRIO_BOUNCE, f"in_port={p},ip,nw_dst={m}", prefix + bounce,
                                self.cookie(t), comment + " (bounce)"))
