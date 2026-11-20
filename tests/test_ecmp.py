import unittest

from routing.ovs_batch import run


class OvsBatchCommandTests(unittest.TestCase):
    def test_command_failure_is_reported(self):
        with self.assertRaisesRegex(RuntimeError, "ls /nonexistent-path"):
            run("ls", "/nonexistent-path")


if __name__ == "__main__":
    unittest.main()
