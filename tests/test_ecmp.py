import unittest

from routing.ecmp import BRIDGES_PER_TRANSACTION, _configure_bridges, _run


class FakeSwitch:
    name = "s1"

    def cmd(self, command):
        return "ovs error: bridge not found\n__rc=1\n"


class EcmpCommandTests(unittest.TestCase):
    def test_command_failure_is_reported(self):
        with self.assertRaisesRegex(RuntimeError, "s1: ovs error: bridge not found"):
            _run(FakeSwitch(), "ovs-ofctl show s1")


class FakeResult:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


class BridgeConfigurationTests(unittest.TestCase):
    def test_all_bridges_share_one_transaction(self):
        calls = []
        _configure_bridges(
            ["s1", "s2"], run=lambda args, **_: calls.append(args) or FakeResult()
        )
        self.assertEqual(
            calls,
            [[
                "ovs-vsctl",
                "--", "set", "bridge", "s1", "protocols=OpenFlow13,OpenFlow14",
                "--", "set-fail-mode", "s1", "secure",
                "--", "del-controller", "s1",
                "--", "set", "bridge", "s2", "protocols=OpenFlow13,OpenFlow14",
                "--", "set-fail-mode", "s2", "secure",
                "--", "del-controller", "s2",
            ]],
        )

    def test_large_fabrics_are_split_into_bounded_transactions(self):
        calls = []
        bridges = [f"s{i}" for i in range(1, BRIDGES_PER_TRANSACTION + 2)]
        _configure_bridges(
            bridges, run=lambda args, **_: calls.append(args) or FakeResult()
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][4], f"s{BRIDGES_PER_TRANSACTION + 1}")

    def test_failure_is_reported(self):
        with self.assertRaisesRegex(RuntimeError, 'no row "s9"'):
            _configure_bridges(
                ["s9"],
                run=lambda args, **_: FakeResult(1, 'ovs-vsctl: no row "s9"'),
            )


if __name__ == "__main__":
    unittest.main()
