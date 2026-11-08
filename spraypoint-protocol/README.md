# Spraypoint on Mininet

A Python library that installs the **Spraypoint** routing protocol (see
[`spraypoint_overview.md`](./spraypoint_overview.md)) as static OpenFlow 1.3
rules on the Open vSwitch switches of a running Mininet network. Each switch
acts as one Spraypoint router. Hosts are the servers attached to it.

- **Spraying:** traffic arriving from a local host port goes to an OpenFlow
  `type=select` group whose buckets are all of the switch's fabric ports.
- **Pointing:** traffic arriving from the fabric matches static
  `ip,nw_dst=<prefix>` rules that forward to the next hops for the switch's
  level (WP0 → t, WPm → WPm-1, IR → WPl, OR → toward IR). When there is more
  than one next hop (`h > 1` or parallel links), the rule uses a shared select
  group.

## Layout

```
spraypoint-protocol/
├── spraypoint/                 # the library
│   ├── params.py               # SpraypointParams(p, h, l, hash_key), l formula
│   ├── fabric.py               # FabricGraph: switches, fabric ports, host ports (no Mininet)
│   ├── levels.py               # WP0..WPl / IR / OR partition, next hops, loop check
│   ├── rules.py                # RuleCompiler -> per-switch Flow/Group objects
│   ├── ovs.py                  # ovs-vsctl / ovs-ofctl -O OpenFlow13 driver (via switch.cmd)
│   ├── mininet_adapter.py      # Mininet net -> FabricGraph, static ARP
│   └── deployer.py             # SpraypointDeployer: the public entry point
├── init_topology_test/
│   ├── init_topology_nodes.py  # Figure 3 edge list
│   └── figure3.py              # Mininet builder + the paper's waypoints/level table
├── verify_spraypoint.py        # entry-point verification script (v2 <-> t)
└── tests/test_spraypoint.py    # offline unit tests + a flow-table packet simulator
```

The protocol logic (`params`, `fabric`, `levels`, `rules`) is plain Python and
does not import Mininet, so you can test it and do dry runs on any machine. Only
`mininet_adapter`, `ovs`, and `deployer` touch Mininet or OVS.

### How a network is read (topology agnostic)

`graph_from_mininet(net)` walks `net.links` and classifies each link by the
node types at its two ends:

| Link | Meaning |
| --- | --- |
| `OVSSwitch` ↔ `OVSSwitch` | fabric link; the OpenFlow port number on each side comes from `switch.ports[intf]` |
| `Host` ↔ `OVSSwitch` | host port on that switch (the "server-facing VRF") |
| anything else | ignored |

It does not matter how the network was built: a `Topo` subclass,
`fnss.to_mininet(...)`, a NetworkX conversion, or `addSwitch`/`addLink` calls.
Parallel links are supported.

### Flow table layout (one table per switch)

| Priority | Match | Action | Role |
| --- | --- | --- | --- |
| 300 | `ip,nw_dst=<local host>/32` | `output:<host port>` | delivery (also covers traffic between hosts on the same switch) |
| 200 | `in_port=<host port>,ip,nw_dst=<dst>` | `group:1` (select, all fabric ports) | source spraying |
| 100 | `ip,nw_dst=<dst>` | `dec_ttl,output:<p>` or `dec_ttl,group:<100+>` | pointing |
| 0 | *(table miss)* | `drop` | everything else, including ARP and IPv6 |

The `in_port` match on host ports does the job of the paper's two VRFs. A
packet is sprayed only when it comes from a local server. If a sprayed packet
comes back to its source over the fabric (for example v2 → w3 → v2), it hits
the pointing rule and goes straight to t. Every rule's **cookie** is the
destination switch's index, so you can filter the dumps by destination (see
below).

### Deterministic "random" choices

Waypoint picks (`p` per node) and next-hop picks (`h` per node) rank the
candidates by `sha256(hash_key | destination | node | purpose | candidate)`.
Every switch computes the same pointing graph, which keeps forwarding loop-free
as the protocol requires. `check_loop_free` checks this for every destination
before any rule is compiled. You can pin waypoint picks for a destination with
`waypoint_overrides`. The verification script does this to reproduce Figure 3.

### Addressing and ARP

Spraypoint routes on L3 only, and switches drop ARP because flooding it on a
cyclic fabric would loop. So by default the deployer installs static ARP
entries for every host pair (`static_arp=True`, which needs the `arp` tool
from `net-tools` in the hosts). Hosts can stay in one flat subnet such as
Mininet's default `10.0.0.0/8`. By default each destination is matched by its
hosts' `/32`s. Pass `subnets={switch: "10.3.0.0/16"}` to match one prefix per
switch, as the Figure 3 builder does.

## Setup

Requirements: Linux, root access, Mininet 2.3+, Open vSwitch 2.8+ (with
OpenFlow 1.3 groups), `iperf3`, and `net-tools`.

```bash
sudo apt install mininet openvswitch-switch iperf3 net-tools
# or, inside an existing Mininet virtualenv (e.g. the repo's /home/rng/mininet-env):
sudo apt install iperf3 net-tools
```

There are no other Python dependencies. Run everything from this directory
(`spraypoint-protocol/`) so that `spraypoint` and `init_topology_test` can be
imported.

## Running the verification script

The script builds Figure 3 (35 switches, one host `h_<switch>` per switch, with
the switch side of that link named `<switch>-host`). It uses `p=2, h=1, l=1`,
installs the rules, prints the levels and the walkthrough, dumps the tables,
and then checks reachability between `v2` and `t`:

