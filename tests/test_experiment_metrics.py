import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import networkx

from experiments.iperf import _parse_ping_output
from experiments.oversubscription import analyze, run
from run_experiment import normalize_run_config
from routing import (
    DEFAULT_ROUTING,
    ROUTING_METHODS,
    get_controller_class,
)
from routing import spraypoint as spraypoint_routing
from topologies import clos


class ExperimentMetricTests(unittest.TestCase):
    def test_ovs_controller_is_default_routing(self):
        self.assertEqual(DEFAULT_ROUTING, "OVS controller")
        self.assertEqual(
            get_controller_class(DEFAULT_ROUTING).__name__,
            "OVSController",
        )
        config = normalize_run_config(
            {
                "experiments": [
                    {"name": "Topology oversubscription ratio"}
                ]
            },
            "test config",
        )
        self.assertEqual(config["routing"]["name"], DEFAULT_ROUTING)

    def test_spraypoint_routing_config_and_controller(self):
        config = normalize_run_config(
            {
                "routing": {"name": "Spraypoint"},
                "experiments": [
                    {"name": "Topology oversubscription ratio"}
                ],
            },
            "test config",
        )

        self.assertIn("Spraypoint", ROUTING_METHODS)
        self.assertIsNone(get_controller_class("Spraypoint"))
        self.assertEqual(
            config["routing"]["parameters"],
            {"p": 2, "h": 1, "l": None, "hash_key": "spraypoint"},
        )

    def test_spraypoint_adapter_deploys_full_mesh(self):
        spraypoint_routing.load_protocol()
        import spraypoint

        installed = {
            "s1": SimpleNamespace(flows=[1, 2], groups={1: object()}),
            "s2": SimpleNamespace(flows=[3], groups={}),
        }
        with patch.object(spraypoint, "SpraypointDeployer") as deployer_class:
            deployer_class.return_value.deploy_full_mesh.return_value = installed
            result = spraypoint_routing.configure(
                object(),
                {"p": 2, "h": 1, "l": None, "hash_key": "test"},
            )

        deployer_class.assert_called_once()
        deployer_class.return_value.deploy_full_mesh.assert_called_once_with()
        self.assertEqual(result["deployment_mode"], "full_mesh")
        self.assertEqual(result["switch_count"], 2)
        self.assertEqual(result["flow_count"], 3)
        self.assertEqual(result["group_count"], 1)

    def test_spraypoint_compiles_routes_for_clos_fat_tree(self):
        deployer, params_class = spraypoint_routing.load_protocol()
        from spraypoint import FabricGraph, HostAttachment, RuleCompiler

        topology = clos.build_topology(k=4)
        fabric = FabricGraph()
        switch_nodes = {
            node
            for node, attributes in topology.nodes(data=True)
            if attributes.get("type") == "switch"
        }
        for switch in switch_nodes:
            fabric.add_switch(str(switch))

        next_ports = {str(switch): 1 for switch in switch_nodes}
        host_index = 0
        for source, target in topology.edges():
            source_is_switch = source in switch_nodes
            target_is_switch = target in switch_nodes
            if source_is_switch and target_is_switch:
                source_name, target_name = str(source), str(target)
                fabric.add_fabric_link(
                    source_name,
                    next_ports[source_name],
                    target_name,
                    next_ports[target_name],
                )
                next_ports[source_name] += 1
                next_ports[target_name] += 1
            else:
                switch, host = (
                    (source, target)
                    if source_is_switch
                    else (target, source)
                )
                switch_name = str(switch)
                host_index += 1
                fabric.add_host(
                    HostAttachment(
                        name=f"h{host_index}",
                        ip=f"10.0.0.{host_index}",
                        mac=None,
                        switch=switch_name,
                        port=next_ports[switch_name],
                    )
                )
                next_ports[switch_name] += 1

        compiler = RuleCompiler(fabric, params_class(p=2, h=1, l=None))
        installed = compiler.compile_full_mesh()

        self.assertEqual(set(installed), set(fabric.switches))
        self.assertTrue(any(rules.groups for rules in installed.values()))

    def make_topology(self):
        topology = networkx.Graph()
        topology.add_node("edge", type="switch", layer="edge")
        topology.add_node("aggregation", type="switch", layer="aggregation")
        topology.add_node("core", type="switch", layer="core")
        topology.add_node("host", type="host", layer="leaf")
        topology.add_edge("edge", "host", type="edge_leaf", capacity=10)
        topology.add_edge(
            "edge",
            "aggregation",
            type="aggregation_edge",
            capacity=2.5,
        )
        topology.add_edge(
            "aggregation",
            "core",
            type="core_aggregation",
            capacity=0.625,
        )
        return topology

    def test_oversubscription_ratio_sums_link_capacities_per_switch(self):
        result = analyze(self.make_topology())

        self.assertEqual(result["maximum_oversubscription_ratio"], 4)
        self.assertEqual(result["switch_count"], 2)
        self.assertEqual(
            [switch["oversubscription_ratio"] for switch in result["switches"]],
            [4, 4],
        )

    def test_oversubscription_run_writes_csv(self):
        with tempfile.TemporaryDirectory() as output_dir:
            result = run(self.make_topology(), output_dir)

            csv_path = Path(output_dir) / result["csv_file"]
            self.assertTrue(csv_path.is_file())
            self.assertEqual(len(csv_path.read_text().splitlines()), 3)

    def test_ping_summary_includes_packet_loss_and_latency(self):
        result = _parse_ping_output(
            "10 packets transmitted, 9 received, 10% packet loss, time 9000ms\n"
            "rtt min/avg/max/mdev = 0.100/0.200/0.300/0.050 ms\n"
        )

        self.assertEqual(result["packets_transmitted"], 10)
        self.assertEqual(result["packets_received"], 9)
        self.assertEqual(result["packet_loss_percent"], 10)
        self.assertEqual(result["avg_rtt_ms"], 0.2)


if __name__ == "__main__":
    unittest.main()
