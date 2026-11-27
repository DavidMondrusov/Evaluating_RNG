# Equivalent comparisons

How to run Azure Clos, Jupiter, RNG and friends so that a difference in the
result can only come from the thing you meant to vary.

## 0. The rule

**One measurement unit is the pair (topology, routing protocol).** Inside a run
both are invariant: a load sweep only changes the traffic matrix (pattern,
active fraction `f`, seed). A comparison is two or more such units, where the
treatment is the one field that differs and every other row of the control
table below matches.

- Topology comparison: treatment = topology, controls = routing, traffic,
  per-ToR capacity, oversubscription, delay/buffer.
- Routing comparison: treatment = routing, controls = topology and everything
  else.

Never put two topologies or two routing protocols in the same curve/CSV.

Paper note: RNG's Figure 13 is a **topology** comparison under a fixed
multipath assumption. ECMP is not that assumption, so an ECMP load sweep is a
routing-and-topology measurement, not Figure 13.

## 1. The control table

| Quantity | Must match | Where it lives |
|---|---|---|
| Traffic nodes | same ToR count | RNG `n`, Azure `leaves`, Jupiter `blocks * tors_per_block` |
| Servers per ToR | same hosts/ToR | `hosts_per_switch`, `hosts_per_leaf`, `hosts_per_tor` |
| Host link rate | same Mbps | `host_link_mbps` (Jupiter hardcodes 10) |
| Host-facing capacity per ToR | `hosts_per_TOR * host_link_mbps` | derived |
| Oversubscription target `r` | same ratio | RNG derived, Azure `leaf_oversubscription`, Jupiter derived |
| Uplink per ToR | `host_capacity / r` Mbps | derived |
| Traffic | same pattern, `f`, seed | `traffic.name`, `demand_mbps`, `seed` |
| Repetitions | same `matrix_repetitions` (>= 10 for a spread) | experiment parameters |
| Delay | same ms | Azure `delay_ms`; RNG/Jupiter/Clos fixed at 1 ms |
| Buffer | same packets | Azure `buffer_packets`; others fixed at 50 |
| Duration | same `duration_seconds` | experiment parameters |
| Routing | same protocol and same `p`, `h`, `l` when it is a control | `routing` |
| Host model | `server` for comparisons | Azure `host_mode` (not `stub`) |

Validation before any run: `Static topology analysis` must show the same ToR
count, same host count, the intended oversubscription, and the same per-ToR
pair capacity. If those four do not match, the comparison is invalid.

## 2. Deriving builder parameters from one point

A point is `(ToRs, hosts_per_tor, host_link_mbps, r)`. Host-facing capacity per
ToR is `H = hosts_per_tor * host_link_mbps`; uplink per ToR must be `H / r`.

### RNG random graph

```
n                = ToRs
hosts_per_switch = hosts_per_tor
host_link_mbps   = host_link_mbps
fabric_link_mbps = hosts_per_tor * host_link_mbps / (r * d)
```

Oversubscription is derived, not entered: `r = hosts_per_switch *
host_link_mbps / (d * fabric_link_mbps)`. Paths per ToR pair `= d`; switches
`= n`. `n * d` must be even and `1 <= d < n`.

### Azure Clos

```
leaves               = ToRs
hosts_per_leaf       = hosts_per_tor
host_link_mbps       = host_link_mbps
leaf_oversubscription = r
spines               = paths per ToR pair
host_mode            = "server"
delay_ms             = 1
buffer_packets       = 50
```

Per-spine link is derived: `spine_link = hosts_per_leaf * host_link_mbps /
(r * spines)`. Switches `= leaves + spines`. This is a 2-tier fabric, so the
leaf ratio is the whole fabric ratio.

### Jupiter direct-connect

All links are hardcoded at 10 Mbps, so only certain points exist:

```
ToR access ratio   = hosts_per_tor / middle_blocks
block egress ratio = tors_per_block * hosts_per_tor / (middle_blocks * (blocks - 1))
```

Both equal `r` when `hosts_per_tor = r * middle_blocks` and
`tors_per_block = blocks - 1`. For `r = 3`, `blocks = 4`, `middle_blocks = 1`,
`tors_per_block = 3`, `hosts_per_tor = 3`. Paths per ToR pair `=
middle_blocks`; switches `= blocks * middle_blocks + blocks * tors_per_block`.
There is no rate knob, so a Jupiter point with a different per-ToR capacity
needs a builder change.

### Clos fat-tree

`hosts_per_leaf` is `k/2`, ToR count is `k^2/2` and host count is `k^3/4`, so
12 ToRs is not buildable (k=4 gives 8 ToRs / 16 hosts, k=6 gives 18 / 54). For
a 3:1 end-to-end fat tree at `host = 10`: `edge_uplink_mbps = 10`,
`aggregation_uplink_mbps = 10 / r`. Do not set every tier to 3:1: per-switch
ratio is not end-to-end ratio in a multi-tier fabric.

### Rail-optimized

Also hardcodes 10 Mbps and has no oversubscription parameter, so it is not
part of an equivalent set yet.

## 3. The 12-ToR, 36-host, 3:1 set

Common point: 12 ToRs, 3 hosts/ToR, 10 Mbps host links, `H = 30` Mbps per ToR,
uplink 10 Mbps per ToR, `r = 3`, matching traffic, ECMP or Spraypoint.

