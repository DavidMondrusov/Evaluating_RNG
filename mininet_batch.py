"""Batched replacements for Mininet steps that cost O(fabric size) per call.

At the 256 leaves x 64 spines scale (~17k links, ~34k OVS ports) these Mininet
defaults dominated a 92 minute run:

* ``OVSSwitch.start`` runs one ovs-vsctl transaction per switch, and each
  transaction makes ovs-vswitchd reconfigure every bridge and port it already
  has (15.6 min). ``BatchedOVSSwitch`` uses Mininet's own batch startup, which
  chains all switches into a few transactions, and fails loudly if one fails.
* ``Mininet.stop`` deletes every link with its own ``ip link del`` while all
  bridges still exist, so each deletion also costs OVS work over all ports
  (~200 ms per link, 58 min). ``stop_network`` deletes the bridges first, then
  every root-namespace link in one kernel operation (interface group delete).
* ``Mininet.staticArp`` runs one ``arp -s`` per host pair (N^2 commands, 3.6 min
  at 512 hosts). ``static_arp`` runs one ``ip -batch`` per host.
"""

import os
import subprocess
import tempfile

from mininet.node import OVSSwitch


# Keeps one ovs-vsctl argument list far below ARG_MAX (~30 bytes per bridge).
BRIDGES_PER_TRANSACTION = 1000
# Interface group used to delete all links at once; unused by anything else
# on a normal host (everything defaults to group 0), checked before use.
TEARDOWN_GROUP = 28014


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


def delete_bridges(switches, run=_run_checked):
    """Delete OVS bridges in as few ovs-vsctl transactions as possible."""
    names = [switch.name for switch in switches]
    for start in range(0, len(names), BRIDGES_PER_TRANSACTION):
        args = ["ovs-vsctl"]
        for name in names[start:start + BRIDGES_PER_TRANSACTION]:
            args += ["--", "--if-exists", "del-br", name]
        run(args, "ovs-vsctl del-br")


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


def stop_network(net):
    """``net.stop()`` with bridges deleted first and links deleted in bulk.

    ``net.stop()`` always runs, so anything the bulk steps did not remove is
    still cleaned up the usual (slow) way.
    """
    try:
        delete_bridges(s for s in net.switches if isinstance(s, OVSSwitch))
        _, errors = delete_links(net)
        if errors:
            print(f"*** Some links were already gone during teardown:\n{errors}")
    finally:
        net.stop()
