import unittest

from run_experiment import normalize_run_config
from topologies import jupiter, rail_optimized


def config(topology):
    return normalize_run_config(
        {
            "topology": topology,
            "routing": {"name": "ECMP shortest path"},
            "experiments": [{"name": "Static topology analysis"}],
        },
        "test",
    )


class RailJupiterTests(unittest.TestCase):
    def test_default_sizes(self):
        rail = rail_optimized.build_topology()
        jup = jupiter.build_topology()
        hosts = lambda t: sum(1 for _, a in t.nodes(data=True) if a["type"] == "host")
        self.assertEqual((hosts(rail), rail.number_of_nodes()), (32, 44))
        self.assertEqual((hosts(jup), jup.number_of_nodes()), (32, 56))

    def test_runner_accepts_topologies(self):
        for name in ("Rail-optimized", "Jupiter direct-connect"):
            self.assertEqual(config({"name": name})["topology"]["name"], name)

    def test_runner_rejects_bad_parameters(self):
        with self.assertRaises(ValueError):
            config({"name": "Rail-optimized", "parameters": {"spine_count": 0}})
        with self.assertRaises(ValueError):
            config({"name": "Jupiter direct-connect", "parameters": {"k": 4}})


if __name__ == "__main__":
    unittest.main()
