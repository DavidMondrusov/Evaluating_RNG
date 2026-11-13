import sys
from pathlib import Path


DEFAULT_PARAMETERS = {
    "p": 2,
    "h": 1,
    "l": None,
    "hash_key": "spraypoint",
}


def controller_class():
    return None


def load_protocol():
    protocol_root = Path(__file__).resolve().parent.parent / "spraypoint-protocol"
    if not protocol_root.is_dir():
        raise FileNotFoundError(
            f"Spraypoint protocol directory not found: {protocol_root}"
        )
    protocol_path = str(protocol_root)
    if protocol_path not in sys.path:
        sys.path.insert(0, protocol_path)

    from spraypoint import SpraypointDeployer, SpraypointParams

    return SpraypointDeployer, SpraypointParams


def configure(net, parameters):
    SpraypointDeployer, SpraypointParams = load_protocol()

    params = SpraypointParams(**parameters)
    deployer = SpraypointDeployer(net, params)
    installed = deployer.deploy_full_mesh()
    return {
        "deployed": True,
        "switch_count": len(installed),
        "flow_count": sum(len(rules.flows) for rules in installed.values()),
        "group_count": sum(len(rules.groups) for rules in installed.values()),
        "deployment_mode": "full_mesh",
    }
