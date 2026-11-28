import csv
import tempfile
import unittest
from pathlib import Path

import fnss

import failures
from experiments.iperf import source_ports
from run_experiment import normalize_run_config
from topologies import clos


def switches(topology):
    return {n for n, a in topology.nodes(data=True) if a["type"] == "switch"}


def flow(flow_id, source, destination, rate, failed=False, connect_failed=False):
    return {
        "flow_id": flow_id,
        "source_host": source,
        "destination_host": destination,
        "offered_mbps": 1.0,
        "server_received_rate": rate,
        "flow_failed": failed,
        "connect_failed": connect_failed,
    }


def run_config(topology, routing, failure, experiments=None):
    return normalize_run_config(
        {
            "topology": topology,
            "routing": {"name": routing},
            "experiments": experiments
            or [{"name": "UDP per-flow probe", "concurrent": True}],
            "failure": failure,
        },
        "test",
    )


class ChooseTests(unittest.TestCase):
    def setUp(self):
        self.topology = clos.build_topology(k=4)

    def test_same_seed_same_failure(self):
        for mode in failures.MODES:
            self.assertEqual(
                failures.choose(self.topology, mode, 2, seed=5),
                failures.choose(self.topology, mode, 2, seed=5),
            )

    def test_seed_changes_failure(self):
        picks = {tuple(failures.choose(self.topology, "link", 1, seed)[1]) for seed in range(10)}
        self.assertGreater(len(picks), 1)

    def test_node_mode_fails_every_link_of_the_switch(self):
        nodes, links = failures.choose(self.topology, "node", 1, seed=3)
        (node,) = nodes
        self.assertIn(node, switches(self.topology))
        self.assertEqual(len(links), self.topology.degree(node))
        self.assertTrue(all(node in link for link in links))

    def test_link_mode_fails_only_switch_links(self):
        nodes, links = failures.choose(self.topology, "link", 3, seed=3)
        self.assertEqual(nodes, [])
        self.assertEqual(len(links), 3)
        fabric = switches(self.topology)
        self.assertTrue(all(u in fabric and v in fabric for u, v in links))

    def test_count_larger_than_candidates_is_rejected(self):
        with self.assertRaises(ValueError):
            failures.choose(self.topology, "node", 1000, seed=1)


class TopologyTests(unittest.TestCase):
    def setUp(self):
        self.topology = clos.build_topology(k=4)

    def test_mininet_names_match_fnss(self):
        names = failures.mininet_names(self.topology)
        mininet_links = {
            frozenset(link)
            for link in fnss.to_mininet(self.topology, relabel_nodes=True).links()
        }
        for u, v in self.topology.edges():
            self.assertIn(frozenset((names[u], names[v])), mininet_links)

    def test_surviving_removes_failed_parts_and_lost_hosts(self):
        nodes, links = failures.choose(self.topology, "node", 1, seed=1)
        lost = failures.lost_hosts(self.topology, nodes)
        remaining = failures.surviving(self.topology, nodes, links)
        self.assertFalse(set(nodes) & set(remaining))
        self.assertFalse(lost & set(remaining))
        self.assertFalse(any(remaining.has_edge(u, v) for u, v in links))


class CompareTests(unittest.TestCase):
    def test_flow_classes(self):
        intact = flow(1, "h1", "h2", 1.0)
        self.assertEqual(failures.flow_class(intact, flow(1, "h1", "h2", 1.0), set()), "unaffected")
        self.assertEqual(failures.flow_class(intact, flow(1, "h1", "h2", 0.5), set()), "degraded")
        self.assertEqual(failures.flow_class(intact, flow(1, "h1", "h2", 0.0, True), set()), "fabric_dead")
        self.assertEqual(
            failures.flow_class(intact, flow(1, "h1", "h2", 0.0, True, connect_failed=True), set()),
            "connect_failed",
        )
        self.assertEqual(failures.flow_class(intact, flow(1, "h1", "h2", 0.0, True), {"h2"}), "endpoint_lost")
        dead = flow(1, "h1", "h2", 0.0, True)
        self.assertEqual(failures.flow_class(dead, dead, set()), "failed_intact")

    def test_source_ports_depend_only_on_seed(self):
        ports = source_ports(range(1, 101), seed=7)
        self.assertEqual(ports, source_ports(range(1, 101), seed=7))
        self.assertNotEqual(ports, source_ports(range(1, 101), seed=8))
        self.assertEqual(len(set(ports.values())), 100)
        self.assertTrue(all(port < 32768 for port in ports.values()))

    def test_compare_flows_writes_rows_and_counts(self):
        phases = {
            "intact": [flow(1, "h1", "h2", 1.0), flow(2, "h3", "h4", 1.0)],
            "stale": [flow(1, "h1", "h2", 0.0, True), flow(2, "h3", "h4", 1.0)],
            "rerouted": [flow(1, "h1", "h2", 1.0), flow(2, "h3", "h4", 1.0)],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flows.csv"
            counts = failures.compare_flows(phases, set(), path)
            rows = list(csv.DictReader(path.open()))
        self.assertEqual(counts, {"stale": {"fabric_dead": 1, "unaffected": 1}, "rerouted": {"unaffected": 2}})
        self.assertEqual([row["stale_class"] for row in rows], ["fabric_dead", "unaffected"])


class ConfigTests(unittest.TestCase):
    def test_valid_failure_is_normalized(self):
        config = run_config({"name": "Clos fat-tree"}, "ECMP shortest path", {"mode": "node"})
        self.assertEqual(config["failure"], {"mode": "node", "count": 1, "seed": 1})

    def test_no_failure_block(self):
        config = normalize_run_config({"topology": {"name": "Clos fat-tree"}}, "test")
        self.assertIsNone(config["failure"])

    def test_unsupported_pair_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            run_config({"name": "Azure Clos"}, "ECMP shortest path", {"mode": "link"})
        with self.assertRaises(NotImplementedError):
            run_config({"name": "Clos fat-tree"}, "Spraypoint", {"mode": "link"})

    def test_invalid_values_are_rejected(self):
        for failure in ({"mode": "rack"}, {"mode": "link", "count": 0}, {"mode": "link", "seed": "x"}):
            with self.assertRaises(ValueError):
                run_config({"name": "Clos fat-tree"}, "ECMP shortest path", failure)

    def test_sequential_probe_is_rejected(self):
        with self.assertRaises(ValueError):
            run_config(
                {"name": "Clos fat-tree"},
                "ECMP shortest path",
                {"mode": "link"},
                [{"name": "UDP per-flow probe", "concurrent": False}],
            )


if __name__ == "__main__":
    unittest.main()
