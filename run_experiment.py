import argparse
import json
import math
import os
import platform
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping

import fnss
import networkx
import mininet
from mininet.link import TCLink
from mininet.net import Mininet
from mininet.node import OVSController

from experiments import iperf
from topologies import clos, jupiter, rail_optimized
from traffic import uniform_random


ROOT = Path(__file__).resolve().parent
RESULTS_ROOT = ROOT / "test-results"
TOPOLOGIES = {
    "Clos fat-tree": {
        "builder": clos.build_topology,
        "parameters": {"k": 4},
    },
    "Rail-optimized": {
        "builder": rail_optimized.build_topology,
        "parameters": dict(rail_optimized.DEFAULT_PARAMETERS),
        "routing": rail_optimized.install_routing,
        "validate": rail_optimized.validate_parameters,
    },
    "Jupiter direct-connect": {
        "builder": jupiter.build_topology,
        "parameters": dict(jupiter.DEFAULT_PARAMETERS),
        "routing": jupiter.install_routing,
        "validate": jupiter.validate_parameters,
    },
}
TRAFFIC_MODELS = {"Uniform random host pairs": uniform_random.build_traffic}
EXPERIMENTS = {
    "UDP iperf throughput": {
        "runner": iperf.run,
        "parameters": {"duration_seconds": 5},
    }
}
DEFAULT_TOPOLOGY = next(iter(TOPOLOGIES))
DEFAULT_TRAFFIC = next(iter(TRAFFIC_MODELS))
DEFAULT_EXPERIMENT = next(iter(EXPERIMENTS))
DEFAULT_TRAFFIC_PARAMETERS = {
    "flow_count": 8,
    "demand_mbps": 1.0,
    "seed": 1,
}


