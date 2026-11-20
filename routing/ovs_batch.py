"""Fast OVS rule install and static ARP for large Mininet networks."""

import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor


def run(*command):
    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(command)}: {result.stdout.strip()}")


def _install_bridge(bridge, groups, flows):
    run("ovs-vsctl", "set", "bridge", bridge, "protocols=OpenFlow13,OpenFlow14",
        "--", "set-fail-mode", bridge, "secure", "--", "del-controller", bridge)
    run("ovs-ofctl", "-O", "OpenFlow13", "del-flows", bridge)
    run("ovs-ofctl", "-O", "OpenFlow13", "del-groups", bridge)
    with tempfile.TemporaryDirectory() as directory:
        if groups:
            path = os.path.join(directory, "groups")
            with open(path, "w") as handle:
                handle.write("\n".join(groups) + "\n")
            run("ovs-ofctl", "-O", "OpenFlow13", "add-groups", bridge, path)
        if flows:
            path = os.path.join(directory, "flows")
            with open(path, "w") as handle:
                handle.write("\n".join(flows) + "\n")
            run("ovs-ofctl", "-O", "OpenFlow14", "--bundle", "add-flows", bridge, path)


def install(rules):
    """Install {bridge: (group specs, flow specs)} on all bridges in parallel."""
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [
            pool.submit(_install_bridge, bridge, groups, flows)
            for bridge, (groups, flows) in rules.items()
        ]
        for future in futures:
            future.result()


def install_static_arp(net):
    """Permanent ARP entries for every other host, one ip -batch per host."""
    entries = [(host.IP(), host.MAC()) for host in net.hosts]
    for host in net.hosts:
        interface = host.defaultIntf().name
        descriptor, path = tempfile.mkstemp()
        try:
            with os.fdopen(descriptor, "w") as handle:
                for ip, mac in entries:
                    if ip != host.IP():
                        handle.write(
                            f"neigh replace {ip} lladdr {mac} dev {interface} nud permanent\n"
                        )
            host.cmd(f"ip -batch {path}")
        finally:
            os.unlink(path)
