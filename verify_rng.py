#!/usr/bin/env python3
"""Build an RNG topology, deploy Spraypoint on it in Mininet, and check reachability.

    sudo /home/rng/mininet-env/bin/python verify_rng.py              # n=8, d=3
    sudo /home/rng/mininet-env/bin/python verify_rng.py -n 16 -d 4 --cli
    python3 verify_rng.py --dry-run     # no Mininet/root: compile and simulate rules

The network is built the same way run_experiment.py builds it
(fnss.to_mininet + TCLink, no controller), so a pass here means the
"RNG random graph" topology works with the "Spraypoint" routing option.
Exit status is 0 only when every check passes.
"""

import argparse
import os
import sys

from routing import spraypoint as spraypoint_routing
from topologies import rng


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    defaults = rng.DEFAULT_PARAMETERS
    parser.add_argument("-n", type=int, default=defaults["n"], help="switches")
    parser.add_argument("-d", type=int, default=defaults["d"], help="fabric uplinks per switch")
    parser.add_argument("--hosts-per-switch", type=int, default=defaults["hosts_per_switch"])
    parser.add_argument("--seed", type=int, default=defaults["seed"], help="wiring seed")
    parser.add_argument("--bw", type=float, default=defaults["fabric_link_mbps"],
                        help="host and fabric link bandwidth in Mbps")
    parser.add_argument("-p", type=int, default=2, help="Spraypoint waypoints per node")
    parser.add_argument("--hops", "-H", dest="h", type=int, default=1,
                        help="Spraypoint next hops h")
    parser.add_argument("-l", type=int, default=None,
                        help="Spraypoint levels (default: paper formula)")
    parser.add_argument("--hash-key", default="spraypoint")
    parser.add_argument("--dry-run", action="store_true",
                        help="compile rules and simulate every host pair; no Mininet needed")
    parser.add_argument("--cli", action="store_true",
                        help="open the Mininet CLI after the checks")
    return parser.parse_args()


def print_topology(topology):
    fabric = [
        (u, v) for u, v, attributes in topology.edges(data=True)
        if attributes["type"] == "fabric"
    ]
    print(f"RNG topology: n={topology.graph['n']} d={topology.graph['d']} "
          f"seed={topology.graph['seed']}, {len(fabric)} fabric links")
    for u, v in fabric:
        print(f"  s{u + 1} -- s{v + 1}")


def dry_run(topology, parameters):
    _, params_class = spraypoint_routing.load_protocol()
    from spraypoint import RuleCompiler
    from spraypoint.deployer import plan_script

    fabric = spraypoint_routing.fabric_from_topology(topology)
    rules = RuleCompiler(fabric, params_class(**parameters)).compile_full_mesh()
    flows = sum(len(r.flows) for r in rules.values())
    groups = sum(len(r.groups) for r in rules.values())
    print(f"\nCompiled {flows} flows and {groups} groups on {len(rules)} switches "
          f"({len(plan_script(rules))} ovs-ofctl commands)")

    simulator = spraypoint_routing.load_simulator()(fabric, rules)
    hosts = [host.name for host in fabric.all_hosts()]
    failures = [
        (source, destination, outcome)
        for source in hosts for destination in hosts if source != destination
        for outcome in [simulator.deliveries(source, destination)]
        if outcome != {("delivered", destination)}
    ]
    pairs = len(hosts) * (len(hosts) - 1)
    for source, destination, outcome in failures:
        print(f"[FAIL] {source} -> {destination}: {sorted(outcome)}")
    print(f"[{'PASS' if not failures else 'FAIL'}] simulated every spray/ECMP "
          f"branch: {pairs - len(failures)}/{pairs} host pairs delivered")
    return 0 if not failures else 1


def live_run(topology, parameters, open_cli):
    import fnss
    from mininet.link import TCLink
    from mininet.log import setLogLevel
    from mininet.net import Mininet

    setLogLevel("warning")
    net = Mininet(
        topo=fnss.to_mininet(topology, relabel_nodes=True),
        link=TCLink,
        controller=spraypoint_routing.controller_class(),
        build=False,
    )
    try:
        net.build()
        net.start()
        result = spraypoint_routing.configure(net, parameters)
        print(f"\nSpraypoint deployed: {result['flow_count']} flows, "
              f"{result['group_count']} groups on {result['switch_count']} switches")
        bridge = net.switches[0].cmd(
            f"ovs-vsctl get bridge {net.switches[0].name} protocols fail_mode"
        ).split()
        print(f"{net.switches[0].name} bridge settings: {' '.join(bridge)}")

        loss = net.pingAll(timeout=1)
        print(f"[{'PASS' if loss == 0 else 'FAIL'}] pingAll: {loss}% packet loss "
              f"across {len(net.hosts)} hosts")
        if open_cli:
            from mininet.cli import CLI
            setLogLevel("output")
            CLI(net)
    finally:
        net.stop()
    return 0 if loss == 0 else 1


def main():
    args = parse_args()
    topology = rng.build_topology(
        n=args.n,
        d=args.d,
        hosts_per_switch=args.hosts_per_switch,
        host_link_mbps=args.bw,
        fabric_link_mbps=args.bw,
        seed=args.seed,
    )
    parameters = {"p": args.p, "h": args.h, "l": args.l, "hash_key": args.hash_key}
    print_topology(topology)
    print(f"Spraypoint parameters: {parameters}")

    if args.dry_run:
        return dry_run(topology, parameters)
    if os.geteuid() != 0:
        print("Mininet needs root: run with sudo (or use --dry-run).", file=sys.stderr)
        return 2
    return live_run(topology, parameters, args.cli)


if __name__ == "__main__":
    sys.exit(main())
