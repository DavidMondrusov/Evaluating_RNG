# Evaluating_RNG
CSE589 project

This repo has a wiki where we can place our research, finding, and any considerations we make.

## Running a network experiment

The interactive runner lets you choose a topology, traffic model, and one or
more experiments. The starter setup provides a 4-ary FNSS fat-tree,
reproducible random host-pair traffic, a UDP per-flow probe (sequential by
default, concurrent on request), and concurrent ICMP latency measurements.
FNSS builds both the topology and traffic matrix;
Mininet runs the network experiments.

Run Mininet experiments with root privileges and the Python environment that
has FNSS and Mininet installed:

```bash
sudo /home/rng/mininet-env/bin/python /home/rng/Evaluating_RNG/run_experiment.py
```

The prompts accept defaults by pressing Enter. Results are saved in a
timestamped directory under `test-results/`. A `--config` invocation puts every
run from that config in one combined folder,
`test-results/batch-<timestamp>-<config>/<NN>-<run name>/`. Interactive runs use
`test-results/<test>-<topology>-<routing>-<timestamp>/`, for example
`sweep-rng-ecmp-20261127T205649/`. Each run includes
`results.json`
(configuration, topology, software, and measurements) and
`traffic-matrix.xml`, plus files produced by its selected experiment
(`iperf.csv` and `iperf.log` for performance tests, or
`oversubscription.csv` for ratio analysis). The results directory is created
automatically when the runner is started.

### Running the quick JSON smoke test

The example config is a single, quick smoke test: a 2-ary Clos topology, one
1 Mbps flow, and a one-second UDP iperf measurement. Run it without answering
the menus:

```bash
sudo /home/rng/mininet-env/bin/python /home/rng/Evaluating_RNG/run_experiment.py \
  --config /home/rng/Evaluating_RNG/experiment-config.example.json
```

The config format supports a `runs` array; each run can set a `name`, topology
name and parameters, a routing option, traffic model and parameters, and a
list of experiments with their parameters. The routing option defaults to
`ECMP shortest path`; `Spraypoint` is also available. Implementations are
registered in the [routing package](./routing). Spraypoint uses the local
[protocol package](./spraypoint-protocol) unchanged and deploys its full-mesh
OpenFlow rules after Mininet starts. Its parameters are `p`, `h`, `l` (use
`null` to derive automatically), and `hash_key`. Spraypoint also requires the
protocol's documented OVS/OpenFlow support and `arp` utility.

The runner supports `Clos fat-tree`, `Azure Clos`, `Rail-optimized`, and
`Jupiter direct-connect` topologies; `Uniform random host pairs`, `Matching
host pairs`, `Hub`, and `Clique` traffic; and `UDP per-flow probe`, `Load
sweep`, and `Static topology analysis` experiments. Omitted fields use
the same defaults as the interactive menus (4-ary topology, 8 flows at
1 Mbps, seed 1, and 5-second iperf duration). Every run gets its own results
directory; if a run fails, the runner records the error and continues with
later runs, then exits with a non-zero status. See
[expected-test-results.md](./expected-test-results.md) for the smoke test's
expected output and pass criteria.

Each run also writes `status.log`: timestamped `start`/`done`/`fail` lines with
elapsed seconds for topology build, traffic planning, Mininet build/start,
routing, each experiment, and Mininet stop. The load sweep adds `sweep matrix`,
`idle ping`, `iperf`, and `summary` phases per active fraction, and the UDP
per-flow probe adds one phase per flow (or one concurrent-flows phase when
`concurrent` is true). Grep `done` and sort by the trailing seconds to find
the slow phase in a large run. HTB quantum warnings from `tc` are suppressed by
sizing the queue quantum near 100 KB.

