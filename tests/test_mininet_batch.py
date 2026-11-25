import unittest
from unittest import mock

import mininet_batch
from mininet_batch import (
    BRIDGES_PER_TRANSACTION,
    TEARDOWN_GROUP,
    _batch_startup_run,
    delete_bridges,
    delete_links,
    root_namespace_end,
    static_arp,
)
from routing.ecmp import bundle_lines


class FakeNode:
    def __init__(self, name, in_namespace, ip=None, mac=None):
        self.name = name
        self.inNamespace = in_namespace
        self.intfs_list = []
        self.commands = []
        self.arp = []
        self._ip, self._mac = ip, mac
        self.batch_files = []

    def IP(self):
        return self._ip

    def MAC(self):
        return self._mac

    def intfList(self):
        return self.intfs_list

    def defaultIntf(self):
        return self.intfs_list[0]

    def delIntf(self, intf):
        self.intfs_list.remove(intf)

    def setARP(self, ip, mac):
        self.arp.append((ip, mac))

    def cmd(self, command):
        self.commands.append(command)
        path = command.split()[2].rstrip(";")
        with open(path) as batch_file:
            self.batch_files.append(batch_file.read().splitlines())
        return "__rc=0\n"


class FakeIntf:
    def __init__(self, node, name):
        self.node, self.name = node, name
        self.link = None
        node.intfs_list.append(self)


class FakeLink:
    def __init__(self, intf1, intf2):
        self.intf1, self.intf2 = intf1, intf2
        intf1.link = intf2.link = self


class FakeNet:
    def __init__(self, links=(), hosts=()):
        self.links = list(links)
        self.hosts = list(hosts)


def link(node1, name1, node2, name2):
    return FakeLink(FakeIntf(node1, name1), FakeIntf(node2, name2))


class BundleTests(unittest.TestCase):
    def test_clears_tables_then_adds_groups_before_flows(self):
        lines = bundle_lines(
            {(1, 2): 1},
            ["priority=100,ip,nw_dst=10.0.0.1,actions=group:1"],
        )
        self.assertEqual(
            lines,
            [
                "flow delete",
                "group delete group_id=all",
                "flow add priority=0,actions=drop",
                "group add group_id=1,type=select,"
                "bucket=actions=output:1,bucket=actions=output:2",
                "flow add priority=100,ip,nw_dst=10.0.0.1,actions=group:1",
            ],
        )


class StartupTests(unittest.TestCase):
    def test_failed_batch_startup_raises(self):
        with self.assertRaisesRegex(RuntimeError, "batch startup failed"):
            _batch_startup_run("echo boom; exit 3")

    def test_successful_batch_startup_returns_output(self):
        self.assertEqual(_batch_startup_run("echo ok"), "ok\n")


