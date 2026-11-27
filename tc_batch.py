"""Batch Mininet's tc commands for root-namespace (switch) interfaces.

Why: iproute2's ``tc`` dumps every network interface (RTM_GETLINK) each time
it runs, so a single ``tc`` call costs time proportional to the number of
interfaces in the namespace (~13 us per interface; ~200 ms at 16k). Mininet
runs about four tc commands per interface when a TCLink is built and about
five more when an OVS switch starts (``TCReapply``, because adding a port to
OVS clears its qdiscs). All OVS switch ports live in the root namespace, so
deploying a large fabric costs O(interfaces^2) in tc alone. ``tc -batch``
lists the interfaces once for the whole file.

Usage::

    net = Mininet(topo=..., link=BatchedTCLink, build=False, ...)
    with batched_tc():
        net.build()
        flush_tc()   # apply link shaping
        net.start()  # OVS add-port wipes qdiscs; Mininet re-queues them
        flush_tc()   # re-apply link shaping

Inside ``batched_tc()``, a ``BatchedTCIntf`` in the root namespace queues the
tc commands Mininet would run instead of running them. Mininet's
``qdisc show`` probe is answered locally: an interface that has not been
configured yet in this session is a fresh veth (noqueue), so nothing needs
deleting; one that was configured before (the TCReapply case) gets a
``qdisc del ... root`` queued first. Such a delete fails harmlessly when OVS
already removed the qdisc, so failures of those lines are ignored; any other
failure raises. Interfaces inside host namespaces, and tc calls made outside
``batched_tc()``, behave exactly like Mininet's TCIntf.
"""

import contextlib
import os
import re
import subprocess
import tempfile

from mininet.link import TCIntf, TCLink


class _State:
    def __init__(self):
        self.lines = []          # queued tc batch lines
        self.queued = set()      # interface names with lines in self.lines
        self.configured = set()  # interface names applied by earlier flushes


_state = None

_FAILED_LINE = re.compile(r"Command failed .*:(\d+)\s*$")


def classify(command):
    """Classify one Mininet tc command given without the leading ``tc``.

    Returns "show" (Mininet's probe of the current root qdisc), "del"
    (Mininet's own clear step) or "queue" (a qdisc/class add to batch).
    """
    command = " ".join(command.split())
    if command.startswith("qdisc show "):
        return "show"
    if command.startswith("qdisc del "):
        return "del"
    if command.startswith(("qdisc add ", "class add ")):
        return "queue"
    raise ValueError(f"Unexpected tc command for batching: {command!r}")


def delete_root_line(interface_name):
    return f"qdisc del dev {interface_name} root"


class BatchedTCIntf(TCIntf):
    """TCIntf that queues root-namespace tc commands while batching."""

    def tc(self, cmd, tc="tc"):
        if _state is None or self.node.inNamespace:
            return super().tc(cmd, tc)
        command = " ".join((cmd % ("", self)).split())
        action = classify(command)
        if action == "show":
            if self.name in _state.configured or self.name in _state.queued:
                _state.lines.append(delete_root_line(self.name))
            # "noqueue" makes TCIntf.config skip its own delete step.
            return "noqueue"
        if action == "queue":
            _state.lines.append(command)
            _state.queued.add(self.name)
        return ""


class BatchedTCLink(TCLink):
    """TCLink whose interfaces are BatchedTCIntf."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("cls1", BatchedTCIntf)
        kwargs.setdefault("cls2", BatchedTCIntf)
        super().__init__(*args, **kwargs)


@contextlib.contextmanager
def batched_tc():
    """Queue root-namespace tc commands until flush_tc() is called."""
    global _state
    if _state is not None:
        raise RuntimeError("batched_tc() is already active")
    _state = _State()
    try:
        yield
        if _state.lines:
            raise RuntimeError(
                f"{len(_state.lines)} queued tc command(s) were never flushed"
            )
    finally:
        _state = None


def unexpected_failures(lines, output):
    """Return tc -batch error messages for lines that are not root deletes.

    tc prints each error message followed by ``Command failed FILE:LINE``.
    """
    failures = []
    message = []
    for text in output.splitlines():
        match = _FAILED_LINE.search(text)
        if not match:
            message.append(text)
            continue
        number = int(match.group(1))
        line = lines[number - 1] if 0 < number <= len(lines) else "?"
        if not line.startswith("qdisc del "):
            failures.append(f"line {number} `{line}`: {' '.join(message)}")
        message = []
    if message and not failures:
        failures.append(" ".join(message))
    return failures


def flush_tc():
    """Run every queued tc command with a single ``tc -force -batch``.

    Returns the number of commands sent. Raises RuntimeError if any command
    other than a root-qdisc delete fails.
    """
    if _state is None:
        raise RuntimeError("flush_tc() called outside batched_tc()")
    lines = _state.lines
    if not lines:
        return 0
    fd, path = tempfile.mkstemp(prefix="mininet-tc-", suffix=".batch")
    try:
        with os.fdopen(fd, "w") as batch_file:
            batch_file.write("\n".join(lines) + "\n")
        result = subprocess.run(
            ["tc", "-force", "-batch", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    finally:
        os.unlink(path)
    failures = unexpected_failures(lines, result.stdout)
    if failures:
        raise RuntimeError(
            f"tc -batch failed for {len(failures)} of {len(lines)} "
            "commands:\n" + "\n".join(failures[:20])
        )
    _state.configured |= _state.queued
    _state.queued = set()
    _state.lines = []
    return len(lines)