`Azure Clos` is a scaled two-tier leaf-spine model of Azure's fabric:
uniform link capacity, 1:1 leaf oversubscription by default (set
`leaf_oversubscription` for 4:1 and similar), one ECMP path per spine, deep
buffers, and DC-scale 50 us link delay. For ToR traffic it defaults to
`host_mode: "stub"`: one stub host per leaf whose link carries the whole leaf
rate.

The interactive prompt accepts `leaf_oversubscription`; for example, enter
`3` to make aggregate host-link capacity per leaf three times its aggregate
leaf-to-spine capacity. This is a local leaf ratio, not the paper's network-wide
worst-case oversubscription ratio.

`traffic.scope` comes from the traffic model: `Uniform random host pairs` uses
`host`; `Matching`, `Hub` and `Clique` use `tor`, applying the pattern to the
leaf graph and expanding each leaf flow into host flows at the leaf's full
rate. Azure Clos then defaults to stub hosts for those patterns. Both settings
can be overridden in a config file.

The UDP per-flow probe runs the selected flows one at a time by default. Each
flow records an idle ICMP baseline, then another ping probe runs concurrently
with the iperf flow to capture loaded packet loss and RTT. Set
`"concurrent": true` on the experiment to run all flows together instead; the
result is then contention-limited rather than single-flow capacity. Client,
server, and ping output are kept in `iperf.log`. Each flow is bounded by its
configured duration plus a 15-second timeout.

The load sweep estimates a paper-style oversubscription ratio for each active
fraction of leaf switches. It rebuilds the selected traffic pattern using the
seeded subset of leaves, so the traffic matrix endpoints are leaves. Each
leaf-pair demand is carried by host-to-host iperf subflows: the source leaf's
aggregate host-link capacity is divided among its outgoing leaf pairs and then
among its attached hosts. Host endpoints are chosen on different leaves.
`oversubscription_ratio` is the inverse of the minimum delivered/offered rate
fraction across leaf pairs; this worst-pair metric is the one closest to the
paper's worst-flow measure. Zero delivered throughput for a pair is reported
as `inf`. `aggregate_offered_to_delivered_ratio` is total offered Mbps divided
by total delivered Mbps for the matrix; it can hide an individual pair that
gets very little service. Both metrics are reported in the CSV and results.
`matrix_repetitions` can be increased to report the mean, minimum, and maximum
ratio across independent traffic matrices. The default is one repetition to
keep Mininet runs practical; the paper used 100 matrices for its plotted
statistics. Active fractions that round to the same number of leaves are
measured only once, and the CSV reports the resulting actual fraction.
Matching traffic requires an even number of leaves, so an odd active count is
rounded down to the nearest even count.

Each active-fraction result also includes aggregate offered, sent, and
delivered Mbps, `failed_flow_count`, and packet loss. A client that exits
unsuccessfully or times out without usable counters contributes zero delivered
throughput and is counted as failed; the sweep continues. Raw client, server,
and ping output is recorded in `load-sweep.log`. Delivered throughput is taken
from iperf's server-side receiver intervals, not the client's offered/send
rate; intervals are aggregated because some iperf3 versions report an empty
final receiver summary.
This is an empirical estimate using iperf and the selected routing, not the
paper's exact multicommodity-flow LP.

### Static topology analysis

`Static topology analysis` needs no Mininet, no root, and no traffic pattern:
it reads only the topology, so it is safe to run before any long build. It
writes `oversubscription.csv` (per-switch downstream/upstream capacity and
ratio) and `tor-pairs.csv` (per ToR pair), and reports in `results.json`:

* `maximum_oversubscription_ratio` plus the switch that has it. For a Clos the
  ratio at each switch is downstream link capacity divided by upstream link
  capacity (1:1 uses 10 Mbps at every layer; 4:1 uses 10 Mbps host links,
  2.5 Mbps edge uplinks, and 0.625 Mbps aggregation uplinks).
* per ToR pair: the number of edge-disjoint paths (Menger), the shortest-path
  hop count, the aggregate pair capacity (max flow over the real link
  capacities), and the weakest and strongest single-path bottleneck.
