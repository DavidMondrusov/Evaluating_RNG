"""Push compiled rules into Open vSwitch with ``ovs-vsctl`` / ``ovs-ofctl -O OpenFlow13``.

Commands run through ``switch.cmd()`` so they work on any Mininet OVS switch
(OVS switches live in the root namespace, so the commands reach the local
ovsdb/vswitchd). Flows are written to a temporary file and loaded with
``add-flows`` so a switch with thousands of entries costs one command.
"""

from __future__ import annotations

import os
import shlex
import tempfile
from typing import Iterable, List

from .rules import SwitchRules

OFCTL = "ovs-ofctl -O OpenFlow13"


class OVSCommandError(RuntimeError):
    pass


def _run(switch, command: str) -> str:
    # Append the exit status so failures are detectable through switch.cmd().
    out = switch.cmd(f"{command}; echo __rc=$?")
    body, _, rc = out.rpartition("__rc=")
    if rc.strip() != "0":
        raise OVSCommandError(f"[{switch.name}] `{command}` failed:\n{body.strip()}")
    return body


def prepare_switch(switch, detach_controller: bool = True) -> None:
    """Enable OpenFlow 1.3, stop L2 fallback behavior, wipe flows and groups.

    ``fail_mode=secure`` keeps OVS from acting as a learning switch when no
    controller is connected; on a cyclic expander that would flood forever.
    """
    br = shlex.quote(switch.name)
    _run(switch, f"ovs-vsctl set bridge {br} protocols=OpenFlow10,OpenFlow13 "
                 f"stp_enable=false -- set-fail-mode {br} secure")
    if detach_controller:
        _run(switch, f"ovs-vsctl del-controller {br}")
    clear_switch(switch)


def clear_switch(switch) -> None:
    br = shlex.quote(switch.name)
    _run(switch, f"{OFCTL} del-flows {br}")
    _run(switch, f"{OFCTL} del-groups {br}")


def install(switch, rules: SwitchRules) -> None:
    """Install groups first (flows reference them), then all flows in one batch."""
    br = shlex.quote(switch.name)
    for gid in sorted(rules.groups):
        spec = rules.groups[gid].to_ofctl()
        _run(switch, f"{OFCTL} add-group {br} {shlex.quote(spec)}")
    if not rules.flows:
        return
    fd, path = tempfile.mkstemp(prefix=f"spraypoint-{switch.name}-", suffix=".flows")
    try:
        with os.fdopen(fd, "w") as handle:
            for flow in rules.flows:
                handle.write(flow.to_ofctl() + "\n")
        _run(switch, f"{OFCTL} add-flows {br} {shlex.quote(path)}")
    finally:
        os.unlink(path)


def dump_flows(switch, cookie: int = None) -> str:
    br = shlex.quote(switch.name)
    filt = f" cookie={cookie:#x}/-1" if cookie is not None else ""
    return _run(switch, f"{OFCTL} dump-flows {br}{filt}")


def dump_groups(switch) -> str:
    return _run(switch, f"{OFCTL} dump-groups {shlex.quote(switch.name)}")


def dump_group_stats(switch) -> str:
    return _run(switch, f"{OFCTL} dump-group-stats {shlex.quote(switch.name)}")


def render_script(rules_by_switch: Iterable[SwitchRules]) -> List[str]:
    """Shell commands equivalent to :func:`install`, for dry runs and docs."""
    lines: List[str] = []
    for rules in rules_by_switch:
        br = rules.switch
        lines.append(f"# ---- {br}")
        lines.append(f"{OFCTL} del-flows {br}; {OFCTL} del-groups {br}")
        for gid in sorted(rules.groups):
            lines.append(f"{OFCTL} add-group {br} '{rules.groups[gid].to_ofctl()}'")
        for flow in rules.flows:
            note = f"   # {flow.comment}" if flow.comment else ""
            lines.append(f"{OFCTL} add-flow {br} '{flow.to_ofctl()}'{note}")
    return lines
