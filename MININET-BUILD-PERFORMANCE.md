# Why Mininet builds are slow, and how to fix them

## Symptom

`mininet build` appears to hang. It is not hung — it is creating links one at a
time, about 0.35 s per link. A 128 leaf x 32 spine Azure Clos (4096 leaf-spine
links + 128 host links = 4224 links) needs **~25 min** on this WSL2 box.

## Where the time goes

Per link Mininet runs ~32 subprocesses, and **tc is applied three times per
interface**:

| # | Call site | What happens |
|---|-----------|--------------|
| 1 | `mininet/link.py:65` `Intf.__init__` | `config()` applies tc |
| 2 | `mininet/node.py:1131` `OVSSwitch.attach` | `TCReapply` applies tc again |
| 3 | `mininet/node.py:1238` `batchStartup` | re-applies tc after OVS |

Each pass = `ethtool -K` + `tc qdisc show` + htb qdisc + htb class + netem qdisc
(5 execs), plus `ifconfig up` in `attach`. So 16 execs per interface, 32 per
link: 4224 x 32 = **135k execs**.

Every exec goes through `Node.cmd` -> a persistent `bash -is` in a pty with a
`chr(127)` sentinel prompt (`node.py:140-175`): pty round trip + fork + exec +
`setns`. Measured cost: ~11 ms each, which is the whole 25 minutes.

Passes 2 and 3 exist only because adding a port to OVS resets the qdisc. Only
the last pass survives; the first two are work OVS throws away.

### Not the problem (don't chase these)

- **OVS batching is already on.** `net.py:481` sets `batch=True` for any switch
  class with `batchStartup`, so all `add-port` / `add-br` calls collapse into a
  few large `ovs-vsctl` invocations.
- **CPU / disk / IO.** Build is single-threaded and latency-bound: python ~1%,
  ovs-vswitchd ~5%. More cores do not help.

## Fixes that need no code change

1. **Stop udev while building.** Every veth add fires a uevent; you can see 25
   `systemd-udevd` workers churning. Benchmark first (40 pairs, ~30 s):

   ```bash
   sudo bash -c 'time (for i in $(seq 1 40); do ip link add vb$i type veth peer name vc$i; done); for i in $(seq 1 40); do ip link del vb$i; done'
   sudo systemctl stop systemd-udevd
   # re-run the same benchmark, then restart udev when done
   ```

   Nothing depends on udev for veths — Mininet sets names and MACs explicitly.

2. **Confirm the kernel OVS datapath** (a userspace/netdev fallback is 5-10x
   slower):

   ```bash
   ovs-vsctl get Open_vSwitch . datapath_types
   ovs-appctl dpif/show | head
   ```

3. **Optimise single-core speed, not core count.** 8 fast cores beat 32 slow
   ones. `taskset`/`nice` will not help.

4. **Prefer native Linux over WSL2.** Exec + `setns` is ~11 ms in WSL2 vs 1-3 ms
   on bare metal — 3-4x faster build for the same code.

5. **Skip generic sysctl tuning** (`rmem`/`wmem`, `somaxconn`, backlog). None of
   it is in the build path.

## Fixes in this repo (biggest win, no Mininet edits)

All three keep the exact same shaping (htb rate/r2q + netem delay/limit):

1. **Build with plain `Link`, apply tc once after `net.build()`.** Removes the
   two wasted passes. This is also more correct: tc must be applied *after* the
   port exists in OVS.
2. **Batch the tc pass.** One heredoc per node (<=32 interfaces each) via
   `node.pexec`, e.g.
   ```bash
   printf '%s\n' \
     "qdisc add dev X root handle 5: htb default 1 r2q N" \
     "class add dev X parent 5: classid 5:1 htb rate ..." \
     "qdisc add dev X parent 5:1 handle 10: netem delay 50us limit 1000" \
     | tc -batch -
   ```
   8448 tc cascades become ~288 execs.
3. **Drop `ethtool -K`** on these links. It only matters for TCP
   checksum/segmentation; the load sweep sends 1460-byte UDP.

Result: 32 execs/link -> ~2, i.e. **4224 links in ~2 min here, ~30-60 s on a
fast native VM**, for the same fabric.

Remaining floor: Mininet's one `ip link add` per veth (4224 execs). Bulk
`ip -batch` creation plus per-node tc batches gets the whole build into tens of
seconds, but that is a custom builder, not a knob.

## Sizing cheat sheet

Cost is linear in link count: `links = leaves x spines + hosts` (stub mode:
`hosts = leaves`). At 0.35 s/link on this box:

| leaves | spines | hosts/leaf | links | build |
|--------|--------|-----------|-------|-------|
| 128 | 32 | 2 | 4224 | ~25 min |
| 32 | 8 | 2 | 288 | ~1.7 min |
| 32 | 4 | 2 | 160 | ~1 min |
| 16 | 8 | 2 | 144 | ~50 s |
| 16 | 4 | 2 | 96 | ~35 s |

Leaf oversubscription is preserved automatically:
`spine_link_mbps = hosts_per_leaf * host_mbps / (ratio * spines)`. What changes
when you shrink the fabric is ECMP path diversity (= `spines`) and per-spine-link
bandwidth. Note it in the writeup if you iterate on a smaller fabric.

`Static topology analysis` runs with `requires_network: False`, so it
validates the fabric math instantly — use it before paying any build cost.

## Cleanup (`sudo mn -c`) and teardown

`mn -c` runs `mininet/clean.py:cleanup()` in four steps:

1. `killall` stale controllers/pings/mnexec, then `time.sleep(1)` — fixed ~1-2 s.
2. `ovs-vsctl --if-exists del-br` for **all** bridges in one batched command
   (`clean.py:81-84`). If anything is left, it re-lists and deletes them one at a
   time with `--timeout=1` (`clean.py:86-88`) — that fallback is the slow path.
3. `ip link del` for every `*-ethN` interface, joined 1000 per subshell
   (`clean.py:91-99`).
4. `killprocs('mininet:')` to reap the per-node shells (`clean.py:105`).

Expected duration:

| leftover state | time |
|----------------|------|
| small lab topology | 2-5 s (mostly the fixed `killall` + `sleep 1`) |
| 2000 interfaces | ~10-20 s |
| this run's failure (6216 interfaces, 288 node shells) | ~30-45 s |
| worst case (batched `del-br` fails, 160 bridges one-by-one) | up to ~160 s |

Watch the `*** Removing ...` lines; `ls /sys/class/net | wc -l` should drop
steadily and end at a handful (`lo`, `eth0`, any docker/ovs internals). If it is
still running after ~3 min, the per-bridge fallback or a stuck ovsdb lock is the
cause, not your topology.

A *clean* `net.stop()` is slower than `mn -c`: it deletes each interface through
`Node.cmd` (pty round trip, ~11 ms), so 8448 interfaces is ~1.5-2 min. If you
abort mid-build, `sudo mn -c` is the faster way out.

## Budget the rest of the run too

- Teardown deletes all 4224 links: ~1-2 min.
- Load sweep: 10 active fractions (0.1-1.0) x (idle ping + 5 s iperf over 256
  flows) is ~10-20 min, and on 0.625 Mbps spine links this, not the build, is
  the long pole.
- If a build is interrupted, always `sudo mn -c` before the next attempt.