* `aggregate_oversubscription_ratio` = offered ToR rate / aggregate pair
  capacity, and `single_path_oversubscription_ratio` = offered ToR rate /
  single-path bottleneck. The second is the ceiling single-path hashing (ECMP)
  leaves a flow with; the first is what spraying across disjoint paths can
  reach. A 128x32 Azure Clos with 0.625 Mbps spine links has 32 disjoint paths,
  aggregate ratio 1.0, and single-path ratio 32.0.
* `topology_summary`: node and link counts by type/layer, host and fabric
  capacity, link capacity distribution, hosts per ToR, connectivity.

Pair analysis costs one max-flow solve per pair (~40 ms at 128 ToRs), so at
most `pair_sample` pairs are analyzed (default 32; the sample is deterministic
for `pair_seed`). Every pair is analyzed when the fabric has fewer than that.

To try Spraypoint on the same measurement path, run
[experiment-config.spraypoint.json](./experiment-config.spraypoint.json) with
the `--config` command. It uses a 4-ary Clos topology, one flow, and a
three-second throughput test.

### RNG topology

`RNG random graph` builds the flat, quasi-random fabric from
[RNG-paper.pdf](./RNG-paper.pdf): `n` switches, each with `d` fabric uplinks
wired as a uniformly random, connected, simple `d`-regular graph, and
`hosts_per_switch` hosts on every switch. Its parameters are `n`, `d`,
`hosts_per_switch`, `host_link_mbps`, `fabric_link_mbps`, and `seed` (the
wiring is reproducible per seed; `n * d` must be even). This v1 builds the
logical switch-to-switch graph directly; the paper's optical ShuffleBoxes are
not modelled. The graph has cycles, so use it with `Spraypoint` routing: the
default `OVS controller` learning switch floods ARP around loops. The
oversubscription experiment only supports Clos layers and rejects RNG.

```bash
# Full runner path: RNG + Spraypoint + UDP iperf
sudo /home/rng/mininet-env/bin/python /home/rng/Evaluating_RNG/run_experiment.py \
  --config /home/rng/Evaluating_RNG/experiment-config.rng.json

# Deploy Spraypoint on an RNG fabric and pingAll (exit 0 on success)
sudo /home/rng/mininet-env/bin/python /home/rng/Evaluating_RNG/verify_rng.py -n 16 -d 4

# Offline: compile the rules and simulate every host pair, no root needed
python3 verify_rng.py --dry-run -n 16 -d 4

# Unit tests (offline)
python3 -m unittest tests/test_rng_topology.py -v
```

### Failure modeling

To see how a failure changes the results of the same experiments, add a
`failure` block to a run in a JSON config:

```json
"failure": {"mode": "link", "count": 2, "seed": 1}
```

* `mode`: `"link"` takes down `count` links between two switches. `"node"`
  takes down `count` switches, together with all of their links, including the
  links to their hosts.
* `seed` decides which links or switches fail. The same seed on the same
  topology always fails the same ones.

The run then measures every experiment three times on the same network:

1. `intact`: before the failure.
2. `stale`: after the failure, while the switches still use their old routes.
   Traffic sent toward a failed link or switch is lost.
3. `rerouted`: after the routes are recomputed using only the links that still
   work.

Each phase's files are saved in its own folder (`intact/`, `stale/`,
`rerouted/`) inside the run's results folder. For the UDP per-flow probe,
`failure-flows-<n>.csv` (`<n>` is the probe's position in the run's experiment
list) shows each flow's rate in every phase, with one of these labels for the
`stale` and `rerouted` phases:

* `unaffected`: delivers at least 90% of its `intact` rate.
* `degraded`: still delivers traffic, but less than 90% of its `intact` rate.
* `fabric_dead`: both hosts still work, but no traffic gets through.
* `connect_failed`: both hosts still work, but iperf3 could not set up the
  test, because its TCP control connection was routed over a failed link. See
  the caveat below.
