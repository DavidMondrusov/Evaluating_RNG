"""Batched replacements for Mininet steps that cost O(fabric size) per call.

At the 256 leaves x 64 spines scale (~17k links, ~34k OVS ports) these Mininet
defaults dominated a 92 minute run:

* ``OVSSwitch.start`` runs one ovs-vsctl transaction per switch, and each
  transaction makes ovs-vswitchd reconfigure every bridge and port it already
  has (15.6 min). ``BatchedOVSSwitch`` uses Mininet's own batch startup, which
  chains all switches into a few transactions, and fails loudly if one fails.
* ``Mininet.stop`` deletes every link with its own ``ip link del`` while all
  bridges still exist, so each deletion also costs OVS work over all ports
  (~200 ms per link, 58 min), and a running ovs-vswitchd removes ports one at
  a time at a cost that grows with the ports present (19.8 min just for the
  bridges). ``stop_network`` stops ovs-vswitchd, drops the kernel datapath in
  one call, removes the bridges from the database, deletes every
  root-namespace link in one kernel operation (interface group delete) and
  restarts ovs-vswitchd.
* ``Mininet.staticArp`` runs one ``arp -s`` per host pair (N^2 commands, 3.6 min
  at 512 hosts). ``static_arp`` runs one ``ip -batch`` per host.
"""

import os
import subprocess
import tempfile
import time

from mininet.node import OVSSwitch
from mininet.util import errRun


# Keeps one ovs-vsctl argument list far below ARG_MAX (~30 bytes per bridge).
BRIDGES_PER_TRANSACTION = 1000
# Interface group used to delete all links at once; unused by anything else
# on a normal host (everything defaults to group 0), checked before use.
TEARDOWN_GROUP = 28014
VSWITCHD_SERVICE = "ovs-vswitchd.service"


def _run_checked(args, what, **kwargs):
    result = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **kwargs,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{what} failed: {result.stdout.strip()}")
    return result.stdout


def _batch_startup_run(command, shell=True):
    """``run`` callback for Mininet's batchStartup that raises on failure."""
    return _run_checked(command, "ovs-vsctl batch startup", shell=shell)


class BatchedOVSSwitch(OVSSwitch):
    """OVSSwitch whose start is merged into a few ovs-vsctl transactions.

    With ``batch=True``, ``start()`` only queues the switch's ovs-vsctl
    command; ``Mininet.start`` then calls ``batchStartup`` once for all
    switches of this class, which runs the queued commands chained (split
    only to respect ARG_MAX) and then reapplies the TCLink shaping that
    add-port clears. Mininet's version ignores ovs-vsctl failures; this one
    raises.
    """

    def __init__(self, name, batch=True, **params):
        super().__init__(name, batch=batch, **params)

    @classmethod
    def batchStartup(cls, switches, run=_batch_startup_run):
        return super().batchStartup(switches, run=run)

    @classmethod
    def batchShutdown(cls, switches, run=errRun):
        """Skip ovs-vsctl for bridges ``delete_bridges`` already removed.

        Mininet's version runs ``ovs-vsctl del-br``, which waits for
        ovs-vswitchd to acknowledge it; that would hang if the daemon were
        not running after an offline teardown.
        """
        remaining = tuple(
            s for s in switches if not getattr(s, "bridge_deleted", False)
        )
        if remaining:
            super().batchShutdown(remaining, run=run)
        for switch in switches:
            if getattr(switch, "bridge_deleted", False):
                switch.terminate()
        return switches


def _write_batch_file(lines):
    fd, path = tempfile.mkstemp(suffix=".batch")
    with os.fdopen(fd, "w") as batch_file:
        batch_file.write("\n".join(lines) + "\n")
    return path


