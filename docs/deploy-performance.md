# Large-topology deploy performance: from VM hang to 27 minutes

This report covers the investigation and fixes for deploying very large
fabrics in Mininet: the problem, how it was investigated, each cause found,
each fix and its measured effect, what did not work, and what remains.

## Summary

On Friday 2026-11-20 this experiment slowed the VM to a crawl, broke DNS,
and never finished building the topology:

```
Topology: Azure Clos, 256 leaves x 64 spines x 2 hosts per leaf, oversubscription 1.0
Routing:  ECMP shortest path
Traffic:  Hub
Experiment: Load sweep (5 s iperf per flow, 1 traffic matrix per active fraction)
```

That fabric has 320 switches, 512 hosts and 16,896 links, which means about
33.8k virtual interfaces in the root network namespace and 33.6k OVS ports.

It now deploys, runs and tears down in **26.8 minutes**, with DNS working
and the machine responsive throughout:

| Phase | Friday | First full run (fixes 1-3) | Now |
|---|---|---|---|
| Build | did not finish (VM rebooted at ~12.5k of 33.8k interfaces) | 6.3 min | 3.7 min |
| Switch start | — | 15.6 min | 14.9 min |
| ECMP, static ARP, tc reapply | — | 11.8 min | 4.7 min |
| **Deploy total** | **never finished** | **33.8 min** | **23.3 min** |
| Teardown | — | 57.6 min | **3.2 min** |
| **Total** | — | **92 min** | **26.8 min** |

The root causes were not one bug but several steps whose cost grows with the
*square* of the fabric size. Small fabrics hide them completely. Each fix
replaces one of these quadratic steps with a step that does the work in a
single operation (a batch).

The changes are on branch `feature/main/improve-rng-deployment-performance`:

| Commit | Change |
|---|---|
| `a04fb79` | Batch Mininet's `tc` commands |
| `245b20d` | ECMP: one `ovs-vsctl` transaction and OpenFlow bundles |
| `f53da57` | Batched OVS startup, static ARP, ECMP tables and link deletion |
| `20a758d` | Teardown with `ovs-vswitchd` stopped |
| `f2f029e` | `extra_scripts/mitigate.sh` (host daemons) |
| `78805a2` | README section "Running large topologies" |

**Out of scope:** every run here ends at the load sweep's idle-ping
`TimeoutExpired` (`experiments/iperf.py`, `measure_idle`) after the first one
or two load fractions. The team decided to ignore it for now, so this report
measures only deploy and teardown, not the traffic phase.

## Starting point: what Friday's run left behind

The system journal from the two hangs, which ended in reboots at 22:52 and
23:28 UTC, showed:

- About 12.5k of the 33.8k interfaces had been created when the VM was
  rebooted. The topology build was still running hours later.
- `systemd-resolved` had used 13-15 minutes of CPU and had to be SIGKILLed on
  shutdown. It is single-threaded and was busy tracking interfaces, so it
  could not answer DNS queries. That was the DNS outage.
- `networkd-dispatcher` was re-running `networkctl list` for every new
  interface, and each run lists every interface: O(N^2).
- `ovs-vswitchd` had used 5 h 37 min of CPU and logged a 310-second stall in
  its main loop.

So two separate problems were mixed together:

1. host daemons reacting to every interface, which caused the DNS outage and
   the unresponsive machine;
2. the deploy itself getting slower with every interface, which is why it
   never finished.

## How it was investigated

Re-running the failing experiment would just hang the VM again. Instead:

- **A staged probe** (`scale_probe.py`, kept outside the repo in
  `~/scale-probe/`) built the same Azure Clos topology at growing sizes: 8x2,
  16x4, 32x8, 64x16, then 128x32 leaves x spines. It timed each phase (build,
  start, ECMP flows, static ARP, stop) and recorded:
  - the CPU used by each host daemon;
  - DNS latency and failures, from a background DNS prober;
  - the interface count.
- **A watchdog** aborted the probe if DNS started failing or the load average
  rose too high, so the probe could not take the VM down.
- **Small benchmarks** checked each suspected cause in isolation, mostly
  without root:
  - `tc` in a private network namespace (`unshare -rn`);
  - a private OVS instance with dummy ports;
  - mass veth creation and deletion.

The growth rate mattered most. A step that takes 1 s at 64x16 and grows
linearly is harmless. A step that takes 1 s and grows quadratically takes
about 4 min at 256x64.

