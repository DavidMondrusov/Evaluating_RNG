import unittest

import tc_batch
from tc_batch import (
    BatchedTCIntf,
    batched_tc,
    classify,
    flush_tc,
    unexpected_failures,
)


class FakeNode:
    def __init__(self, in_namespace=False):
        self.inNamespace = in_namespace
        self.commands = []

    def cmd(self, command):
        self.commands.append(command)
        return "qdisc noqueue 0: root refcnt 2\n"


def make_intf(name, in_namespace=False):
    intf = BatchedTCIntf.__new__(BatchedTCIntf)
    intf.name = name
    intf.node = FakeNode(in_namespace)
    return intf


SHOW = "%s qdisc show dev %s"
HTB = "%s qdisc add dev %s root handle 5:0 htb default 1"
CLASS = "%s class add dev %s parent 5:0 classid 5:1 htb rate 5.000000Mbit burst 15k"


class ClassifyTests(unittest.TestCase):
    def test_classifies_mininet_commands(self):
        self.assertEqual(classify(" qdisc show dev s1-eth1"), "show")
        self.assertEqual(classify(" qdisc del dev s1-eth1 root"), "del")
        self.assertEqual(classify(" qdisc add dev s1-eth1 root htb"), "queue")
        self.assertEqual(classify(" class add dev s1-eth1 parent 5:0"), "queue")

    def test_rejects_unknown_commands(self):
        with self.assertRaises(ValueError):
            classify(" filter add dev s1-eth1")


class BatchedIntfTests(unittest.TestCase):
    def test_first_config_queues_adds_without_delete(self):
        intf = make_intf("s1-eth1")
        with batched_tc():
            self.assertEqual(intf.tc(SHOW), "noqueue")
            intf.tc(HTB)
            intf.tc(CLASS)
            self.assertEqual(
                tc_batch._state.lines,
                [
                    "qdisc add dev s1-eth1 root handle 5:0 htb default 1",
                    "class add dev s1-eth1 parent 5:0 classid 5:1 htb "
                    "rate 5.000000Mbit burst 15k",
                ],
            )
            tc_batch._state.lines.clear()
        self.assertEqual(intf.node.commands, [])

    def test_reapply_queues_root_delete_first(self):
        intf = make_intf("s1-eth1")
        with batched_tc():
            tc_batch._state.configured.add("s1-eth1")
            intf.tc(SHOW)
            intf.tc(HTB)
            self.assertEqual(
                tc_batch._state.lines[0], "qdisc del dev s1-eth1 root"
            )
            tc_batch._state.lines.clear()

    def test_namespaced_and_unbatched_interfaces_run_immediately(self):
        host_intf = make_intf("h1-eth0", in_namespace=True)
        switch_intf = make_intf("s1-eth1")
        with batched_tc():
            host_intf.tc(SHOW)
            self.assertEqual(tc_batch._state.lines, [])
        switch_intf.tc(SHOW)
        self.assertEqual(host_intf.node.commands, ["tc qdisc show dev h1-eth0"])
        self.assertEqual(switch_intf.node.commands, ["tc qdisc show dev s1-eth1"])

    def test_unflushed_commands_are_an_error(self):
        intf = make_intf("s1-eth1")
        with self.assertRaisesRegex(RuntimeError, "never flushed"):
            with batched_tc():
                intf.tc(HTB)
        self.assertIsNone(tc_batch._state)

    def test_flush_outside_context_is_an_error(self):
        with self.assertRaises(RuntimeError):
            flush_tc()


class FailureParsingTests(unittest.TestCase):
    LINES = [
        "qdisc del dev s1-eth1 root",
        "qdisc add dev s1-eth1 root handle 5:0 htb default 1",
    ]

    def test_ignores_failed_root_deletes(self):
        output = (
            "Error: Cannot delete qdisc with handle of zero.\n"
            "Command failed /tmp/x.batch:1\n"
        )
        self.assertEqual(unexpected_failures(self.LINES, output), [])

    def test_reports_other_failures(self):
        output = (
            "Error: Exclusivity flag on, cannot modify.\n"
            "Command failed /tmp/x.batch:2\n"
        )
        (failure,) = unexpected_failures(self.LINES, output)
        self.assertIn("line 2", failure)
        self.assertIn("Exclusivity flag on", failure)

    def test_reports_unattributed_output(self):
        self.assertEqual(
            unexpected_failures(self.LINES, "Cannot find device \"x\"\n"),
            ['Cannot find device "x"'],
        )


if __name__ == "__main__":
    unittest.main()
