import csv
import math
from pathlib import Path


SWITCH_LINK_TYPES = {
    "edge": ("edge_leaf", "aggregation_edge"),
    "aggregation": ("aggregation_edge", "core_aggregation"),
}


def _capacity_mbps(attributes, source, target):
    capacity = attributes.get("capacity")
    if (
        not isinstance(capacity, (int, float))
        or isinstance(capacity, bool)
        or not math.isfinite(capacity)
        or capacity <= 0
    ):
        raise ValueError(
            f"Link {source!r} - {target!r} must have a positive finite "
            "capacity in Mbps"
        )
    return float(capacity)


def analyze(topology):
    """Calculate downstream-to-upstream capacity ratios at each switch."""
    switches = []
    for node, node_attributes in topology.nodes(data=True):
        if node_attributes.get("type") != "switch":
            continue

        layer = node_attributes.get("layer")
        if layer == "core":
            continue
        if layer not in SWITCH_LINK_TYPES:
            raise ValueError(
                f"Switch {node!r} has unsupported layer {layer!r}"
            )

        downstream_type, upstream_type = SWITCH_LINK_TYPES[layer]
        downstream_capacity = 0.0
        upstream_capacity = 0.0
        for source, target, link_attributes in topology.edges(node, data=True):
            link_type = link_attributes.get("type")
            capacity = _capacity_mbps(link_attributes, source, target)
            if link_type == downstream_type:
                downstream_capacity += capacity
            elif link_type == upstream_type:
                upstream_capacity += capacity

        if downstream_capacity <= 0 or upstream_capacity <= 0:
            raise ValueError(
                f"Switch {node!r} is missing downstream or upstream links"
            )

        switches.append(
            {
                "node": str(node),
                "layer": layer,
                "downstream_capacity_mbps": downstream_capacity,
                "upstream_capacity_mbps": upstream_capacity,
                "oversubscription_ratio": (
                    downstream_capacity / upstream_capacity
                ),
            }
        )

    if not switches:
        raise ValueError("Topology has no edge or aggregation switches to analyze")

    maximum_ratio = max(
        switch["oversubscription_ratio"] for switch in switches
    )
    return {
        "ratio_definition": (
            "sum of downstream link capacities divided by sum of upstream "
            "link capacities for each edge and aggregation switch"
        ),
        "capacity_unit": "Mbps",
        "maximum_oversubscription_ratio": maximum_ratio,
        "switch_count": len(switches),
        "switches": switches,
    }


def run(topology, output_dir):
    """Write per-switch oversubscription ratios to JSON and CSV results."""
    result = analyze(topology)
    csv_path = Path(output_dir) / "oversubscription.csv"
    columns = (
        "node",
        "layer",
        "downstream_capacity_mbps",
        "upstream_capacity_mbps",
        "oversubscription_ratio",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(result["switches"])

    return {**result, "csv_file": csv_path.name}
