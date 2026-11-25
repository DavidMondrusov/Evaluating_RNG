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
from mininet.net import Mininet

from experiments import iperf, load_sweep, oversubscription
from routing import (
    DEFAULT_ROUTING,
    ROUTING_METHODS,
    configure as configure_routing,
    default_parameters as default_routing_parameters,
    get_controller_class,
)
from mininet_batch import BatchedOVSSwitch, stop_network
from tc_batch import BatchedTCLink, batched_tc, flush_tc
from topologies import azure_clos, clos, jupiter, rail_optimized, rng
from traffic import clique, hub, matching, uniform_random


ROOT = Path(__file__).resolve().parent
RESULTS_ROOT = ROOT / "test-results"
TOPOLOGIES = {
    "Clos fat-tree": {
        "builder": clos.build_topology,
        "parameters": {
            "k": 4,
            "host_link_mbps": 10,
            "edge_uplink_mbps": 10,
            "aggregation_uplink_mbps": 10,
        },
        "validator": clos.validate_parameters,
        "prompts": ("k",),
    },
    "RNG random graph": {
        "builder": rng.build_topology,
        "parameters": dict(rng.DEFAULT_PARAMETERS),
        "validator": lambda parameters: rng.validate_parameters(**parameters),
        "prompts": ("n", "d", "hosts_per_switch"),
    },
    "Rail-optimized": {
        "builder": rail_optimized.build_topology,
        "parameters": dict(rail_optimized.DEFAULT_PARAMETERS),
        "validator": lambda parameters: rail_optimized.validate_parameters(
            **parameters
        ),
        "prompts": (
            "scalable_units",
            "servers_per_unit",
            "gpus_per_server",
            "spine_count",
        ),
    },
    "Jupiter direct-connect": {
        "builder": jupiter.build_topology,
        "parameters": dict(jupiter.DEFAULT_PARAMETERS),
        "validator": lambda parameters: jupiter.validate_parameters(
            **parameters
        ),
        "prompts": (
            "blocks",
            "middle_blocks",
            "tors_per_block",
            "hosts_per_tor",
        ),
    },
    "Azure Clos": {
        "builder": azure_clos.build_topology,
        "parameters": dict(azure_clos.DEFAULT_PARAMETERS),
        "validator": azure_clos.validate_parameters,
        "prompts": (
            "leaves",
            "spines",
            "hosts_per_leaf",
            "leaf_oversubscription",
        ),
    },
}
TRAFFIC_MODELS = {
    "Uniform random host pairs": {
        "builder": uniform_random.build_traffic,
        "parameters": {"flow_count": 8, "demand_mbps": 1.0, "seed": 1},
    },
    "Matching host pairs": {
        "builder": matching.build_traffic,
        "parameters": {"demand_mbps": 1.0, "seed": 1},
    },
    "Hub": {
        "builder": hub.build_traffic,
        "parameters": {"demand_mbps": 1.0, "seed": 1},
    },
    "Clique": {
        "builder": clique.build_traffic,
        "parameters": {"demand_mbps": 1.0, "seed": 1},
    },
}
EXPERIMENTS = {
    "UDP iperf throughput": {
        "runner": iperf.run,
        "parameters": {"duration_seconds": 5},
        "requires_network": True,
        "uses_traffic_demand": True,
    },
    "Load sweep": {
        "runner": load_sweep.run,
        "parameters": {
            "duration_seconds": 5,
            "active_fractions": [index / 10 for index in range(1, 11)],
            "matrix_repetitions": 1,
        },
        "requires_network": True,
        "needs_topology": True,
        "needs_traffic_model": True,
    },
    "Topology oversubscription ratio": {
        "runner": oversubscription.run,
        "parameters": {},
        "requires_network": False,
    }
}
DEFAULT_TOPOLOGY = next(iter(TOPOLOGIES))
DEFAULT_TRAFFIC = next(iter(TRAFFIC_MODELS))
DEFAULT_EXPERIMENT = next(iter(EXPERIMENTS))


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
        {"name", "topology", "routing", "traffic", "experiments"},
    )
    name = config.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise ValueError(f"{location}.name must be a non-empty string")

    routing_config = require_mapping(
        config.get("routing", {}),
        f"{location}.routing",
        {"name", "parameters"},
    )
    routing_name = routing_config.get("name", DEFAULT_ROUTING)
    if not isinstance(routing_name, str) or routing_name not in ROUTING_METHODS:
        raise ValueError(
            f"{location}.routing.name must be one of: "
            + ", ".join(ROUTING_METHODS)
        )
    routing_parameters = require_mapping(
        routing_config.get("parameters", {}),
        f"{location}.routing.parameters",
        default_routing_parameters(routing_name),
    )
    routing_parameters = {
        **default_routing_parameters(routing_name),
        **routing_parameters,
    }
    if routing_name == "Spraypoint":
        for parameter in ("p", "h"):
            value = routing_parameters[parameter]
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(
                    f"{location}.routing.parameters.{parameter} must be a "
                    "positive integer"
                )
        levels = routing_parameters["l"]
        if levels is not None and (
            not isinstance(levels, int)
            or isinstance(levels, bool)
            or levels < 0
        ):
            raise ValueError(
                f"{location}.routing.parameters.l must be a non-negative "
                "integer or null"
            )
        hash_key = routing_parameters["hash_key"]
        if not isinstance(hash_key, str) or not hash_key:
            raise ValueError(
                f"{location}.routing.parameters.hash_key must be a "
                "non-empty string"
            )

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
    try:
        TOPOLOGIES[topology_name]["validator"](topology_parameters)
    except ValueError as error:
        raise ValueError(
            f"{location}.topology.parameters: {error}"
        ) from error

    raw_traffic = config.get("traffic", {})
    if not isinstance(raw_traffic, Mapping):
        raise ValueError(f"{location}.traffic must be a JSON object")
    traffic_name = raw_traffic.get("name", DEFAULT_TRAFFIC)
    if not isinstance(traffic_name, str) or traffic_name not in TRAFFIC_MODELS:
        raise ValueError(
            f"{location}.traffic.name must be one of: "
            + ", ".join(TRAFFIC_MODELS)
        )
    traffic_parameters = TRAFFIC_MODELS[traffic_name]["parameters"]
    traffic_config = require_mapping(
        raw_traffic,
        f"{location}.traffic",
        {"name", *traffic_parameters},
    )
    traffic_parameters = {
        **traffic_parameters,
        **{
            key: value
            for key, value in traffic_config.items()
            if key != "name"
        },
    }
    if "flow_count" in traffic_parameters:
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
        experiment_name = (
            raw_experiment.get("name", DEFAULT_EXPERIMENT)
            if isinstance(raw_experiment, Mapping)
            else DEFAULT_EXPERIMENT
        )
        if (
            not isinstance(experiment_name, str)
            or experiment_name not in EXPERIMENTS
        ):
            raise ValueError(
                f"{experiment_location}.name must be one of: "
                + ", ".join(EXPERIMENTS)
            )
        experiment_parameters = EXPERIMENTS[experiment_name]["parameters"]
        experiment_config = require_mapping(
            raw_experiment,
            experiment_location,
            {"name", *experiment_parameters},
        )
        parameters = {
            **experiment_parameters,
            **{
                key: value
                for key, value in experiment_config.items()
                if key != "name"
            },
        }
        if "duration_seconds" in parameters:
            duration_seconds = parameters["duration_seconds"]
            if (
                not isinstance(duration_seconds, int)
                or isinstance(duration_seconds, bool)
                or duration_seconds < 1
            ):
                raise ValueError(
                    f"{experiment_location}.duration_seconds must be a "
                    "positive integer"
                )
        if "active_fractions" in parameters:
            fractions = parameters["active_fractions"]
            if not isinstance(fractions, list) or not fractions:
                raise ValueError(
                    f"{experiment_location}.active_fractions must be a "
                    "non-empty list"
                )
            for fraction in fractions:
                if (
                    not isinstance(fraction, (int, float))
                    or isinstance(fraction, bool)
                    or not math.isfinite(fraction)
                    or not 0 < fraction <= 1
                ):
                    raise ValueError(
                        f"{experiment_location}.active_fractions values must "
                        "be numbers greater than 0 and at most 1"
                    )
        if "matrix_repetitions" in parameters:
            repetitions = parameters["matrix_repetitions"]
            if (
                not isinstance(repetitions, int)
                or isinstance(repetitions, bool)
                or repetitions < 1
            ):
                raise ValueError(
                    f"{experiment_location}.matrix_repetitions must be a "
                    "positive integer"
                )
        experiments.append(
            {"name": experiment_name, "parameters": parameters}
        )

    return {
        "name": name.strip() if name else None,
        "routing": {"name": routing_name, "parameters": routing_parameters},
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


def to_mininet_topology(topology):
    """Convert an FNSS topology to a Mininet Topo, keeping link buffer sizes.

    FNSS stores buffer sizes in the "buffer" link attribute, but
    fnss.to_mininet only reads "buffer_size", so it silently drops them.
    Copy each buffer into the link's max_queue_size, which TCLink applies as
    the netem queue limit (in packets) on both interfaces.
    """
    mininet_topology = fnss.to_mininet(topology, relabel_nodes=True)
    if topology.graph.get("buffer_unit") != "packets":
        # max_queue_size counts packets; fnss.to_mininet discards other units.
        return mininet_topology

    # Same relabeling as fnss.to_mininet: sorted hosts h1.., sorted switches s1..
    mininet_names = {}
    for node_type, prefix in (("host", "h"), ("switch", "s")):
        nodes = sorted(
            node for node, attributes in topology.nodes(data=True)
            if attributes.get("type") == node_type
        )
        for index, node in enumerate(nodes, start=1):
            mininet_names[node] = f"{prefix}{index}"

    for source, target, attributes in topology.edges(data=True):
        if "buffer" in attributes:
            link_info = mininet_topology.linkInfo(
                mininet_names[source], mininet_names[target]
            )
            link_info.setdefault("max_queue_size", int(attributes["buffer"]))
    return mininet_topology


def interactive_config():
    print("Network experiment runner")
    topology_name = choose_one("Choose a topology", TOPOLOGIES)
    topology_parameters = dict(TOPOLOGIES[topology_name]["parameters"])
    for parameter_name in TOPOLOGIES[topology_name].get("prompts", ()):
        default = topology_parameters[parameter_name]
        label = parameter_name.replace("_", " ").capitalize()
        if parameter_name == "k":
            while True:
                value = prompt_positive_int("Clos k (even, >= 2)", default)
                if value % 2 == 0:
                    break
                print("k must be an even integer.")
        elif parameter_name == "leaf_oversubscription":
            value = prompt_positive_float(
                "Leaf host-to-uplink oversubscription ratio", default
            )
        elif isinstance(default, int):
            value = prompt_positive_int(label, default)
        else:
            value = prompt_positive_float(label, default)
        topology_parameters[parameter_name] = value
    routing_name = choose_one("Choose a routing option", ROUTING_METHODS)
    traffic_name = choose_one("Choose a traffic model", TRAFFIC_MODELS)
    experiment_names = choose_many("Choose experiments to run", EXPERIMENTS)

    traffic_defaults = TRAFFIC_MODELS[traffic_name]["parameters"]
    traffic = {
        "name": traffic_name,
        "demand_mbps": traffic_defaults["demand_mbps"],
        "seed": traffic_defaults["seed"],
    }
    if "flow_count" in traffic_defaults:
        traffic["flow_count"] = prompt_positive_int(
            "Number of traffic flows", traffic_defaults["flow_count"]
        )
    if any(
        EXPERIMENTS[name].get("uses_traffic_demand")
        for name in experiment_names
    ):
        traffic["demand_mbps"] = prompt_positive_float(
            "Demand per flow (Mbps)", traffic_defaults["demand_mbps"]
        )
        traffic["seed"] = prompt_integer(
            "Random seed", traffic_defaults["seed"]
        )
    experiments = []
    for experiment_name in experiment_names:
        experiment = {"name": experiment_name}
        if "duration_seconds" in EXPERIMENTS[experiment_name]["parameters"]:
            experiment["duration_seconds"] = prompt_positive_int(
                "UDP iperf duration per flow (seconds)", 5
            )
        if experiment_name == "Load sweep":
            experiment["matrix_repetitions"] = prompt_positive_int(
                "Traffic matrices per active fraction", 1
            )
        experiments.append(experiment)
    return normalize_run_config(
        {
            "topology": {
                "name": topology_name,
                "parameters": topology_parameters,
            },
            "routing": {
                "name": routing_name,
                "parameters": default_routing_parameters(routing_name),
            },
            "traffic": traffic,
            "experiments": experiments,
        },
        "interactive run",
    )


def run_experiment(config, config_source=None):
    topology_config = config["topology"]
    traffic_config = config["traffic"]
    topology = TOPOLOGIES[topology_config["name"]]["builder"](
        **topology_config["parameters"]
    )
    network_required = any(
        EXPERIMENTS[experiment["name"]]["requires_network"]
        for experiment in config["experiments"]
    )
    traffic_matrix = None
    if network_required:
        traffic_matrix = TRAFFIC_MODELS[traffic_config["name"]]["builder"](
            topology,
            **{
                key: value
                for key, value in traffic_config.items()
                if key != "name"
            },
        )

    run_id, run_dir = create_run_directory()
    traffic_summary = None
    if traffic_matrix is not None:
        traffic_path = run_dir / "traffic-matrix.xml"
        fnss.write_traffic_matrix(traffic_matrix, str(traffic_path))
        traffic_summary = {
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
        }

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
        "routing": config["routing"],
        "routing_setup": None,
        "traffic": traffic_summary,
        "experiments": [],
    }
    summary_path = run_dir / "results.json"
    write_summary(summary_path, summary)

    net = None
    network_needs_cleanup = False
    experiments_completed = False
    network_stopped = not network_required
    try:
        if not network_stopped:
            if os.geteuid() != 0:
                raise PermissionError(
                    "Mininet requires root privileges. Run this script with "
                    "the environment's Python executable via sudo."
                )

            mininet_topology = to_mininet_topology(topology)
            host_nodes = sorted(
                node for node, attributes in topology.nodes(data=True)
                if attributes.get("type") == "host"
            )
            host_names = {
                node: f"h{index}"
                for index, node in enumerate(host_nodes, start=1)
            }
            net = Mininet(
                topo=mininet_topology,
                link=BatchedTCLink,
                switch=BatchedOVSSwitch,
                controller=get_controller_class(config["routing"]["name"]),
                build=False,
            )
            network_needs_cleanup = True
            network_stopped = False
            # tc rescans every interface on each call; batch it so deploy
            # time stays linear in the number of links (see tc_batch.py).
            with batched_tc():
                net.build()
                flush_tc()
                net.start()
                flush_tc()
            summary["routing_setup"] = configure_routing(
                config["routing"]["name"],
                net,
                config["routing"]["parameters"],
            )
            write_summary(summary_path, summary)
        for experiment_config in config["experiments"]:
            experiment_name = experiment_config["name"]
            experiment_definition = EXPERIMENTS[experiment_name]
            if experiment_definition["requires_network"]:
                topology_arguments = (
                    {"topology": topology}
                    if experiment_definition.get("needs_topology")
                    else {}
                )
                if experiment_definition.get("needs_traffic_model"):
                    topology_arguments.update(
                        {
                            "traffic_name": traffic_config["name"],
                            "traffic_builder": TRAFFIC_MODELS[
                                traffic_config["name"]
                            ]["builder"],
                            "traffic_parameters": {
                                key: value
                                for key, value in traffic_config.items()
                                if key != "name"
                            },
                        }
                    )
                experiment_result = experiment_definition["runner"](
                    net,
                    traffic_matrix,
                    host_names,
                    output_dir=run_dir,
                    **topology_arguments,
                    **experiment_config["parameters"],
                )
            else:
                experiment_result = experiment_definition["runner"](
                    topology,
                    output_dir=run_dir,
                    **experiment_config["parameters"],
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
                stop_network(net)
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