class TeardownTests(unittest.TestCase):
    def test_bridges_are_deleted_in_bounded_transactions(self):
        calls = []
        switches = [
            FakeNode(f"s{i}", False) for i in range(BRIDGES_PER_TRANSACTION + 1)
        ]
        delete_bridges(switches, run=lambda args, what: calls.append(args))
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0][:5], ["ovs-vsctl", "--", "--if-exists", "del-br", "s0"]
        )
        self.assertEqual(calls[1][1:], ["--", "--if-exists", "del-br",
                                        f"s{BRIDGES_PER_TRANSACTION}"])

    def test_root_namespace_end_prefers_switch_side(self):
        host, switch = FakeNode("h1", True), FakeNode("s1", False)
        host_link = link(host, "h1-eth0", switch, "s1-eth1")
        self.assertEqual(root_namespace_end(host_link).name, "s1-eth1")
        h2, h3 = FakeNode("h2", True), FakeNode("h3", True)
        self.assertIsNone(root_namespace_end(link(h2, "h2-eth0", h3, "h3-eth0")))

    def _delete(self, net, group_members=""):
        batches = []

        def fake_run(args, **_):
            if args[:3] == ["ip", "-o", "link"]:
                return mock.Mock(stdout=group_members)
            with open(args[-1]) as batch_file:
                batches.append((args[:-1], batch_file.read().splitlines()))
            return mock.Mock(stdout="")

        with mock.patch.object(mininet_batch.subprocess, "run", fake_run):
            result = delete_links(net)
        return result, batches

    def _fabric(self):
        s1, s2, h1 = FakeNode("s1", False), FakeNode("s2", False), FakeNode("h1", True)
        h2, h3 = FakeNode("h2", True), FakeNode("h3", True)
        fabric = link(s1, "s1-eth1", s2, "s2-eth1")
        host_link = link(h1, "h1-eth0", s1, "s1-eth2")
        namespaced = link(h2, "h2-eth0", h3, "h3-eth0")
        return FakeNet([fabric, host_link, namespaced]), (s1, h1, fabric, namespaced)

    def test_links_deleted_as_one_group_and_forgotten(self):
        net, (s1, h1, fabric, namespaced) = self._fabric()
        result, batches = self._delete(net)
        group = TEARDOWN_GROUP
        self.assertEqual(result, (2, ""))
        self.assertEqual(
            batches,
            [(["ip", "-force", "-batch"],
              [f"link set dev s1-eth1 group {group}",
               f"link set dev s1-eth2 group {group}",
               f"link del group {group}"])],
        )
        self.assertEqual(net.links, [namespaced])
        self.assertEqual(s1.intfs_list, [])
        self.assertEqual(h1.intfs_list, [])
        self.assertIsNone(fabric.intf1)

    def test_group_already_in_use_falls_back_to_per_link_deletes(self):
        net, _ = self._fabric()
        result, batches = self._delete(net, group_members="7: eth9: <UP>")
        self.assertEqual(result, (2, ""))
        self.assertEqual(
            batches[0][1], ["link del dev s1-eth1", "link del dev s1-eth2"]
        )


class StaticArpTests(unittest.TestCase):
    def test_one_batch_per_single_homed_host(self):
        switch = FakeNode("s1", False)
        hosts = [
            FakeNode(f"h{i}", True, ip=f"10.0.0.{i}", mac=f"00:00:00:00:00:0{i}")
            for i in (1, 2, 3)
        ]
        for i, host in enumerate(hosts, start=1):
            link(host, f"h{i}-eth0", switch, f"s1-eth{i}")
        static_arp(FakeNet(hosts=hosts))
        self.assertEqual(len(hosts[0].commands), 1)
        self.assertEqual(
            hosts[0].batch_files,
            [[
                "neigh replace 10.0.0.2 lladdr 00:00:00:00:00:02 "
                "dev h1-eth0 nud permanent",
                "neigh replace 10.0.0.3 lladdr 00:00:00:00:00:03 "
                "dev h1-eth0 nud permanent",
            ]],
        )

    def test_multi_homed_hosts_use_arp(self):
        switch = FakeNode("s1", False)
        h1 = FakeNode("h1", True, ip="10.0.0.1", mac="m1")
        h2 = FakeNode("h2", True, ip="10.0.0.2", mac="m2")
        link(h1, "h1-eth0", switch, "s1-eth1")
        link(h1, "h1-eth1", switch, "s1-eth2")
        link(h2, "h2-eth0", switch, "s1-eth3")
        static_arp(FakeNet(hosts=[h1, h2]))
        self.assertEqual(h1.arp, [("10.0.0.2", "m2")])
        self.assertEqual(h1.commands, [])

    def test_failure_is_reported(self):
        switch = FakeNode("s1", False)
        h1 = FakeNode("h1", True, ip="10.0.0.1", mac="m1")
        h2 = FakeNode("h2", True, ip="10.0.0.2", mac="m2")
        link(h1, "h1-eth0", switch, "s1-eth1")
        link(h2, "h2-eth0", switch, "s1-eth2")
        h1.cmd = lambda command: "RTNETLINK answers: Invalid argument\n__rc=1\n"
        with self.assertRaisesRegex(RuntimeError, "h1: static ARP failed"):
            static_arp(FakeNet(hosts=[h1, h2]))


if __name__ == "__main__":
    unittest.main()
