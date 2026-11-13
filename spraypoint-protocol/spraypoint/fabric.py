"""Topology model used by the protocol logic.

The model is independent of Mininet: it only knows switches, the OpenFlow
ports that connect them (fabric ports), and the hosts hanging off each
switch (host ports). :mod:`spraypoint.mininet_adapter` builds it from a live
``Mininet`` object; :meth:`FabricGraph.from_edges` builds one offline for
planning and unit tests.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple


@dataclass(frozen=True)
class HostAttachment:
    """A host plugged into a switch."""

    name: str
    ip: str
    mac: Optional[str]
    switch: str
    port: int  # OpenFlow port number on ``switch``
    port_name: Optional[str] = None


@dataclass
class FabricGraph:
    """Switch-level graph with OpenFlow port numbers.

    ``ports[u][v]`` lists the ports on switch ``u`` that lead to switch ``v``
    (more than one when links are parallel).
    """

    ports: Dict[str, Dict[str, List[int]]] = field(default_factory=dict)
    hosts: Dict[str, List[HostAttachment]] = field(default_factory=dict)
    port_names: Dict[str, Dict[int, str]] = field(default_factory=dict)

    # ----------------------------------------------------------- building
    def add_switch(self, name: str) -> None:
        self.ports.setdefault(name, {})
        self.hosts.setdefault(name, [])
        self.port_names.setdefault(name, {})

    def add_fabric_link(self, u: str, u_port: int, v: str, v_port: int,
                        u_port_name: Optional[str] = None,
                        v_port_name: Optional[str] = None) -> None:
        if u == v:
            raise ValueError(f"self-loop on switch {u!r} is not supported")
        self.add_switch(u)
        self.add_switch(v)
        self.ports[u].setdefault(v, []).append(u_port)
        self.ports[v].setdefault(u, []).append(v_port)
        if u_port_name:
            self.port_names[u][u_port] = u_port_name
        if v_port_name:
            self.port_names[v][v_port] = v_port_name

    def add_host(self, host: HostAttachment) -> None:
        self.add_switch(host.switch)
        self.hosts[host.switch].append(host)
        if host.port_name:
            self.port_names[host.switch][host.port] = host.port_name

    @classmethod
    def from_edges(cls, edges: Iterable[Tuple[str, str]],
                   hosts_per_switch: int = 1,
                   host_ip=None) -> "FabricGraph":
        """Build a graph from switch edges with synthetic port numbers.

        Each switch gets ``hosts_per_switch`` hosts on the lowest ports, then
        fabric ports in edge order. Ports are named ``<source>-<target>``
        (``v2-w3``, ``v2-h_v2``), like the Figure 3 Mininet build.
        ``host_ip(switch, index, k)`` may return
        the IP of the k-th host on a switch; by default 10.0.<index>.<k+1>.
        """
        graph = cls()
        order: List[str] = []
        for u, v in edges:
            for node in (u, v):
                if node not in graph.ports:
                    graph.add_switch(node)
                    order.append(node)
        next_port = {}
        for index, sw in enumerate(order, start=1):
            for k in range(hosts_per_switch):
                ip = host_ip(sw, index, k) if host_ip else f"10.0.{index}.{k + 1}"
                name = f"h_{sw}" if hosts_per_switch == 1 else f"h_{sw}_{k}"
                graph.add_host(HostAttachment(
                    name=name, ip=ip, mac=None, switch=sw, port=k + 1,
                    port_name=f"{sw}-{name}",
                ))
            next_port[sw] = hosts_per_switch + 1
        for u, v in edges:
            pu, pv = next_port[u], next_port[v]
            next_port[u] += 1
            next_port[v] += 1
            graph.add_fabric_link(u, pu, v, pv, f"{u}-{v}", f"{v}-{u}")
        return graph

    # ----------------------------------------------------------- queries
    @property
    def switches(self) -> List[str]:
        return sorted(self.ports)

    def neighbors(self, u: str) -> List[str]:
        return sorted(self.ports[u])

    def fabric_ports(self, u: str) -> List[int]:
        return sorted(p for plist in self.ports[u].values() for p in plist)

    def ports_to(self, u: str, v: str) -> List[int]:
        return sorted(self.ports[u][v])

    def host_ports(self, u: str) -> List[int]:
        return sorted(h.port for h in self.hosts.get(u, []))

    def all_hosts(self) -> List[HostAttachment]:
        return [h for sw in self.switches for h in self.hosts[sw]]

    def host(self, name: str) -> HostAttachment:
        for h in self.all_hosts():
            if h.name == name:
                return h
        raise KeyError(f"unknown host {name!r}")

    def switch_of(self, host_or_switch: str) -> str:
        """Accept a switch name or a host name and return the switch."""
        if host_or_switch in self.ports:
            return host_or_switch
        return self.host(host_or_switch).switch

    def port_label(self, u: str, port: int) -> str:
        name = self.port_names.get(u, {}).get(port)
        return f"{port}({name})" if name else str(port)

    def degree(self, u: str) -> int:
        return len(self.ports[u])

    def average_degree(self) -> float:
        if not self.ports:
            return 0.0
        return sum(self.degree(u) for u in self.ports) / len(self.ports)

    def bfs_distances(self, sources: Iterable[str],
                      allowed: Optional[Set[str]] = None) -> Dict[str, int]:
        """Multi-source BFS. ``allowed`` restricts the nodes that may be visited."""
        dist: Dict[str, int] = {}
        queue: deque = deque()
        for s in sources:
            if allowed is None or s in allowed:
                dist[s] = 0
                queue.append(s)
        while queue:
            u = queue.popleft()
            for v in self.ports[u]:
                if v in dist or (allowed is not None and v not in allowed):
                    continue
                dist[v] = dist[u] + 1
                queue.append(v)
        return dist