def static_arp(net):
    """Same result as ``Mininet.staticArp``, with one command per host.

    Each host gets a permanent neighbor entry for every other host's IP (what
    ``arp -s`` creates), loaded with ``ip -batch``. Hosts with more than one
    interface keep Mininet's per-entry ``arp -s``, which picks the interface
    by route.
    """
    entries = [(host, host.IP(), host.MAC()) for host in net.hosts]
    for src in net.hosts:
        if len(src.intfList()) != 1:
            for dst, ip, mac in entries:
                if dst is not src:
                    src.setARP(ip=ip, mac=mac)
            continue
        dev = src.defaultIntf().name
        lines = [
            f"neigh replace {ip} lladdr {mac} dev {dev} nud permanent"
            for dst, ip, mac in entries
            if dst is not src
        ]
        if not lines:
            continue
        path = _write_batch_file(lines)
        try:
            output = src.cmd(f"ip -batch {path}; echo __rc=$?")
        finally:
            os.unlink(path)
        body, _, status = output.rpartition("__rc=")
        if status.strip() != "0":
            raise RuntimeError(f"{src.name}: static ARP failed: {body.strip()}")


def delete_bridges(switches, run=_run_checked, no_wait=False):
    """Delete OVS bridges in as few ovs-vsctl transactions as possible.

    no_wait: only update the database (``--no-wait``), for when ovs-vswitchd
    is stopped and so cannot acknowledge the change.
    """
    switches = list(switches)
    for start in range(0, len(switches), BRIDGES_PER_TRANSACTION):
        chunk = switches[start:start + BRIDGES_PER_TRANSACTION]
        args = ["ovs-vsctl"] + (["--no-wait"] if no_wait else [])
        for switch in chunk:
            args += ["--", "--if-exists", "del-br", switch.name]
        run(args, "ovs-vsctl del-br")
        for switch in chunk:
            switch.bridge_deleted = True


def offline_teardown_blocker(net, run=_run_checked):
    """Why the OVS-offline teardown cannot be used here, or None if it can.

    It deletes the whole kernel datapath, so it is only safe when every OVS
    bridge on the host belongs to this network.
    """
    ours = {switch.name for switch in net.switches}
    foreign = sorted(set(run(["ovs-vsctl", "list-br"], "ovs-vsctl list-br")
                         .split()) - ours)
    if foreign:
        return f"OVS has bridges outside this network: {' '.join(foreign)}"
    return None


def offline_teardown(net, run=_run_checked):
    """Remove all bridges, ports and links with ovs-vswitchd stopped.

    A running ovs-vswitchd removes ports one at a time, each costing time
    proportional to the ports still present: deleting the bridges of the
    256x64 fabric (33.6k ports) took 19.8 min. With the daemon stopped,
    ``ovs-dpctl del-dp`` drops the kernel datapath and all its ports at once,
    the bridges are removed from the database only (``--no-wait``), and the
    links are deleted as one interface group. ovs-vswitchd then starts with
    an empty configuration. It is restarted even if a step fails.

    Only ovs-vswitchd is stopped (``--job-mode=ignore-dependencies``): a plain
    ``systemctl stop`` also stops openvswitch-switch.service, which requires
    it, and with it ovsdb-server, which is PartOf that unit. The database is
    changed before the datapath is touched, so if it is unreachable nothing
    has been destroyed and the restarted daemon still has every bridge.

    Returns the time of each step in seconds and any link deletion errors.
    """
    steps = {}

    def timed(name, function, *args, **kwargs):
        start = time.monotonic()
        try:
            return function(*args, **kwargs)
        finally:
            steps[name] = round(time.monotonic() - start, 2)

    switches = [s for s in net.switches if isinstance(s, OVSSwitch)]
    timed("stop_vswitchd_s", run,
          ["systemctl", "stop", "--job-mode=ignore-dependencies",
           VSWITCHD_SERVICE],
          "stopping ovs-vswitchd")
    try:
        timed("delete_bridges_s", delete_bridges, switches, run=run,
              no_wait=True)
        datapaths = run(["ovs-dpctl", "dump-dps"], "ovs-dpctl dump-dps").split()
        for datapath in datapaths:
            timed("del_dp_s", run, ["ovs-dpctl", "del-dp", datapath],
                  "ovs-dpctl del-dp")
        _, steps["link_errors"] = timed("delete_links_s", delete_links, net)
    finally:
        timed("start_vswitchd_s", run,
              ["systemctl", "start", VSWITCHD_SERVICE], "starting ovs-vswitchd")
    return steps


