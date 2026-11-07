import csv
import re
import signal
import subprocess
import time
from pathlib import Path


def _read_output(process, timeout):
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        stdout, _ = process.communicate()
        raise subprocess.TimeoutExpired(
            error.cmd,
            error.timeout,
            output=stdout,
        ) from error
    return stdout or ""


def _run_flow(client, server, demand_mbps, duration_seconds, log_file, flow_id):
    server_process = server.popen(
        ["iperf", "-p", "5001", "-u", "-s"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    client_process = None
    server_output = ""
    client_output = ""
    try:
        time.sleep(0.2)
        if server_process.poll() is not None:
            server_output = _read_output(server_process, timeout=1)
            raise RuntimeError(
                f"iperf UDP server exited before flow {flow_id} started"
            )

        client_process = client.popen(
            [
                "iperf",
                "-p",
                "5001",
                "-u",
                "-t",
                str(duration_seconds),
                "-c",
                server.IP(),
                "-b",
                f"{demand_mbps:g}M",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            client_output = _read_output(
                client_process,
                timeout=duration_seconds + 15,
            )
        except subprocess.TimeoutExpired as error:
            client_output = error.output or ""
            raise TimeoutError(
                f"iperf client timed out for flow {flow_id} after "
                f"{duration_seconds + 15} seconds"
            ) from error
    finally:
        if client_process is not None and client_process.poll() is None:
            client_process.kill()
            client_output = _read_output(client_process, timeout=1)
        if server_process.poll() is None:
            server_process.send_signal(signal.SIGINT)
        try:
            server_output += _read_output(server_process, timeout=5)
        except subprocess.TimeoutExpired:
            server_process.kill()
            server_output += _read_output(server_process, timeout=1)
        log_file.write(
            f"--- Flow {flow_id} client output ---\n"
            f"{client_output}\n"
            f"--- Flow {flow_id} server output ---\n"
            f"{server_output}\n"
        )

    server_rates = re.findall(r"([\d.]+ \w+/sec)", server_output)
    client_rates = re.findall(r"([\d.]+ \w+/sec)", client_output)
    if not server_rates:
        raise RuntimeError(
            f"iperf server received no reportable UDP data for flow {flow_id}; "
            f"see {log_file.name}"
        )
    if not client_rates:
        raise RuntimeError(
            f"iperf client returned no reportable UDP rate for flow {flow_id}; "
            f"see {log_file.name}"
        )
    return server_rates[-1], client_rates[-1]


def run(net, traffic_matrix, host_names, duration_seconds, output_dir):
    """Run one UDP iperf test for every demand in an FNSS traffic matrix."""
    output_dir = Path(output_dir)
    log_path = output_dir / "iperf.log"
    csv_path = output_dir / "iperf.csv"
    results = []

    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        columns = (
            "flow_id",
            "source_node",
            "destination_node",
            "source_host",
            "destination_host",
            "offered_mbps",
            "server_received_rate",
            "client_reported_rate",
        )
        writer = csv.DictWriter(csv_file, fieldnames=columns)
        writer.writeheader()

        for flow_id, ((source, destination), demand_mbps) in enumerate(
            traffic_matrix.flows().items(), start=1
        ):
            if source not in host_names or destination not in host_names:
                raise ValueError(
                    f"Traffic flow {source!r} -> {destination!r} references "
                    "a node without a Mininet host mapping"
                )

            source_host = host_names[source]
            destination_host = host_names[destination]
            with log_path.open("a", encoding="utf-8") as log_file:
                print(
                    f"*** Iperf: testing UDP bandwidth between "
                    f"{source_host} and {destination_host} "
                    f"({duration_seconds}s, {demand_mbps:g} Mbps)"
                )
                server_rate, client_rate = _run_flow(
                    net.get(source_host),
                    net.get(destination_host),
                    demand_mbps,
                    duration_seconds,
                    log_file,
                    flow_id,
                )

            result = {
                "flow_id": flow_id,
                "source_node": str(source),
                "destination_node": str(destination),
                "source_host": source_host,
                "destination_host": destination_host,
                "offered_mbps": float(demand_mbps),
                "server_received_rate": server_rate,
                "client_reported_rate": client_rate,
            }
            results.append(result)
            writer.writerow(result)
            csv_file.flush()
            print(
                f"*** Results: server {server_rate}, client {client_rate}"
            )

    return {
        "protocol": "UDP",
        "duration_seconds": duration_seconds,
        "flow_count": len(results),
        "log_file": log_path.name,
        "csv_file": csv_path.name,
        "flows": results,
    }