## The journey, step by step

### Step 1: host daemons and the DNS outage (`mitigate.sh`)

**Cause:** several system daemons do work for every new network interface:

- `systemd-resolved` tracks every link;
- `networkd-dispatcher` runs `networkctl list` per interface (O(N^2));
- `systemd-networkd` tries to manage every interface;
- `udev` rules run `systemd-sysctl` and the open-iscsi handler per interface.

Mininet creates tens of thousands of interfaces, and DNS already started
failing at a few hundred.

**Fix:** `extra_scripts/mitigate.sh on` changes the host temporarily, and
`off` restores it:

- it stops `networkd-dispatcher` and `systemd-resolved`, and points
  `/etc/resolv.conf` straight at the upstream DNS servers;
- it tells `systemd-networkd` to leave Mininet interfaces (`s*-eth*`,
  `h*-eth*`) unmanaged;
- it overrides the two udev rules for Mininet interfaces. systemd 249
  ignores the simpler `OPTIONS+="last_rule"` approach, so the rule files
  themselves have to be overridden in `/etc`;
- it disables IPv6 by default on new interfaces.

**Result:** DNS stayed healthy at every size from then on (zero failed
probes). The build barely changed (64x16: 85 s to 80 s), which showed that
the daemons were not why the deploy never finished. Step 6 later added a
udevd change that did help speed.

### Step 2: `tc` and the deploy that never finished (`a04fb79`)

**Cause:** links in this project are `TCLink`s, shaped with Linux traffic
control (`tc`): an `htb` qdisc for the rate and `netem` for delay and queue
size. The `tc` in Ubuntu 22.04 (iproute2 5.15) asks the kernel for the list
of *every* interface each time it runs. The benchmark showed the cost per call:

| Interfaces in namespace | Time per `tc` call |
|---|---|
| 1 | 1.3 ms |
| 1,000 | 7-8 ms |
| 4,000 | 27-30 ms |
| 8,000 | 75-85 ms |
| 16,000 | 146-218 ms |

Mininet runs about 4 `tc` commands per interface when it builds a link. It
runs about 5 more when the switch starts, because adding a port to OVS wipes
its qdiscs and Mininet reapplies them. That is about 9 calls x 33.8k
interfaces, each costing time proportional to the interfaces already there.
In other words O(N^2), and at full size it effectively never finishes. The
staged probe showed it directly: the build cost per interface rose from
about 20 ms at 600 interfaces to 385 ms at 8k.

**Fix:** `tc_batch.py`. `BatchedTCLink`/`BatchedTCIntf` queue the tc commands
for switch interfaces instead of running them. `flush_tc()` then runs the
whole queue as one `tc -batch` file, which lists the interfaces once.
`run_experiment.py` wraps `net.build()` and `net.start()` in `batched_tc()`.
Host interfaces live in their own small namespaces and are unaffected. The
shaping was checked on sampled ports after each run (`htb 5:` and
`netem 10:` present).

- Benchmark: 100 commands at 8k interfaces ran 62x faster as a batch.

**Result (64x16):** build 80 s to 23 s, start 126 s to 10 s. The cost per
interface became flat instead of growing.

### Step 3: ECMP setup (`245b20d`)

**Cause:** `routing/ecmp.py` made 3 separate `ovs-vsctl` calls per switch
(protocols, fail mode, controller). Each `ovs-vsctl` transaction makes
`ovs-vswitchd` re-check every bridge and port before it returns, so the cost
was O(switches x ports). Its flow loading also sent each flow as a separate
message. The private-OVS benchmark showed that this costs time that grows
with the total port count, and that an OpenFlow 1.4 *bundle* (all flows
committed at once) was about 13x faster at 2-9k ports.

**Fix:** all bridges are configured in one chained `ovs-vsctl` transaction
(in chunks of 1000 bridges to stay below the command-line length limit).
Flows are loaded with `ovs-ofctl --bundle`. Bridges now advertise both
OpenFlow 1.3 and 1.4, because bundles need 1.4.

**Result (64x16):** ECMP setup 42 s to 5.7 s.

### Step 4: first complete full-size run, 92 minutes

With steps 1-3 in place, the full 256x64 experiment finished for the first
time. It took 92 minutes, and the phase breakdown showed new targets:

