"""Offline tests for the RNG topology and its Spraypoint deployment plan.

Run from the repo root:  python3 -m unittest tests/test_rng_topology.py -v
"""

import unittest

import networkx

from routing import spraypoint as spraypoint_routing
from run_experiment import TOPOLOGIES, normalize_run_config
from topologies import rng


def switch_subgraph(topology):
    switches = [
        node for node, attributes in topology.nodes(data=True)
        if attributes["type"] == "switch"
    ]
    return topology.subgraph(switches)


class RngTopologyTests(unittest.TestCase):
    def test_default_topology_is_connected_regular_graph_with_hosts(self):
        topology = rng.build_topology()
        n, d = rng.DEFAULT_PARAMETERS["n"], rng.DEFAULT_PARAMETERS["d"]
        switches = switch_subgraph(topology)

        self.assertEqual(switches.number_of_nodes(), n)
        self.assertEqual({degree for _, degree in switches.degree()}, {d})
        self.assertTrue(networkx.is_connected(switches))
        hosts = [
            node for node, attributes in topology.nodes(data=True)
            if attributes["type"] == "host"
        ]
        self.assertEqual(len(hosts), n)
        for host in hosts:
            (switch,) = topology.neighbors(host)
            self.assertEqual(topology.nodes[switch]["type"], "switch")

    def test_capacities_units_and_link_types(self):
        topology = rng.build_topology(
            n=6, d=3, hosts_per_switch=2, host_link_mbps=10, fabric_link_mbps=40
        )
        self.assertEqual(topology.graph["capacity_unit"], "Mbps")
        self.assertEqual(topology.graph["delay_unit"], "ms")
        self.assertEqual(topology.graph["buffer_unit"], "packets")
        capacities = {
            attributes["type"]: attributes["capacity"]
            for _, _, attributes in topology.edges(data=True)
        }
        self.assertEqual(capacities, {"fabric": 40, "host_link": 10})
        self.assertEqual(topology.number_of_edges(), 6 * 3 // 2 + 6 * 2)

    def test_wiring_is_reproducible_per_seed(self):
        def fabric_edges(seed):
            return sorted(switch_subgraph(
                rng.build_topology(n=16, d=4, seed=seed)
            ).edges())

        self.assertEqual(fabric_edges(5), fabric_edges(5))
        self.assertNotEqual(fabric_edges(5), fabric_edges(6))

    def test_invalid_parameters_rejected(self):
        for parameters in (
            {"n": 1},
            {"d": 0},
            {"n": 4, "d": 4},
            {"n": 5, "d": 3},
            {"hosts_per_switch": 0},
            {"fabric_link_mbps": 0},
            {"host_link_mbps": float("inf")},
            {"seed": True},
        ):
            with self.subTest(parameters=parameters):
                with self.assertRaises(ValueError):
                    rng.build_topology(**parameters)

    def test_runner_accepts_rng_config_and_validates_parameters(self):
        config = normalize_run_config(
            {
                "topology": {
                    "name": "RNG random graph",
                    "parameters": {"n": 10, "d": 4},
                },
                "routing": {"name": "Spraypoint"},
            },
            "test config",
        )
        self.assertIn("RNG random graph", TOPOLOGIES)
        self.assertEqual(
            config["topology"]["parameters"],
            {**rng.DEFAULT_PARAMETERS, "n": 10, "d": 4},
        )
        with self.assertRaisesRegex(ValueError, "topology.parameters: n . d"):
            normalize_run_config(
                {
                    "topology": {
                        "name": "RNG random graph",
                        "parameters": {"n": 9, "d": 3},
                    }
                },
                "test config",
            )
        with self.assertRaisesRegex(ValueError, "unknown field"):
            normalize_run_config(
                {
                    "topology": {
                        "name": "RNG random graph",
                        "parameters": {"k": 4},
                    }
                },
                "test config",
            )


class RngSpraypointTests(unittest.TestCase):
    """Compile Spraypoint for RNG graphs and walk every spray/ECMP branch."""

    def test_full_mesh_delivers_every_host_pair(self):
        _, params_class = spraypoint_routing.load_protocol()
        from spraypoint import RuleCompiler

        simulator_class = spraypoint_routing.load_simulator()
        cases = (
            ({"n": 8, "d": 3}, params_class(p=2, h=1, l=None)),
            ({"n": 16, "d": 4, "hosts_per_switch": 2},
             params_class(p=2, h=2, l=None)),
            ({"n": 30, "d": 4, "seed": 7}, params_class(p=2, h=1, l=2)),
        )
        for topology_parameters, params in cases:
            with self.subTest(topology=topology_parameters, params=params):
                topology = rng.build_topology(**topology_parameters)
                fabric = spraypoint_routing.fabric_from_topology(topology)
                rules = RuleCompiler(fabric, params).compile_full_mesh()

                self.assertEqual(set(rules), set(fabric.switches))
                simulator = simulator_class(fabric, rules)
                hosts = [host.name for host in fabric.all_hosts()]
                for source in hosts:
                    for destination in hosts:
                        if source != destination:
                            self.assertEqual(
                                simulator.deliveries(source, destination),
                                {("delivered", destination)},
                                (source, destination),
                            )

    def test_offline_fabric_matches_mininet_naming(self):
        topology = rng.build_topology(n=4, d=3, hosts_per_switch=2)
        fabric = spraypoint_routing.fabric_from_topology(topology)

        self.assertEqual(fabric.switches, ["s1", "s2", "s3", "s4"])
        self.assertEqual(fabric.host("h1").switch, "s1")
        self.assertEqual(fabric.host("h2").switch, "s1")
        self.assertEqual(fabric.host("h3").switch, "s2")
        self.assertEqual(fabric.host("h8").ip, "10.0.0.8")
        for switch in fabric.switches:
            self.assertEqual(fabric.degree(switch), 3)
            ports = fabric.fabric_ports(switch) + fabric.host_ports(switch)
            self.assertEqual(sorted(ports), list(range(1, 6)))


if __name__ == "__main__":
    unittest.main()