* `endpoint_lost`: the sending or receiving host was attached to a failed
  switch. No topology can save such a flow, so compare topologies on
  `fabric_dead`, not on this label.
* `failed_intact`: the flow already failed before the failure.

`results.json` lists the failed switches and links (Mininet names), the hosts
that lost their switch, and how many flows got each label.

Only two combinations are supported: `Clos fat-tree` with `ECMP shortest path`,
and `RNG random graph` with `Spraypoint`. Any other combination stops with an
error before Mininet starts. The UDP per-flow probe must use
`"concurrent": true`.

Caveat: running the same config twice gives the same `fabric_dead` flows (TODO: this is not yet true! OVS in the way we are configuring it still hashes nondeterministically between paths (from Linux hashing randomness), so we need to figure out a way to get it to hash deterministically for this to actually hold), but
the `connect_failed` flows can differ. With `"concurrent": true`, each flow's
UDP traffic uses a fixed source port chosen from the traffic `seed`, so it
takes the same path in every run. iperf3 also opens a TCP control connection
for each flow, and that connection always uses a random port, so its path
changes between runs. iperf3 has no option to fix that port. Treat
`connect_failed` counts as noisy, and repeat a run to see how much they vary.

To run the example, which fails links and switches in a 4-ary fat-tree and in
an RNG of the same size (8 switches with 2 hosts each, 1:1 oversubscription):

```bash
sudo /home/rng/mininet-env/bin/python /home/rng/Evaluating_RNG/run_experiment.py \
  --config /home/rng/Evaluating_RNG/experiment-config.failure.json
```

## Running large topologies

Large fabrics create tens of thousands of virtual interfaces in the root
network namespace. For example, `Azure Clos` with 256 leaves, 64 spines and
2 hosts per leaf creates about 34k. Several host daemons react to every one
of them. Without mitigation this can starve DNS and keep the machine busy for
hours. Before a large experiment, quiet them with
[extra_scripts/mitigate.sh](./extra_scripts/mitigate.sh), and restore the
system afterwards:

```bash
sudo /home/rng/Evaluating_RNG/extra_scripts/mitigate.sh on
# ... run as many experiments as you like ...
sudo /home/rng/Evaluating_RNG/extra_scripts/mitigate.sh off

# Show the current state (no root needed)
/home/rng/Evaluating_RNG/extra_scripts/mitigate.sh status
```

`on` saves the original state in `/var/lib/mininet-mitigate`, and `off`
restores it. While mitigations are on:

- networkd-dispatcher, systemd-resolved and systemd-udevd are stopped.
- DNS goes straight to the upstream servers through a static
  `/etc/resolv.conf`.
- systemd-networkd and udev leave Mininet interfaces (`s*-eth*`, `h*-eth*`)
  alone.
- New interfaces get no IPv6.
- Hotplugged devices are not handled, because udev is stopped. This doesn't
  matter on the experiment VM.

If the machine reboots while mitigations are on, the services start again
but the override files remain. Run `off` and then `on` again before the next
experiment. If an experiment crashes, clean up Mininet with `sudo mn -c`; the
mitigations stay as they are.

The runner also batches the steps whose cost grows with fabric size: tc
shaping, OVS startup, ECMP flow tables, static ARP and teardown. See
[tc_batch.py](./tc_batch.py) and [mininet_batch.py](./mininet_batch.py).
Teardown briefly stops and restarts `ovs-vswitchd`, which removes all bridges
and ports at once, but only when every OVS bridge on the host belongs to the
experiment. Otherwise it removes them through the running daemon.

For scale, the full 256x64 Azure Clos run with ECMP and a load sweep took
92 min originally and about 27 min with these changes. Most of the remaining
time (about 15 min) is OVS adding the 34k switch ports at startup.