| Phase | Time |
|---|---|
| Build | 6.3 min |
| Switch start | 15.6 min |
| tc reapply and bridge setup | 2.0 min |
| ECMP flows | 6.2 min |
| Static ARP | 3.6 min |
| **Teardown** | **57.6 min** |

Teardown was the biggest surprise. Mininet's `net.stop()` deletes each of the
16,896 links with its own `ip link del` while all OVS bridges still exist,
and each deletion cost about 200 ms.

### Step 5: batching the rest (`f53da57`)

A new module, `mininet_batch.py`, replaces four more Mininet defaults:

| Step | Mininet default | Replacement |
|---|---|---|
| Switch start | one `ovs-vsctl` transaction per switch | `BatchedOVSSwitch`: Mininet's built-in batch startup (a few chained transactions), which raises on failure, unlike Mininet's own |
| Static ARP | `arp -s` for every host pair: 512 x 511 = 262k commands | `static_arp`: one `ip -batch` file of permanent neighbor entries per host |
| ECMP tables | 4+ `ovs-ofctl` calls per switch | each switch's whole table (clear, drop rule, groups, flows) in one atomic `ovs-ofctl bundle` file |
| Link deletion | `ip link del` per link | every link put in one interface group and deleted with a single `ip link del group` |

The link deletion trick deserves a note. Deleting a veth costs the kernel
about 19 ms, mostly waiting for other CPUs to stop using the device. Sending
the deletions in one `ip -batch` does not change that. Deleting a whole
*interface group* lets the kernel unregister all the devices together with a
single wait. Benchmark: 17,000 veths took 44 s that way, against about
5.5 min one by one.

**Result:**

- 64x16: ECMP 5.7 to 1.6 s, static ARP 15 to 0.3 s, stop 71 to 16 s.
- Real 128x32 experiment, together with step 6: total time from 9:31 to
  3:22.

### Step 6: udevd (`mitigate.sh`, extended)

**Cause:** even with the step-1 rule overrides, `systemd-udevd` used about
2,600 CPU-seconds during build and start at 128x32. That is 15-17 of the 32
cores busy just processing the interface events, which pushed the load
average above 24.

**Fix:** `mitigate.sh` also stops `systemd-udevd` and its two sockets while
experiments run, so nothing restarts it in the background. Mininet and OVS do
not need udev for virtual interfaces. The only loss is hotplug handling,
which an experiment VM does not need.

**Result (128x32):** build 89 s to 42 s, start 72 s to 56 s, udevd CPU from
~2,600 CPU-seconds to zero.

### Step 7: an idea that made things worse (reverted)

**Hypothesis:** deleting the OVS bridges would be cheaper if their ports were
already gone, so delete links first, then bridges.

**Result (128x32):** bridge deletion went from 66 s to 119 s. OVS handles
vanished ports worse than bridges it deletes itself. The change was
reverted, and bridges-first stayed.

### Step 8: second full run, 43.6 minutes, and the real OVS limit

| Phase | 92-min run | 43.6-min run |
|---|---|---|
| Build | 6.3 min | 3.2 min |
| Switch start | 15.6 min | 13.2 min |
| ECMP, ARP, tc reapply | 11.8 min | 4.6 min |
| Delete bridges | (in teardown) | 19.8 min |
| Delete links | (in teardown) | 2.1 min |
| **Total** | **92 min** | **43.6 min** |

Two phases were left, both inside `ovs-vswitchd` itself: switch start
(adding 33.6k ports) and bridge deletion (removing them). The daemon adds and
removes ports one at a time in a single-threaded loop, and each port costs
time proportional to the ports already present. Batching the `ovs-vsctl`
commands cannot help, because the per-port work happens inside the daemon.
The logs show it clearly:

- removal ran at about 470 ports per minute at the start and 6,700 per
  minute at the end, as fewer ports remained;
- the 128x32 times multiplied by (33.6k / 8.7k)^2 match the full-size times.

My projection for this run had been 15-17 minutes, because I extrapolated
with a milder growth curve. The full run showed the growth is quadratic.

### Step 9: teardown with ovs-vswitchd stopped (`20a758d`)

**Fix:** if the daemon is the bottleneck, take it out of the way.
`mininet_batch.offline_teardown()` does five things:

1. it stops only `ovs-vswitchd`; the OVS database server keeps running;
2. it removes every bridge from the OVS database (`ovs-vsctl --no-wait`), a
   database change that needs no daemon;
3. it deletes the kernel datapath and all its ports in one call
   (`ovs-dpctl del-dp`);