| | RNG | Azure Clos | Jupiter |
|---|---|---|---|
| parameters | `n=12, d=3, hosts_per_switch=3, host_link=10, fabric_link=10/3` | `leaves=12, spines=4, hosts_per_leaf=3, host_link=10, leaf_oversubscription=3, host_mode=server, delay_ms=1, buffer_packets=50` | `blocks=4, middle_blocks=1, tors_per_block=3, hosts_per_tor=3` |
| ToRs / hosts | 12 / 36 | 12 / 36 | 12 / 36 |
| switches | 12 | 16 | 16 |
| paths per ToR pair | 3 | 4 | 1 |
| pair capacity | 10 Mbps | 10 Mbps | 10 Mbps |
| max static ratio | 3.0 | 3.0 | 3.0 |

Ready to run (3 topologies x 2 routings):

```
experiment-config.equivalent-12tor.json
```

Run it as root, because it builds Mininet networks:

```
sudo ~/mininet-env/bin/python3 run_experiment.py --config experiment-config.equivalent-12tor.json
```

All six runs land in one combined folder,
`test-results/batch-<timestamp>-experiment-config.equivalent-12tor/`, with one
subdirectory per run named `<NN>-<run name>/` (for example
`01-rng-ecmp/`). Interactive runs use
`test-results/<test>-<topology>-<routing>-<timestamp>/`, for example
`sweep-rng-ecmp-20261127T205649/`.

The static-only check needs no root: choose `Static topology analysis` in the
menu for each topology and compare the table above.

## 4. What the load sweep now reports

`load-sweep.csv` puts the results first:

```
active_fraction, oversubscription_ratio, min_oversubscription_ratio,
max_oversubscription_ratio, aggregate_offered_to_delivered_ratio,
min_aggregate_offered_to_delivered_ratio, max_aggregate_offered_to_delivered_ratio,
minimum_flow_rate_fraction, delivered_mbps, offered_mbps, sent_mbps,
packet_loss_percent, mean_loaded_rtt_ms, mean_idle_rtt_ms, active_leaf_count,
flow_count, measured_flow_count, failed_flow_count, valid, matrix_repetitions
```

- `valid=False` means at least one repetition had a failed iperf flow. Ratios
  then come from the valid repetitions only; if every flow failed the ratio
  columns are empty. `inf` is never written.
- Read CSVs by column name (old result files still have the old order).

## 5. What to compare, and what not to

Compare:

- **Goodput fraction** `delivered_mbps / offered_mbps` at each `f`, or
  equivalently `1 / aggregate_offered_to_delivered_ratio`. Offered per ToR is
  the same at every point, so this is the cross-topology number.
- **`oversubscription_ratio`** (min per-ToR-pair delivered fraction) only when
  `valid=True` and the transport can use the fabric's paths. With ECMP a flow
  is pinned to one path, so this is a path-limited number, not the topology's
  ratio.
- **Loaded minus idle RTT** (`mean_loaded_rtt_ms - mean_idle_rtt_ms`) only with
  identical delay and buffer settings.
- **Cost**: switch count (and link count) per ToR and per server, from
  `Static topology analysis` (`nodes_by_type["switch"]`, `link_count`).

Do not compare:

- raw `delivered_mbps` across points with different host count or host link
  rate;
- `packet_loss_percent` as an independent metric (fixed-rate UDP makes it
  `1 - goodput`);
- `flow_count` (it changes with `f` and host model by design);
- any row with `valid=False` as if it were a clean measurement.

## 6. Path count is a topology result, not a bug

At the equivalent 12-ToR point, a matching at `f=1` offers 30 Mbps per ToR
pair direction against a 10 Mbps uplink, so the fabric ratio is 3. Observed
ECMP numbers still differ:

- RNG: 3 paths at 3.333 Mbps, 3 flows per pair -> best case about 10 Mbps
  delivered if the flows land on distinct paths.
- Azure: 4 paths at 2.5 Mbps, 3 flows < 4 paths -> best case about 7.5 Mbps,
  `r ~ 4`; hash collisions can idle more capacity.
- Jupiter: 1 path at 10 Mbps, 3 flows share it -> 10 Mbps delivered.

That difference is the treatment (topology), not a configuration error. If you
want to remove the hashing artefact from a topology comparison, either put at
least `max(paths)` flows per ToR pair (for example 4 hosts/ToR against Azure's
4 spines) or compare the static/LP oversubscription instead of the measured
one.

## 7. Pitfalls

- Per-switch maximum ratio is end-to-end ratio only for single-tier fabrics
  (RNG, 2-tier Azure). A 3-tier Clos needs the tier product checked.
- `host_mode="stub"` invents one host per leaf; use `server` for comparisons.
- Jupiter and Rail hardcode 10 Mbps, so they only hit points where
  `hosts_per_TOR` and uplink count are in the target ratio.
- `f` rounds to whole ToRs; matching forces an even active count (12 ToRs:
  `f=0.5` -> 6, `f=0.25` -> 2).
- `matrix_repetitions=1` makes `min == max`; use >= 10 to see the spread.
- Routing parameters are part of the unit: `p`, `h` and `l` must match when
  routing is a control.
- Our Spraypoint installs OVS `type=select` groups, so it selects one path per
  flow; it is not per-packet spraying. Flow count per pair therefore matters.
- Builder defaults differ: Azure is 0.05 ms / 1000 packets; RNG, Jupiter, Clos
  are 1 ms / 50 packets. Match them explicitly.

## 8. Adding another topology or routing

A new topology joins the set only if it can hit the point: expose rate and
oversubscription knobs (or document the derivation), pass the static table
(ToRs, hosts, ratio, pair capacity), and add one run per routing to the
config. A new routing joins only with the same topology and traffic and the
same `p`, `h`, `l` meaning.