```bash
# Selective pair mode (default): rules only for h_v2 <-> h_t
sudo python3 verify_spraypoint.py

# Full mesh: rules toward every host on every switch
sudo python3 verify_spraypoint.py --mode full

# Use hashed waypoint selection instead of the figure's picks
sudo python3 verify_spraypoint.py --waypoints hashed

# Keep the network up afterwards to poke around
sudo python3 verify_spraypoint.py --cli

# No Mininet / no root: print levels and the equivalent ovs-ofctl commands
python3 verify_spraypoint.py --dry-run
```

With the repo's Mininet virtualenv, replace `python3` with
`/home/rng/mininet-env/bin/python`. Other options: `--src/--dst` (switch
names), `-p`, `-H/--hops` (h), `-l`, `--hash-key`, `--iperf-time`,
`--iperf-streams`, `--bw <Mbps>` (TCLink shaping), `-v`.

Checks performed (the exit status is 0 only when all pass):

1. `ping -c 5` from h_v2 to h_t, and from h_t to h_v2, with 0% loss
2. `iperf3 -c ... -P 4 -J` from h_v2 to h_t, with nonzero throughput

The script then prints `dump-group-stats` for v2. The per-bucket counters show
how the spray group spread the iperf3 streams across v2's fabric links.

If Mininet is left in a bad state after an interrupted run, clean up with
`sudo mn -c`.

### Expected levels for Figure 3 and one known difference

With the figure's waypoint picks (v1→{w1,w2}, v2→{w3,w4}, v3→{w5,w6},
v4→{w7,w8}), the computed levels match the overview's table with one
exception. **`o1` is computed as IR, not OR.** In the edge list, o1 is
adjacent to w4 ∈ WP1(t), and IR(t) is defined as the neighbors of WP_l(t)
that are not in a prior level. The library follows the definition, so o1's
pointing rule is `o1 → w4 → v2 → t`. The paper's walkthrough has
`o1 → r6 → w5 → v3 → t`, which treats o1 as OR. The script prints this
difference explicitly. If the figure is meant to be authoritative, the edge
`("w4", "o1")` in `init_topology_nodes.py` is the cause.

The paths from v2 then look like this:

```
v2 sprays to [o1, t, w3, w4]
  via o1 [IR]:  v2 -> o1 -> w4 -> v2 -> t
  via t:        v2 -> t
  via w3 [WP1]: v2 -> w3 -> v2 -> t
  via w4 [WP1]: v2 -> w4 -> v2 -> t
```

## Using the library on your own network

```python
from mininet.net import Mininet
from spraypoint import SpraypointParams, SpraypointDeployer

net = ...            # any Mininet network with OVS switches (FNSS, Topo, NetworkX...)
net.start()

dep = SpraypointDeployer(net, SpraypointParams(p=2, h=1, l=None))  # l=None -> paper formula
dep.deploy_full_mesh()                      # every destination
# dep.deploy_pair("h1", "h7")               # or one host pair, both directions
print(dep.levels("h7").table())             # level partition toward h7's switch
print(dep.dump("s3", destination="h7"))     # groups + flows on s3 for that destination
```

`SpraypointDeployer` options:

| Option | Default | Purpose |
| --- | --- | --- |
| `waypoint_overrides` | `None` | `{dst_switch: {wp_node: [picks]}}` to pin waypoint picks |
| `subnets` | `None` | `{switch: prefix or [prefixes]}` for `nw_dst` matches; `/32` per host otherwise |
| `static_arp` | `True` | install host-to-host ARP entries |
| `detach_controllers` | `True` | `ovs-vsctl del-controller` so no controller adds conflicting flows |
| `dec_ttl` | `True` | decrement TTL on pointing hops as a loop safety net |

Before installing anything, each deployment sets every OVS bridge to
`protocols=OpenFlow10,OpenFlow13`, `stp_enable=false`, and `fail_mode=secure`.
It then clears the bridge's flows and groups, so redeploying (for example from
pair mode to full mesh) starts clean.

## Inspecting OVS flow and group tables

All commands need `sudo` and `-O OpenFlow13`. From the Mininet CLI, prefix them
with `sh`.

```bash
# Groups (spray group 1, pointing groups 100+)
sudo ovs-ofctl -O OpenFlow13 dump-groups v2
sudo ovs-ofctl -O OpenFlow13 dump-group-stats v2     # per-bucket packet/byte counters

# Flows, with port names instead of numbers
sudo ovs-ofctl -O OpenFlow13 --names dump-flows v2

# Only the rules serving one destination (cookie = destination index;
# the script prints it, e.g. 0x13 for t)
sudo ovs-ofctl -O OpenFlow13 dump-flows v2 cookie=0x13/-1

# Port number <-> interface name (v2-host is the host port)
sudo ovs-ofctl -O OpenFlow13 show v2

# Bridge settings applied by the deployer
sudo ovs-vsctl get bridge v2 protocols fail_mode

# Follow one packet through a switch without sending it
sudo ovs-appctl ofproto/trace v2 in_port=1,ip,nw_src=10.3.0.1,nw_dst=10.1.0.1

# Watch the walkthrough switches while traffic runs
for s in v2 w3 w4 o1 t; do echo "== $s"; sudo ovs-ofctl -O OpenFlow13 dump-flows $s cookie=0x13/-1; done
```

`--dry-run` prints the exact `add-group` / `add-flow` commands the deployer
would run, without needing Mininet.

## Tests

```bash
python3 -m unittest discover -v
```

The tests run offline. They cover the l formula, the Figure 3 partition
(including the o1 case), loop freedom for every destination on Figure 3 and on
a random 4-regular expander, and invalid overrides. They also include a small
flow-table simulator that follows **every** spray and ECMP branch of the
compiled rules, and checks that every host pair is delivered in both pair mode
and full mesh mode.