def choose_one(title, choices):
    options = list(choices)
    while True:
        print(f"\n{title}")
        for index, name in enumerate(options, start=1):
            print(f"  {index}. {name}")
        answer = input("Select an option: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        print("Enter one of the listed numbers.")


def choose_many(title, choices):
    options = list(choices)
    while True:
        print(f"\n{title}")
        for index, name in enumerate(options, start=1):
            print(f"  {index}. {name}")
        answer = input(
            "Select one or more options (comma-separated, or 'all'): "
        ).strip()
        if answer.lower() == "all":
            return options
        try:
            indexes = [int(value.strip()) for value in answer.split(",")]
        except ValueError:
            indexes = []
        if (
            indexes
            and len(indexes) == len(set(indexes))
            and all(1 <= index <= len(options) for index in indexes)
        ):
            return [options[index - 1] for index in indexes]
        print("Enter listed numbers separated by commas, or 'all'.")


def prompt_positive_int(prompt, default):
    while True:
        answer = input(f"{prompt} [{default}]: ").strip()
        if not answer:
            return default
        try:
            value = int(answer)
        except ValueError:
            value = 0
        if value > 0:
            return value
        print("Enter a positive integer.")


def prompt_positive_float(prompt, default):
    while True:
        answer = input(f"{prompt} [{default}]: ").strip()
        if not answer:
            return default
        try:
            value = float(answer)
        except ValueError:
            value = 0
        if value > 0 and math.isfinite(value):
            return value
        print("Enter a positive finite number.")


def prompt_integer(prompt, default):
    while True:
        answer = input(f"{prompt} [{default}]: ").strip()
        if not answer:
            return default
        try:
            return int(answer)
        except ValueError:
            print("Enter an integer.")


def require_mapping(value, location, allowed_keys):
    if not isinstance(value, Mapping):
        raise ValueError(f"{location} must be a JSON object")
    unknown_keys = set(value) - set(allowed_keys)
    if unknown_keys:
        raise ValueError(
            f"{location} contains unknown field(s): "
            + ", ".join(sorted(unknown_keys))
        )
    return value


def normalize_run_config(config, location):
    config = require_mapping(
        config,
        location,
        {"name", "topology", "traffic", "experiments"},
    )
    name = config.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise ValueError(f"{location}.name must be a non-empty string")

    topology_config = config.get("topology", {})
    topology_config = require_mapping(
        topology_config,
        f"{location}.topology",
        {"name", "parameters"},
    )
    topology_name = topology_config.get("name", DEFAULT_TOPOLOGY)
    if not isinstance(topology_name, str) or topology_name not in TOPOLOGIES:
        raise ValueError(
            f"{location}.topology.name must be one of: "
            + ", ".join(TOPOLOGIES)
        )
    topology_parameters = topology_config.get("parameters", {})
    topology_parameters = require_mapping(
        topology_parameters,
        f"{location}.topology.parameters",
        TOPOLOGIES[topology_name]["parameters"],
    )
    topology_parameters = {
        **TOPOLOGIES[topology_name]["parameters"],
        **topology_parameters,
    }
    validate_topology = TOPOLOGIES[topology_name].get("validate")
    if validate_topology:
        try:
            validate_topology(**topology_parameters)
        except ValueError as error:
            raise ValueError(f"{location}.topology.parameters: {error}") from error
    else:
        k = topology_parameters.get("k")
        if (
            not isinstance(k, int)
            or isinstance(k, bool)
            or k < 2
            or k % 2
        ):
            raise ValueError(
                f"{location}.topology.parameters.k must be an even integer >= 2"
            )

    traffic_config = require_mapping(
        config.get("traffic", {}),
        f"{location}.traffic",
        {"name", *DEFAULT_TRAFFIC_PARAMETERS},
    )
    traffic_name = traffic_config.get("name", DEFAULT_TRAFFIC)
    if not isinstance(traffic_name, str) or traffic_name not in TRAFFIC_MODELS:
        raise ValueError(
            f"{location}.traffic.name must be one of: "
            + ", ".join(TRAFFIC_MODELS)
        )
    traffic_parameters = {
        **DEFAULT_TRAFFIC_PARAMETERS,
        **{
            key: value
            for key, value in traffic_config.items()
            if key != "name"
        },
    }
    flow_count = traffic_parameters["flow_count"]
    if (
        not isinstance(flow_count, int)
        or isinstance(flow_count, bool)
        or flow_count < 1
    ):
        raise ValueError(f"{location}.traffic.flow_count must be a positive integer")
    demand_mbps = traffic_parameters["demand_mbps"]
    if (
        not isinstance(demand_mbps, (int, float))
        or isinstance(demand_mbps, bool)
        or not math.isfinite(demand_mbps)
        or demand_mbps <= 0
    ):
        raise ValueError(
            f"{location}.traffic.demand_mbps must be a positive finite number"
        )
    seed = traffic_parameters["seed"]
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError(f"{location}.traffic.seed must be an integer")

    raw_experiments = config.get("experiments", [{"name": DEFAULT_EXPERIMENT}])
    if not isinstance(raw_experiments, list) or not raw_experiments:
        raise ValueError(f"{location}.experiments must be a non-empty list")
    experiments = []
    for index, raw_experiment in enumerate(raw_experiments, start=1):
        experiment_location = f"{location}.experiments[{index}]"
        experiment_config = require_mapping(
            raw_experiment,
            experiment_location,
            {"name", "duration_seconds"},
        )
        experiment_name = experiment_config.get("name", DEFAULT_EXPERIMENT)
        if (
            not isinstance(experiment_name, str)
            or experiment_name not in EXPERIMENTS
        ):
            raise ValueError(
                f"{experiment_location}.name must be one of: "
                + ", ".join(EXPERIMENTS)
            )
        parameters = {
            **EXPERIMENTS[experiment_name]["parameters"],
            **{
                key: value
                for key, value in experiment_config.items()
                if key != "name"
            },
        }
        duration_seconds = parameters["duration_seconds"]
        if (
            not isinstance(duration_seconds, int)
            or isinstance(duration_seconds, bool)
            or duration_seconds < 1
        ):
            raise ValueError(
                f"{experiment_location}.duration_seconds must be a positive integer"
            )
        experiments.append(
            {"name": experiment_name, "parameters": parameters}
        )

    return {
        "name": name.strip() if name else None,
        "topology": {"name": topology_name, "parameters": topology_parameters},
        "traffic": {"name": traffic_name, **traffic_parameters},
        "experiments": experiments,
    }


def load_config(path):
    try:
        with Path(path).open(encoding="utf-8") as config_file:
            config = json.load(config_file)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Invalid JSON in {path} at line {error.lineno}, "
            f"column {error.colno}: {error.msg}"
        ) from error
    config = require_mapping(config, "config", {"runs"})
    runs = config.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError("config.runs must be a non-empty list")
    return [
        normalize_run_config(run, f"config.runs[{index}]")
        for index, run in enumerate(runs, start=1)
    ]


def create_run_directory():
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = RESULTS_ROOT / f"run-{run_id}"
    run_dir.mkdir()
    return run_id, run_dir


def write_summary(path, summary):
    path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def topology_snapshot(topology):
    return {
        "node_count": topology.number_of_nodes(),
        "edge_count": topology.number_of_edges(),
        "nodes": [
            {"id": str(node), "attributes": attributes}
            for node, attributes in topology.nodes(data=True)
        ],
        "edges": [
            {
                "source": str(source),
                "target": str(destination),
                "attributes": attributes,
            }
            for source, destination, attributes in topology.edges(data=True)
        ],
    }


def interactive_config():
    print("Network experiment runner")
    topology_name = choose_one("Choose a topology", TOPOLOGIES)
    traffic_name = choose_one("Choose a traffic model", TRAFFIC_MODELS)
    experiment_names = choose_many("Choose experiments to run", EXPERIMENTS)

    flow_count = prompt_positive_int("Number of traffic flows", 8)
    demand_mbps = prompt_positive_float("Demand per flow (Mbps)", 1.0)
    seed = prompt_integer("Random seed", 1)
    duration_seconds = prompt_positive_int("UDP iperf duration per flow (seconds)", 5)
    return normalize_run_config(
        {
            "topology": {"name": topology_name},
            "traffic": {
                "name": traffic_name,
                "flow_count": flow_count,
                "demand_mbps": demand_mbps,
                "seed": seed,
            },
            "experiments": [
                {"name": name, "duration_seconds": duration_seconds}
                for name in experiment_names
            ],
        },
        "interactive run",
    )