def root_namespace_end(link):
    """The end of ``link`` living in the root namespace, or None.

    Deleting one end of a veth deletes its peer too, even when the peer is in
    a host's namespace.
    """
    for intf in (link.intf1, link.intf2):
        if intf is not None and not intf.node.inNamespace:
            return intf
    return None


def _ip_batch(lines):
    path = _write_batch_file(lines)
    try:
        return subprocess.run(
            ["ip", "-force", "-batch", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ).stdout.strip()
    finally:
        os.unlink(path)


def _group_members(group):
    return subprocess.run(
        ["ip", "-o", "link", "show", "group", str(group)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    ).stdout.strip()


def delete_links(net):
    """Delete all root-namespace links in one kernel operation.

    Every link's root-namespace end is put in ``TEARDOWN_GROUP`` and the whole
    group is deleted with ``ip link del group``. The kernel then unregisters
    them as one batch with a single synchronization, ~2 ms per link instead
    of ~19 ms for separate deletions (44 s vs ~5.5 min at 17k links). If some
    other device already uses that group, it falls back to one ``link del``
    line per link in a single ``ip -batch``.

    Deleted links are removed from ``net.links`` and from their nodes, so a
    later ``net.stop()`` does not try to delete them again. Returns the
    number of links deleted and ip's output for lines that failed (e.g. an
    interface that was already gone); teardown carries on regardless.
    """
    deleted = []
    names = []
    for link in net.links:
        intf = root_namespace_end(link)
        if intf is not None:
            deleted.append(link)
            names.append(intf.name)
    if not names:
        return 0, ""
    if _group_members(TEARDOWN_GROUP):
        errors = _ip_batch([f"link del dev {name}" for name in names])
    else:
        errors = _ip_batch(
            [f"link set dev {name} group {TEARDOWN_GROUP}" for name in names]
            + [f"link del group {TEARDOWN_GROUP}"]
        )
    for link in deleted:
        for intf in (link.intf1, link.intf2):
            intf.node.delIntf(intf)
            intf.link = None
        link.intf1 = link.intf2 = None
    deleted_ids = {id(link) for link in deleted}
    net.links = [link for link in net.links if id(link) not in deleted_ids]
    return len(deleted), errors


def stop_network(net, offline=True):
    """``net.stop()`` with bridges and links deleted in bulk first.

    offline: use ``offline_teardown`` (ovs-vswitchd stopped meanwhile) when
    it is safe; otherwise delete the bridges through the running daemon,
    then the links as one group.

    ``net.stop()`` always runs, so anything the bulk steps did not remove is
    still cleaned up the usual (slow) way. Returns the step timings.
    """
    steps = {}
    try:
        blocker = offline_teardown_blocker(net) if offline else "disabled"
        if blocker is None:
            try:
                steps.update(offline_teardown(net))
            except Exception as error:  # vswitchd was restarted; go online
                blocker = f"offline teardown failed: {error}"
                steps["offline_error"] = str(error)[:500]
        steps["offline"] = blocker is None
        if blocker is not None:
            if offline:
                print(f"*** Not stopping OVS for teardown: {blocker}")
            start = time.monotonic()
            delete_bridges(s for s in net.switches if isinstance(s, OVSSwitch))
            steps["delete_bridges_s"] = round(time.monotonic() - start, 2)
            start = time.monotonic()
            _, steps["link_errors"] = delete_links(net)
            steps["delete_links_s"] = round(time.monotonic() - start, 2)
        if steps.get("link_errors"):
            print("*** Some links were already gone during teardown:\n"
                  f"{steps['link_errors']}")
    finally:
        start = time.monotonic()
        net.stop()
        steps["net_stop_rest_s"] = round(time.monotonic() - start, 2)
    return steps
