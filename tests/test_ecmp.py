import unittest

from routing.ecmp import _run


class FakeSwitch:
    name = "s1"

    def cmd(self, command):
        return "ovs error: bridge not found\n__rc=1\n"


class EcmpCommandTests(unittest.TestCase):
    def test_command_failure_is_reported(self):
        with self.assertRaisesRegex(RuntimeError, "s1: ovs error: bridge not found"):
            _run(FakeSwitch(), "ovs-ofctl show s1")


if __name__ == "__main__":
    unittest.main()
