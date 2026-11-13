"""Offline tests: level partitioning, loop freedom, and rule-level packet walks.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import ipaddress
import random
import re
import unittest

from init_topology_test import figure3
from spraypoint import FabricGraph, RuleCompiler, SpraypointParams, compute_levels, compute_levels_count
from spraypoint.levels import check_loop_free

PARAMS = SpraypointParams(p=2, h=1, l=1)


def figure_graph() -> FabricGraph:
    return FabricGraph.from_edges(figure3.edges, host_ip=lambda sw, i, k: f"10.{i}.0.1")


def random_regular_edges(n: int, d: int, seed: int):
    """Configuration-model random d-regular simple graph (retry until simple)."""
    rng = random.Random(seed)
    while True:
        stubs = [i for i in range(n) for _ in range(d)]
        rng.shuffle(stubs)
        pairs = list(zip(stubs[::2], stubs[1::2]))
        if all(a != b for a, b in pairs) and len({frozenset(p) for p in pairs}) == len(pairs):
            return [(f"s{a}", f"s{b}") for a, b in pairs]


class Simulator:
    """Walks packets through compiled rules, following every group bucket."""

    _MATCH = re.compile(r"(\w+)=([^,]+)")

    def __init__(self, graph: FabricGraph, rules):
        self.graph = graph
        self.rules = rules
        self.peer = {}  # (switch, port) -> switch
        for u in graph.switches:
            for v, plist in graph.ports[u].items():
                for p in plist:
                    self.peer[(u, p)] = v
        self.host_ports = {(h.switch, h.port): h for h in graph.all_hosts()}

    def _lookup(self, sw, in_port, ip):
        best = None
        for f in self.rules.get(sw).flows if sw in self.rules else []:
            fields = dict(self._MATCH.findall(f.match))
            if "in_port" in fields and int(fields["in_port"]) != in_port:
                continue
            if "nw_dst" in fields and ip not in ipaddress.ip_network(fields["nw_dst"]):
                continue
            if best is None or f.priority > best.priority:
                best = f
        return best

    def deliveries(self, src_host, dst_host, max_hops=32):
        """Return the set of outcomes over all spray/ECMP choices."""
        src = self.graph.host(src_host)
        ip = ipaddress.ip_address(self.graph.host(dst_host).ip)
        outcomes, stack = set(), [(src.switch, src.port, 0, (src.switch,))]
        while stack:
            sw, in_port, hops, path = stack.pop()
            if hops > max_hops:
                outcomes.add(("loop", path))
                continue
            flow = self._lookup(sw, in_port, ip)
            if flow is None or flow.actions == "drop":
                outcomes.add(("drop", path))
                continue
            target = flow.actions.split(",")[-1]
            if target == "in_port":
                ports = (in_port,)
            elif target.startswith("group:"):
                group = self.rules[sw].groups[int(target.split(":")[1])]
                ports = tuple(in_port if group.bucket_action(p) == "in_port" else p
                              for p in group.ports)
            else:
                ports = (int(target.split(":")[1]),)
            for port in ports:
                # OpenFlow: output:P is a no-op when P is the ingress port.
                if port == in_port and target != "in_port" and not (
                        target.startswith("group:") and
                        self.rules[sw].groups[int(target.split(":")[1])].ingress == in_port):
                    outcomes.add(("drop", path))
                    continue
                if (sw, port) in self.host_ports:
                    outcomes.add(("delivered", self.host_ports[(sw, port)].name))
                else:
                    nxt = self.peer[(sw, port)]
                    back = next(p for p in self.graph.ports[nxt][sw])
                    stack.append((nxt, back, hops + 1, path + (nxt,)))
        return outcomes


class LevelTests(unittest.TestCase):
    def test_compute_l_formula(self):
        self.assertEqual(compute_levels_count(10_000, 64, 2), 1)
        self.assertEqual(compute_levels_count(35, 3, 2), 1)
        self.assertEqual(compute_levels_count(100_000, 8, 2), 10)  # ceil(log2(781.25))

    def test_figure3_with_paper_waypoints(self):
        la = compute_levels(figure_graph(), "t", PARAMS, figure3.PAPER_WAYPOINTS_T)
        table = la.table()
        self.assertEqual(table, figure3.PAPER_LEVELS_T)
        # Walkthrough: w3/w4 point back to v2, v2 goes to t.
        self.assertEqual(la.next_hops["w3"], ["v2"])
        self.assertEqual(la.next_hops["w4"], ["v2"])
        self.assertEqual(la.next_hops["v2"], ["t"])
        check_loop_free(la)

    def test_invalid_override_rejected(self):
        with self.assertRaises(ValueError):
            compute_levels(figure_graph(), "t", PARAMS, {"v1": ["v2"]})

    def test_loop_free_every_destination_hashed(self):
        for edges in (figure3.edges, random_regular_edges(40, 4, 7)):
            g = FabricGraph.from_edges(edges)
            for params in (PARAMS, SpraypointParams(p=2, h=2, l=2, hash_key="k")):
                for t in g.switches:
                    la = compute_levels(g, t, params)
                    check_loop_free(la)
                    for u, hops in la.next_hops.items():
                        self.assertLessEqual(len(hops), params.h)
                        for v in hops:
                            self.assertIn(v, g.ports[u])

    def test_hashing_is_deterministic(self):
        g = figure_graph()
        a = compute_levels(g, "t", PARAMS).table()
        b = compute_levels(g, "t", PARAMS).table()
        self.assertEqual(a, b)


class RuleTests(unittest.TestCase):
    def test_pair_mode_v2_to_t(self):
        g = figure_graph()
        c = RuleCompiler(g, PARAMS, {"t": figure3.PAPER_WAYPOINTS_T}, figure3.subnets())
        rules = c.compile_pair("h_v2", "h_t")
        v2 = rules["v2"]
        spray = [gr for gr in v2.groups.values() if gr.purpose == "spray"]
        self.assertEqual(len(spray), 1)
        self.assertEqual(set(spray[0].ports), set(g.fabric_ports("v2")))
        self.assertTrue(any(f.match.startswith(f"in_port={g.host('h_v2').port},")
                            for f in v2.flows))
        self.assertEqual(c.pair_switches("h_v2", "h_t"), {"v2", "t", "w3", "w4", "o1", "r6", "w5", "v3"})
        sim = Simulator(g, rules)
        self.assertEqual(sim.deliveries("h_v2", "h_t"), {("delivered", "h_t")})
        self.assertEqual(sim.deliveries("h_t", "h_v2"), {("delivered", "h_v2")})

    def test_full_mesh_all_pairs_deliver(self):
        for edges, subnets in ((figure3.edges, figure3.subnets()),
                               (random_regular_edges(30, 4, 3), None)):
            g = FabricGraph.from_edges(
                edges, host_ip=lambda sw, i, k: f"10.{i}.0.1")
            if subnets is None:
                subnets = {}
            for params in (PARAMS, SpraypointParams(p=2, h=2, l=1)):
                rules = RuleCompiler(g, params, subnets=subnets).compile_full_mesh()
                sim = Simulator(g, rules)
                hosts = [h.name for h in g.all_hosts()]
                for s in hosts:
                    for d in hosts:
                        if s != d:
                            self.assertEqual(sim.deliveries(s, d), {("delivered", d)}, (s, d))


if __name__ == "__main__":
    unittest.main()