4. it deletes all links as one interface group;
5. it starts `ovs-vswitchd` again, always, even if a step failed. It comes up
   with an empty configuration.

It also has three safeguards:

- **Foreign bridges:** it runs only if every OVS bridge on the host belongs
  to the experiment, because deleting the datapath would also destroy other
  bridges' ports. Otherwise it uses the normal path.
- **Fallback:** if the offline teardown fails, it falls back to the normal
  path instead of Mininet's slow one.
- **No hang:** Mininet's own shutdown runs an `ovs-vsctl del-br` that waits
  for the daemon, so it skips bridges that are already deleted. Otherwise it
  would hang if the daemon were down.

**A bug found while testing:** the first version used a plain
`systemctl stop ovs-vswitchd`, and systemd followed the dependencies between
the OVS units. `openvswitch-switch` requires the daemon, so it stopped too,
and the database server is part of `openvswitch-switch`, so it stopped as
well. With the database gone, step 2 failed after the datapath had already
been deleted. The restart safeguard recovered everything, but teardown fell
back to the slow path. The fix has three parts:

- stop with `--job-mode=ignore-dependencies`;
- change the database *before* touching the datapath, so a database failure
  destroys nothing;
- fall back cleanly if anything fails.

The retest confirmed that only `ovs-vswitchd` stopped and restarted.

**Result:**

- 128x32: teardown 84 s to 30 s. Bridge removal went from 66 s to 12 s, and
  it now grows linearly.
- Full 256x64: teardown 22.0 min to 3.2 min.

### Step 10: third full run, 26.8 minutes

| Phase | 92-min run | 43.6-min run | Now |
|---|---|---|---|
| Build | 6.3 min | 3.2 min | 3.7 min |
| Switch start | 15.6 min | 13.2 min | 14.9 min |
| ECMP, ARP, tc reapply | 11.8 min | 4.6 min | 4.7 min |
| **Deploy total** | **33.8 min** | **21.0 min** | **23.3 min** |
| **Teardown** | **57.6 min** | **22.0 min** | **3.2 min** |
| **Total** | **92 min** | **43.6 min** | **26.8 min** |

Notes on this run:

- The deploy was 2.3 min slower than the previous run even though the deploy
  code did not change. This is run-to-run variance in OVS's port adds.
- The load average peaked at about 50 during teardown. That is the kernel
  freeing about 34k devices at once (tasks waiting on the kernel count toward
  load). It was brief and did not affect DNS.

A user-started repeat of the same full run took 26.9 min (deploy 23.5 min),
so the result reproduces.

## Does the faster deploy build the same network?

The changes are meant to build exactly the same network, only faster. To
check this, the same small experiment ran on both versions of the code:

- **Before:** commit `0f61b40`, the last commit before any of these changes.
- **After:** this branch.

Both ran with mitigations on, so host conditions were the same. The config
was Azure Clos with 8 leaves, 2 spines and 2 hosts per leaf (16 hosts), with
ECMP, in four runs:

- **hub-a, hub-b:** hub traffic with UDP iperf and a load sweep. 15 flows of
  1 Mbps converge on one 10 Mbps host link, so the tc shaping matters.
- **uniform-a, uniform-b:** 8 random host pairs with UDP iperf, the
  uncongested case.

Each scenario ran twice to measure normal run-to-run noise. All 8 runs
completed. None hit the idle-ping error at this size.

**Identical in both versions:** topology, traffic matrix, ECMP setup
(10 switches, 160 flows, 8 groups) and the iperf flow pairs.

**UDP iperf:**

| Run | Throughput per flow (before → after) | Min RTT (before → after) | Loss / failed flows |
|---|---|---|---|
| hub-a | 0.9914 → 0.9914 Mbps | 0.48 → 0.48 ms | 0 / 0 in both |
| hub-b | 0.9914 → 0.9914 Mbps | 0.48 → 0.48 ms | 0 / 0 in both |
| uniform-a | 0.9914 → 0.9914 Mbps | 0.50 → 0.50 ms | 0 / 0 in both |
| uniform-b | 0.9914 → 0.9914 Mbps | 0.49 → 0.50 ms | 0 / 0 in both |

**Load sweep (hub traffic):** delivered throughput and loss agree within
noise, so rates and queues behave the same. At 100% active leaves:

