import csv
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fnss
import networkx
from mininet.link import TCIntf

from experiments.iperf import (
    IperfResultError,
    _iperf_rates,
    _parse_ping_output,
    run_flows_concurrently,
)
from experiments.load_sweep import (
    _flows_for_matrix,
    _host_capacity,
    _leaf_hosts,
    _summary,
    _traffic_for_active_leaves,
    run as run_load_sweep,
)
from experiments.oversubscription import analyze, run
from run_experiment import normalize_run_config, to_mininet_topology
from routing import (
    DEFAULT_ROUTING,
    ROUTING_METHODS,
    get_controller_class,
)
from routing import spraypoint as spraypoint_routing
from topologies import azure_clos, clos
from traffic.matching import build_traffic as build_matching_traffic


class ExperimentMetricTests(unittest.TestCase):
    def test_ecmp_is_default_routing(self):
        self.assertEqual(DEFAULT_ROUTING, "ECMP shortest path")
        self.assertIsNone(get_controller_class(DEFAULT_ROUTING))
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

    def test_ecmp_clos_alias_is_removed(self):
        self.assertIn("ECMP shortest path", ROUTING_METHODS)
        self.assertNotIn("ECMP (Clos)", ROUTING_METHODS)
        self.assertNotIn("OVS controller", ROUTING_METHODS)
        for removed_name in ("ECMP (Clos)", "OVS controller"):
            with self.subTest(routing=removed_name):
                with self.assertRaisesRegex(
                    ValueError, "routing.name must be one of"
                ):
                    normalize_run_config(
                        {
                            "routing": {"name": removed_name},
                            "experiments": [
                                {"name": "Topology oversubscription ratio"}
                            ],
                        },
                        "test config",
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

    def test_mininet_links_keep_fnss_buffer_as_queue_size(self):
        topology = clos.build_topology(k=4)
        mininet_topology = to_mininet_topology(topology)

        links = mininet_topology.links(withInfo=True)
        self.assertEqual(len(links), topology.number_of_edges())
        for source, target, info in links:
            self.assertEqual(info.get("max_queue_size"), 50, (source, target))
            self.assertEqual(info["bw"], 10)
            self.assertIn("delay", info)

    def test_queue_size_skipped_for_byte_buffers(self):
        topology = clos.build_topology(k=2)
        fnss.set_buffer_sizes_constant(topology, 15000, "bytes")
        mininet_topology = to_mininet_topology(topology)

        for _, _, info in mininet_topology.links(withInfo=True):
            self.assertNotIn("max_queue_size", info)

    def test_tclink_applies_queue_size_as_netem_limit(self):
        commands, _ = TCIntf.delayCmds(
            parent=" root ", delay="1000.0us", max_queue_size=50
        )

        self.assertEqual(len(commands), 1)
        self.assertIn("netem delay 1000.0us limit 50", commands[0])

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

    def test_iperf_rates_rejects_missing_summaries(self):
        with self.assertRaisesRegex(IperfResultError, "missing rate summaries"):
            _iperf_rates({"end": {}})

    def test_iperf_rates_uses_server_receiver_counters(self):
        result = _iperf_rates(
            {
                "end": {
                    "sum": {"bits_per_second": 10_000_000},
                },
                "server_output_json": {
                    "intervals": [
                        {
                            "sum": {
                                "bytes": 300_000,
                                "seconds": 0.5,
                            },
                        },
                        {
                            "sum": {
                                "bytes": 300_000,
                                "seconds": 0.5,
                            },
                        },
                    ],
                    "end": {
                        "sum": {"bits_per_second": 0},
                    },
                },
            }
        )

        self.assertEqual(result, (4.8, 10.0))

    def test_iperf_rates_does_not_treat_sender_rate_as_received(self):
        with self.assertRaisesRegex(IperfResultError, "receiver sum"):
            _iperf_rates(
                {
                    "end": {
                        "sum": {"bits_per_second": 10_000_000},
                    },
                }
            )

    def test_concurrent_iperf_records_failed_flows_and_continues(self):
        class FakeProcess:
            def __init__(self, output, running=False, returncode=0):
                self.output = output
                self.running = running
                self.returncode = returncode

            def communicate(self, timeout=None):
                return self.output, None

            def poll(self):
                return None if self.running else self.returncode

            def send_signal(self, _signal):
                self.running = False

            def kill(self):
                self.running = False

        class FakeHost:
            def IP(self):
                return "10.0.0.2"

            def popen(self, command, **_kwargs):
                if command[:2] == ["iperf3", "-s"]:
                    return FakeProcess("server output", running=True)
                if command[0] == "iperf3":
                    return FakeProcess('{"end":{}}', returncode=1)
                return FakeProcess(
                    "1 packets transmitted, 1 received, 0% packet loss\n"
                    "rtt min/avg/max/mdev = 1/1/1/0 ms\n"
                )

        class FakeNetwork:
            def get(self, _name):
                return FakeHost()

        flows = {1: ("source", "destination", 1.0)}
        host_names = {"source": "h1", "destination": "h2"}
        idle_latency = {1: {"avg_rtt_ms": 1.0}}

        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8") as log_file:
            measured = run_flows_concurrently(
                FakeNetwork(),
                flows,
                host_names,
                5,
                log_file,
                idle_latency,
            )

            log_file.seek(0)
            log = log_file.read()

        self.assertEqual(measured[1][:2], (0.0, 0.0))
        self.assertIsNotNone(measured[1][4])
        summary = _summary(0.6, flows, measured)
        self.assertEqual(summary["failed_flow_count"], 1)
        self.assertEqual(summary["sent_mbps"], 0.0)
        self.assertEqual(summary["delivered_mbps"], 0.0)
        self.assertIsNone(summary["packet_loss_percent"])
        self.assertIn("source=source, destination=destination", log)
        self.assertIn("client_exit_code=1", log)
        self.assertIn('{"end":{}}', log)
        self.assertIn("server output", log)
        self.assertIn("measurement failure", log)

    def test_concurrent_iperf_timeout_is_counted_without_aborting(self):
        class FakeProcess:
            def __init__(
                self, output, running=False, returncode=0, timeout=False
            ):
                self.output = output
                self.running = running
                self.returncode = returncode
                self.timeout = timeout

            def communicate(self, timeout=None):
                if self.timeout:
                    self.timeout = False
                    raise subprocess.TimeoutExpired(
                        ["iperf3"], timeout, output=""
                    )
                return self.output, None

            def poll(self):
                return None if self.running else self.returncode

            def send_signal(self, _signal):
                self.running = False

            def kill(self):
                self.running = False
                self.returncode = -9

        class FakeHost:
            def IP(self):
                return "10.0.0.2"

            def popen(self, command, **_kwargs):
                if command[:2] == ["iperf3", "-s"]:
                    return FakeProcess("server output", running=True)
                if command[0] == "iperf3":
                    return FakeProcess("", running=True, timeout=True)
                return FakeProcess(
                    "1 packets transmitted, 1 received, 0% packet loss\n"
                    "rtt min/avg/max/mdev = 1/1/1/0 ms\n"
                )

        class FakeNetwork:
            def get(self, _name):
                return FakeHost()

        flows = {1: ("source", "destination", 1.0)}
        host_names = {"source": "h1", "destination": "h2"}
        idle_latency = {1: {"avg_rtt_ms": 1.0}}

        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8") as log_file:
            measured = run_flows_concurrently(
                FakeNetwork(),
                flows,
                host_names,
                5,
                log_file,
                idle_latency,
            )
            log_file.seek(0)
            log = log_file.read()

        summary = _summary(0.6, flows, measured)
        self.assertEqual(summary["failed_flow_count"], 1)
        self.assertEqual(summary["sent_mbps"], 0.0)
        self.assertEqual(summary["delivered_mbps"], 0.0)
        self.assertIn("iperf3 client timed out after 20 seconds", log)
        self.assertIn("measurement failure", log)

    def test_sweep_loss_excludes_failed_flows_without_sent_counters(self):
        flows = {
            1: ("source", "destination", 1.0),
            2: ("other_source", "other_destination", 1.0),
        }
        idle = {"avg_rtt_ms": 1.0}
        measured = {
            1: (0.0, 0.0, idle, idle, "missing counters"),
            2: (8.0, 10.0, idle, idle, None),
        }

        summary = _summary(0.6, flows, measured)

        self.assertEqual(summary["failed_flow_count"], 1)
        self.assertEqual(summary["sent_mbps"], 10.0)
        self.assertEqual(summary["delivered_mbps"], 8.0)
        self.assertAlmostEqual(summary["packet_loss_percent"], 20.0)

    def test_load_sweep_selects_leaf_fraction_and_rebuilds_traffic(self):
        topology = azure_clos.build_topology(
            leaves=4,
            spines=2,
            hosts_per_leaf=2,
        )
        leaves, hosts_by_leaf = _leaf_hosts(topology)

        active_leaves, traffic_matrix = _traffic_for_active_leaves(
            topology,
            leaves,
            hosts_by_leaf,
            2,
            11,
            build_matching_traffic,
            {"demand_mbps": 1.0, "seed": 1},
        )

        endpoints = {
            node
            for source, destination in traffic_matrix.flows()
            for node in (source, destination)
        }
        host_names = {
            host: f"h{index}"
            for index, host in enumerate(
                sorted(
                    node
                    for node, attributes in topology.nodes(data=True)
                    if attributes.get("type") == "host"
                ),
                start=1,
            )
        }
        flows = _flows_for_matrix(
            traffic_matrix,
            host_names,
            {leaf: hosts_by_leaf[leaf] for leaf in active_leaves},
            _host_capacity(topology),
        )
        active_hosts = {
            host for leaf in active_leaves for host in hosts_by_leaf[leaf]
        }
        host_leaf = {
            host: leaf
            for leaf in active_leaves
            for host in hosts_by_leaf[leaf]
        }
        host_endpoints = {
            node
            for source, destination, _ in flows.values()
            for node in (source, destination)
        }
        self.assertEqual(len(active_leaves), 2)
        self.assertTrue(endpoints)
        self.assertLessEqual(endpoints, set(active_leaves))
        self.assertLessEqual(host_endpoints, active_hosts)
        self.assertTrue(
            all(
                host_leaf[source] != host_leaf[destination]
                for source, destination, _ in flows.values()
            )
        )

    def test_paper_style_ratio_uses_worst_flow(self):
        flows = {
            1: ("source", "destination", 10.0),
            2: ("other_source", "other_destination", 10.0),
        }
        idle = {"avg_rtt_ms": 1.0}
        measured = {
            1: (5.0, 10.0, idle, idle, None),
            2: (10.0, 10.0, idle, idle, None),
        }

        summary = _summary(0.5, flows, measured, active_leaf_count=2)

        self.assertEqual(summary["minimum_flow_rate_fraction"], 0.5)
        self.assertEqual(summary["oversubscription_ratio"], 2.0)
        self.assertAlmostEqual(
            summary["aggregate_offered_to_delivered_ratio"], 20 / 15
        )

    def test_paper_style_ratio_aggregates_host_flows_per_leaf_pair(self):
        flows = {
            1: ("source1", "destination1", 10.0),
            2: ("source2", "destination2", 10.0),
            3: ("source3", "destination3", 10.0),
        }
        idle = {"avg_rtt_ms": 1.0}
        measured = {
            1: (0.0, 10.0, idle, idle, None),
            2: (20.0, 10.0, idle, idle, None),
            3: (5.0, 10.0, idle, idle, None),
        }
        host_leaf = {
            "source1": "leaf1",
            "source2": "leaf1",
            "source3": "leaf2",
            "destination1": "leaf3",
            "destination2": "leaf3",
            "destination3": "leaf4",
        }

        summary = _summary(1.0, flows, measured, host_leaf=host_leaf)

        self.assertEqual(summary["minimum_flow_rate_fraction"], 0.5)
        self.assertEqual(summary["oversubscription_ratio"], 2.0)
        self.assertEqual(
            summary["aggregate_offered_to_delivered_ratio"], 30 / 25
        )

    def test_load_sweep_config_defaults_and_validation(self):
        config = normalize_run_config(
            {"experiments": [{"name": "Load sweep"}]},
            "test config",
        )
        parameters = config["experiments"][0]["parameters"]
        self.assertEqual(
            parameters["active_fractions"],
            [index / 10 for index in range(1, 11)],
        )
        self.assertEqual(parameters["matrix_repetitions"], 1)

        with self.assertRaisesRegex(ValueError, "active_fractions"):
            normalize_run_config(
                {
                    "experiments": [
                        {
                            "name": "Load sweep",
                            "active_fractions": [0],
                        }
                    ]
                },
                "test config",
            )

        with self.assertRaisesRegex(ValueError, "matrix_repetitions"):
            normalize_run_config(
                {
                    "experiments": [
                        {
                            "name": "Load sweep",
                            "matrix_repetitions": 0,
                        }
                    ]
                },
                "test config",
            )

    def test_azure_leaf_oversubscription_sets_uplink_capacity(self):
        topology = azure_clos.build_topology(
            leaves=2,
            spines=2,
            hosts_per_leaf=2,
            host_link_mbps=10,
            leaf_oversubscription=3,
        )
        host_capacity = 2 * 10
        uplink_capacity = sum(
            attributes["capacity"]
            for source, target, attributes in topology.edges(data=True)
            if attributes["type"] == "aggregation_edge"
            and "leaf1" in (source, target)
        )

        self.assertAlmostEqual(host_capacity / uplink_capacity, 3.0)

    def test_load_sweep_runs_leaf_level_matching_matrices(self):
        topology = azure_clos.build_topology(
            leaves=4,
            spines=2,
            hosts_per_leaf=2,
        )
        host_names = {
            host: f"h{index}"
            for index, host in enumerate(
                sorted(
                    node
                    for node, attributes in topology.nodes(data=True)
                    if attributes.get("type") == "host"
                ),
                start=1,
            )
        }

        def idle_measurements(_net, flows, _host_names):
            return {flow_id: {"avg_rtt_ms": 1.0} for flow_id in flows}

        def throughput_measurements(
            _net, flows, _host_names, _duration, _log_file, idle_latency
        ):
            return {
                flow_id: (
                    demand / 2,
                    demand,
                    idle_latency[flow_id],
                    {"avg_rtt_ms": 2.0},
                    None,
                )
                for flow_id, (_, _, demand) in flows.items()
            }

        with tempfile.TemporaryDirectory() as output_dir:
            with patch(
                "experiments.load_sweep.iperf.measure_idle",
                side_effect=idle_measurements,
            ), patch(
                "experiments.load_sweep.iperf.run_flows_concurrently",
                side_effect=throughput_measurements,
            ):
                result = run_load_sweep(
                    object(),
                    None,
                    host_names,
                    topology,
                    duration_seconds=1,
                    active_fractions=[0.5, 1.0],
                    matrix_repetitions=1,
                    traffic_name="Matching host pairs",
                    traffic_builder=build_matching_traffic,
                    traffic_parameters={"demand_mbps": 1.0, "seed": 1},
                    output_dir=output_dir,
                )

            with (Path(output_dir) / result["csv_file"]).open(
                encoding="utf-8"
            ) as csv_file:
                rows = list(csv.DictReader(csv_file))

        self.assertEqual(result["active_fractions"], [0.5, 1.0])
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [float(row["oversubscription_ratio"]) for row in rows],
            [2.0, 2.0],
        )
        self.assertEqual(
            [
                float(row["aggregate_offered_to_delivered_ratio"])
                for row in rows
            ],
            [
                float(row["offered_mbps"]) / float(row["delivered_mbps"])
                for row in rows
            ],
        )
        self.assertEqual([int(row["active_leaf_count"]) for row in rows], [2, 4])


if __name__ == "__main__":
    unittest.main()
