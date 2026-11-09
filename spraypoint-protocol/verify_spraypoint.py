#!/usr/bin/env python3
"""Deploy Spraypoint on the Figure 3 topology and verify v2 <-> t reachability.

    sudo python3 verify_spraypoint.py                 # pair mode, paper waypoints
    sudo python3 verify_spraypoint.py --mode full     # every destination
    python3 verify_spraypoint.py --dry-run            # no Mininet: print levels and rules

Exit status is 0 when every check passes.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time

from init_topology_test import figure3
from spraypoint import FabricGraph, RuleCompiler, SpraypointParams
from spraypoint.deployer import plan_script
from spraypoint.levels import LevelAssignment

WALKTHROUGH_SWITCHES = ["v2", "w3", "w4", "o1", "r6", "w5", "v3", "t"]


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["pair", "full"], default="pair",
                    help="pair: rules for src<->dst only; full: full mesh (default: pair)")
    ap.add_argument("--src", default="v2", help="source switch (its host is used)")
    ap.add_argument("--dst", default="t", help="destination switch (its host is used)")
    ap.add_argument("-p", type=int, default=2, help="waypoints per node (default 2)")
    ap.add_argument("--hops", "-H", dest="h", type=int, default=1, help="eligible next hops h (default 1)")
    ap.add_argument("-l", type=int, default=1, help="waypoint levels l (default 1)")
    ap.add_argument("--hash-key", default="spraypoint", help="shared deterministic hashing key")
    ap.add_argument("--waypoints", choices=["paper", "hashed"], default="paper",
                    help="paper: pin WP1(t) to Figure 3's picks; hashed: deterministic hashing everywhere")
    ap.add_argument("--iperf-time", type=int, default=5, help="iperf3 duration in seconds")
    ap.add_argument("--iperf-streams", type=int, default=4, help="parallel iperf3 streams (exercises spraying)")
    ap.add_argument("--bw", type=float, default=None, help="link bandwidth in Mbps (TCLink); default unshaped")
    ap.add_argument("--dry-run", action="store_true", help="compute levels/rules only; no Mininet needed")
    ap.add_argument("--cli", action="store_true", help="open the Mininet CLI after the checks")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args()


# --------------------------------------------------------------------- report
def print_levels(la: LevelAssignment) -> None:
    print(f"\nLevels toward {la.destination} (l={la.l}):")
    for label, nodes in la.table().items():
        print(f"  {label:>4}: {', '.join(nodes)}")
    if la.selections:
        print("  waypoint selections:")
        for m, sel in enumerate(la.selections, start=1):
            for u in sorted(sel):
                print(f"    WP{m - 1} {u} selects {sel[u]}")
    print("  next hops:")
    for u in sorted(la.next_hops, key=lambda n: (la.level_of(n), n)):
        print(f"    {la.level_of(u):>4} {u:>4} -> {', '.join(la.next_hops[u])}")


def print_walk(la: LevelAssignment, compiler: RuleCompiler, src: str) -> None:
    graph = compiler.graph
    print(f"\nWalkthrough {src} -> {la.destination}: {src} sprays to {graph.neighbors(src)}")
    for v in graph.neighbors(src):
        path, u = [v], v
        while u != la.destination and la.next_hops.get(u):
            u = la.next_hops[u][0]
            path.append(u)
        print(f"  via {v:>3} [{la.level_of(v)}]: {src} -> " + " -> ".join(path))


def offline_graph() -> FabricGraph:
    """Same switch/port numbering Mininet assigns in figure3.build_network()."""
    return FabricGraph.from_edges(figure3.edges, host_ip=lambda sw, i, k: f"10.{i}.0.1")


# --------------------------------------------------------------------- checks
def check_ping(src_host, dst_host, count=5) -> bool:
    out = src_host.cmd(f"ping -c {count} -W 1 {dst_host.IP()}")
    print(out.strip())
    ok = f" 0% packet loss" in out
    print(f"[{'PASS' if ok else 'FAIL'}] ping {src_host.name} -> {dst_host.name}")
    return ok


def check_iperf3(client, server, seconds, streams) -> bool:
    if not shutil.which("iperf3"):
        print("[FAIL] iperf3 is not installed (apt install iperf3)")
        return False
    srv = server.popen(["iperf3", "-s", "-1", "-p", "5201"])
    try:
        time.sleep(0.5)
        raw = client.cmd(f"iperf3 -c {server.IP()} -p 5201 -t {seconds} -P {streams} -J")
    finally:
        srv.terminate()
        srv.wait()
    try:
        result = json.loads(raw[raw.index("{"):])
    except ValueError:
        print(raw.strip())
        print("[FAIL] iperf3 produced no JSON result")
        return False
    if "error" in result:
        print(f"[FAIL] iperf3: {result['error']}")
        return False
    end = result["end"]
    rx = end["sum_received"]["bits_per_second"] / 1e6
    retr = end.get("sum_sent", {}).get("retransmits", "n/a")
    ok = rx > 0
    print(f"[{'PASS' if ok else 'FAIL'}] iperf3 {client.name} -> {server.name}: "
          f"{rx:.2f} Mbit/s received, {streams} streams, retransmits={retr}")
    return ok


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    params = SpraypointParams(p=args.p, h=args.h, l=args.l, hash_key=args.hash_key)
    overrides = {"t": figure3.PAPER_WAYPOINTS_T} if args.waypoints == "paper" else None
    src_h, dst_h = figure3.host_name(args.src), figure3.host_name(args.dst)
    print(f"Spraypoint parameters: p={params.p} h={params.h} l={params.l} "
          f"waypoints={args.waypoints} mode={args.mode}")

    if args.dry_run:
        compiler = RuleCompiler(offline_graph(), params, overrides, figure3.subnets())
        la = compiler.levels(args.dst)
        print_levels(la)
        print_walk(la, compiler, args.src)
        rules = (compiler.compile_pair(src_h, dst_h) if args.mode == "pair"
                 else compiler.compile_full_mesh())
        print("\nEquivalent ovs-ofctl commands:")
        print("\n".join(plan_script(rules)))
        return 0

    if os.geteuid() != 0:
        print("Mininet needs root: run with sudo (or use --dry-run).", file=sys.stderr)
        return 2

    from mininet.log import setLogLevel
    from spraypoint import SpraypointDeployer

    setLogLevel("warning")
    net = figure3.build_network(args.bw)
    results = []
    try:
        net.start()
        dep = SpraypointDeployer(net, params, waypoint_overrides=overrides,
                                 subnets=figure3.subnets())
        la = dep.levels(args.dst)
        print_levels(la)
        print_walk(la, dep.compiler, args.src)

        if args.mode == "pair":
            dep.deploy_pair(src_h, dst_h)
        else:
            dep.deploy_full_mesh()

        print(f"\nInstalled tables (rules toward {args.dst}, cookie "
              f"{dep.compiler.cookie(args.dst):#x}):")
        for sw in [args.src] + [s for s in WALKTHROUGH_SWITCHES if s != args.src]:
            if sw in dep.graph.ports:
                print(dep.dump(sw, destination=args.dst))

        src, dst = net.get(src_h), net.get(dst_h)
        print("\n--- reachability ---")
        results.append(check_ping(src, dst))
        results.append(check_ping(dst, src))
        results.append(check_iperf3(src, dst, args.iperf_time, args.iperf_streams))
        print(f"\nSpray group counters on {args.src} (bucket counters show the ECMP spread):")
        print(dep.group_stats(args.src).strip())

        if args.cli:
            from mininet.cli import CLI
            # The CLI prints nodes/net/dump/ping through Mininet's logger at
            # "output" level, which "warning" (used above to quiet startup) hides.
            setLogLevel("output")
            CLI(net)
    finally:
        net.stop()

    passed = all(results)
    print(f"\n{'ALL CHECKS PASSED' if passed else 'SOME CHECKS FAILED'} "
          f"({sum(results)}/{len(results)})")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