| | hub-a (before → after) | hub-b (before → after) |
|---|---|---|
| Delivered | 15.7 → 17.9 Mbps | 17.6 → 18.5 Mbps |
| Loss | 88.8 → 87.2% | 87.4 → 86.8% |

The worst-pair `oversubscription_ratio` varies a lot, but as much *within*
one version as between versions. With the old code, the same config gave 32
and 60 at 100%, and 14.0 and 6.8 at 62.5%. The cause is ECMP itself:

- each iperf subflow uses a random source port;
- the switches hash that port to pick a spine.

At 25%, hub-a and hub-b had exactly the same subflows (local ports 43156 and
56537). In one run they landed on different spines and delivered 19.3 Mbps.
In the other they landed on the same spine and delivered 9.7 Mbps.

**Conclusion:** the batched deploy builds an equivalent network, and traffic
results are unchanged. At this small size the new code is only 3-8 s faster
per run (for example, 163 s to 159 s), as expected, because the gains come
from steps whose cost grows with fabric size.

**Note for experiment design:** with ECMP, the worst-pair oversubscription
ratio depends on a few hash outcomes, so a single matrix per active fraction
(the default) is not enough to draw conclusions. Use `matrix_repetitions`
greater than 1, or repeat runs and report the spread.

## Where the time goes now

Switch start, about 15 minutes, is 56% of the run. It is the same OVS limit
as step 8: `ovs-vswitchd` adds ports one at a time, at a cost that grows with
the square of the port count.

The natural next idea mirrors step 9: write every bridge and port into the
OVS database while the daemon is stopped, then start it once. It is
untested, and the daemon may still add the ports one at a time internally,
so it needs a probe test at 64x16 and 128x32 before a full run. If it works,
a full run could drop to about 12-14 minutes.

The same per-switch `ovs-vsctl` pattern from step 3 also exists in
`spraypoint-protocol/spraypoint/ovs.py`. It is unchanged so far, so
Spraypoint routing on very large fabrics would still pay that cost.

## How to run large experiments now

```bash
sudo extra_scripts/mitigate.sh on       # once, before experimenting
sudo /home/rng/mininet-env/bin/python run_experiment.py
sudo extra_scripts/mitigate.sh off      # when done
```

- `extra_scripts/mitigate.sh status` (no root needed) shows the current state.
- If the VM reboots while mitigations are on, run `off` then `on` again.
- If an experiment crashes, clean up with `sudo mn -c`.
- To time a run, prefix the command with `time` and note the `real` line. It
  is printed to the terminal, not into a `tee`'d log. Phase times can also be
  reconstructed from the run directory under `test-results/`:
  - `results.json` has `started_at` and `completed_at`;
  - the load sweep log is created when the deploy ends.

Is `mitigate.sh` required? The experiment runs without it, because all the
deploy and teardown fixes are in the runner. But the udevd step is a real
speedup (step 6), and without the rest DNS fails during large runs. Use it
for anything larger than a few thousand interfaces.

## Appendix: evidence and tools

These are kept on the VM, outside the repo:

| Location | Contents |
|---|---|
| `~/scale-probe/scale_probe.py` | staged probe (phases, daemon CPU, DNS watchdog, per-step timings) |
| `~/scale-probe/tc_bench.sh`, `tc_bench.log` | tc cost vs interface count (no root) |
| `~/scale-probe/ofctl_bench.sh` | private OVS: separate flow mods vs bundle |
| `~/scale-probe/scale-probe-*.jsonl` | every probe run: baseline, mitigated, batched, ecmp-bundle, lifecycle, udevd-off, links-first, offline-teardown(-v2) |
| `~/scale-probe/INVESTIGATION.md` | running log this report is based on |

Real experiment runs (under `test-results/`):

| Run | Size | Total |
|---|---|---|
| `run-20261125T022807793321Z` | 64x16 | 2:18 |
| `run-20261125T024746434597Z` | 128x32 | 9:31 |
| `run-20261125T030509092312Z` | 256x64 | 92 min |
| `run-20261125T170054219218Z` | 128x32 | 3:22 |
| `run-20261125T170714018536Z` | 256x64 | 43.6 min |
| `run-20261125T182724439862Z` | 256x64 | 26.8 min |

Software on the VM: Ubuntu 22.04 (kernel 5.15, systemd 249), iproute2 5.15,
Open vSwitch 2.17, Mininet 2.3.0.dev6. Hardware: 32 cores, 62 GB RAM.
