# Evaluating_RNG
CSE589 project

This repo has a wiki where we can place our research, finding, and any considerations we make.

## Running a network experiment

The interactive runner lets you choose a topology, traffic model, and one or
more experiments. The starter setup provides a 4-ary FNSS fat-tree,
reproducible random host-pair traffic, UDP iperf throughput, and concurrent
ICMP latency measurements. FNSS builds both the topology and traffic matrix;
Mininet runs the network experiments.

Run Mininet experiments with root privileges and the Python environment that
has FNSS and Mininet installed:

```bash
sudo /home/rng/mininet-env/bin/python /home/rng/Evaluating_RNG/run_experiment.py
```

The prompts accept defaults by pressing Enter. Results are saved in a
timestamped directory under `test-results/`. Each run includes `results.json`
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
`OVS controller`, an L2 learning switch that only works on loop-free fabrics
such as k=2. `Spraypoint` and `ECMP (Clos)` handle cyclic Clos fabrics; ECMP
installs static ARP plus per-destination select groups, with no controller.
Implementations are registered in the [routing package](./routing). Spraypoint
uses the local [protocol package](./spraypoint-protocol) unchanged and deploys
its full-mesh OpenFlow rules after Mininet starts. Its parameters are `p`,
`h`, `l` (use `null` to derive automatically), and `hash_key`. Spraypoint also
requires the protocol's documented OVS/OpenFlow support and `arp` utility.

The runner supports `Clos fat-tree` and `Azure Clos` topologies; `Uniform
random host pairs`, `Matching host pairs`, `Hub`, and `Clique` traffic;
`UDP iperf throughput`, `Load sweep`, and `Topology oversubscription ratio`
experiments. Omitted fields use the same defaults as the interactive menus
(4-ary topology, 8 flows at 1 Mbps, seed 1, and 5-second iperf duration).

`Azure Clos` is a scaled two-tier leaf-spine model of Azure's fabric:
uniform link capacity, 1:1 leaf oversubscription by default (set
`leaf_oversubscription` for 4:1 and similar), one ECMP path per spine, deep
buffers, and DC-scale 50 us link delay.
The interactive prompt accepts `leaf_oversubscription`; for example, enter
`3` to make aggregate host-link capacity per leaf three times its aggregate
leaf-to-spine capacity. This is a local leaf ratio, not the paper's network-wide
worst-case oversubscription ratio.

Every run gets its own results directory; if a run fails, the runner records
the error and continues with later runs, then exits with a non-zero status. See
[expected-test-results.md](./expected-test-results.md) for the smoke test's
expected output and pass criteria.

UDP iperf tests each selected traffic flow in turn. The runner records an idle
ICMP ping baseline, then runs another ping probe concurrently with each flow
to capture loaded packet loss and RTT. Both measurements are included with the
iperf results. Client, server, and ping output are kept in `iperf.log`. Each
iperf flow is bounded by its configured duration plus a 15-second timeout.

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

### Comparing performance and oversubscription

The [performance config](./experiment-config.performance.json) runs the same
four UDP flows at 8 Mbps over 1:1 and 4:1 Clos capacity profiles. It records
throughput, idle latency, and latency under load. The [oversubscription config](./experiment-config.oversubscription.json)
calculates the ratio for each edge and aggregation switch in both profiles;
this analysis does not start Mininet and does not require root privileges.

For these Clos profiles, the ratio at each switch is the sum of downstream
link capacities divided by the sum of upstream link capacities. The 1:1
profile uses 10 Mbps at every layer. The 4:1 profile uses 10 Mbps host links,
2.5 Mbps edge uplinks, and 0.625 Mbps aggregation uplinks. Run either config
with the same `--config` command shown above, substituting its filename.

To try Spraypoint on the same measurement path, run
[experiment-config.spraypoint.json](./experiment-config.spraypoint.json) with
the `--config` command. It uses a 4-ary Clos topology, one flow, and a
three-second throughput test.