def run_experiment(config, config_source=None):
    topology_config = config["topology"]
    traffic_config = config["traffic"]
    topology = TOPOLOGIES[topology_config["name"]]["builder"](
        **topology_config["parameters"]
    )
    traffic_matrix = TRAFFIC_MODELS[traffic_config["name"]](
        topology,
        flow_count=traffic_config["flow_count"],
        demand_mbps=traffic_config["demand_mbps"],
        seed=traffic_config["seed"],
    )
    run_id, run_dir = create_run_directory()
    traffic_path = run_dir / "traffic-matrix.xml"
    fnss.write_traffic_matrix(traffic_matrix, str(traffic_path))

    summary = {
        "run_id": run_id,
        "status": "in_progress",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "machine": socket.gethostname(),
        "working_directory": str(ROOT),
        "config_source": str(config_source) if config_source else "interactive",
        "run_name": config["name"],
        "software": {
            "python": sys.version,
            "platform": platform.platform(),
            "fnss": getattr(fnss, "__version__", "unknown"),
            "networkx": networkx.__version__,
            "mininet_module_path": mininet.__file__,
        },
        "topology": {
            "name": topology_config["name"],
            "parameters": topology_config["parameters"],
            **topology_snapshot(topology),
        },
        "traffic": {
            "name": traffic_config["name"],
            "flow_count": len(traffic_matrix),
            "demand_mbps": traffic_config["demand_mbps"],
            "seed": traffic_config["seed"],
            "volume_unit": traffic_matrix.attrib["volume_unit"],
            "file": traffic_path.name,
            "flows": [
                {
                    "source_node": str(source),
                    "destination_node": str(destination),
                    "demand_mbps": volume,
                }
                for (source, destination), volume in traffic_matrix.flows().items()
            ],
        },
        "experiments": [],
    }
    summary_path = run_dir / "results.json"
    write_summary(summary_path, summary)

    net = None
    network_needs_cleanup = False
    experiments_completed = False
    network_stopped = False
    try:
        if os.geteuid() != 0:
            raise PermissionError(
                "Mininet requires root privileges. Run this script with the "
                "environment's Python executable via sudo."
            )

        mininet_topology = fnss.to_mininet(topology, relabel_nodes=True)
        host_nodes = sorted(
            node for node, attributes in topology.nodes(data=True)
            if attributes.get("type") == "host"
        )
        host_names = {
            node: f"h{index}"
            for index, node in enumerate(host_nodes, start=1)
        }
        # Topologies with their own routing install static flows instead of
        # relying on Mininet's learning-switch controller.
        install_routing = TOPOLOGIES[topology_config["name"]].get("routing")
        net = Mininet(
            topo=mininet_topology,
            link=TCLink,
            controller=None if install_routing else OVSController,
            build=False,
        )
        network_needs_cleanup = True
        net.build()
        net.start()
        if install_routing:
            install_routing(net, topology)
        for experiment_config in config["experiments"]:
            experiment_name = experiment_config["name"]
            experiment_result = EXPERIMENTS[experiment_name]["runner"](
                net,
                traffic_matrix,
                host_names,
                experiment_config["parameters"]["duration_seconds"],
                run_dir,
            )
            summary["experiments"].append(
                {
                    "name": experiment_name,
                    "parameters": experiment_config["parameters"],
                    **experiment_result,
                }
            )
            write_summary(summary_path, summary)
        experiments_completed = True
    finally:
        try:
            if network_needs_cleanup:
                net.stop()
                network_stopped = True
        finally:
            active_exception = sys.exc_info()[1]
            summary["status"] = (
                "completed"
                if experiments_completed and network_stopped
                else "failed"
            )
            if active_exception is not None:
                summary["error"] = {
                    "type": type(active_exception).__name__,
                    "message": str(active_exception),
                }
            summary["completed_at"] = datetime.now(timezone.utc).isoformat()
            write_summary(summary_path, summary)

    if summary["status"] == "completed":
        print(f"\nResults saved in {run_dir}")
    return run_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run Mininet network experiments.")
    parser.add_argument(
        "--config",
        type=Path,
        help="JSON file describing one or more runs; omit for interactive menus.",
    )
    args = parser.parse_args(argv)
    if args.config is None:
        run_experiment(interactive_config())
        return 0

    config_path = args.config.expanduser().resolve()
    runs = load_config(config_path)
    failures = 0
    for index, config in enumerate(runs, start=1):
        label = config["name"] or f"run {index}/{len(runs)}"
        print(f"\n=== {label} ===")
        try:
            run_experiment(config, config_source=config_path)
        except Exception as error:
            failures += 1
            print(f"Run failed ({type(error).__name__}): {error}", file=sys.stderr)
    if failures:
        print(f"\n{failures} of {len(runs)} configured run(s) failed.", file=sys.stderr)
        return 1
    print(f"\nCompleted all {len(runs)} configured run(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
