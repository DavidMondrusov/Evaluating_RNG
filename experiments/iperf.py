import csv
import re
import signal
import subprocess
import time
from pathlib import Path


PING_METRIC_NAMES = (
    "packets_transmitted",
    "packets_received",
    "packet_loss_percent",
    "min_rtt_ms",
    "avg_rtt_ms",
    "max_rtt_ms",
    "mdev_rtt_ms",
)


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


def _parse_ping_output(output):
    packet_summary = re.search(
        r"(\d+)\s+packets transmitted,\s+(\d+)\s+"
        r"(?:packets )?received,.*?([\d.]+)% packet loss",
        output,
    )
    if packet_summary is None:
        raise RuntimeError("ping returned no packet summary")

    transmitted, received, loss_percent = packet_summary.groups()
    latency_summary = re.search(
        r"(?:rtt|round-trip)\s+min/avg/max/(?:mdev|stddev)\s*=\s*"
        r"([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms",
        output,
    )
    return {
        "packets_transmitted": int(transmitted),
        "packets_received": int(received),
        "packet_loss_percent": float(loss_percent),
        "min_rtt_ms": float(latency_summary.group(1))
        if latency_summary
        else None,
        "avg_rtt_ms": float(latency_summary.group(2))
        if latency_summary
        else None,
        "max_rtt_ms": float(latency_summary.group(3))
        if latency_summary
        else None,
        "mdev_rtt_ms": float(latency_summary.group(4))
        if latency_summary
        else None,
    }


def _run_idle_ping(client, destination_ip, log_file, flow_id):
    process = client.popen(
        [
            "ping",
            "-n",
            "-i",
            "0.1",
            "-c",
            "10",
            destination_ip,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        output = _read_output(process, timeout=5)
    except subprocess.TimeoutExpired as error:
        output = error.output or ""
        log_file.write(
            f"--- Flow {flow_id} idle ping output ---\n{output}\n"
        )
        raise TimeoutError(
            f"idle ping latency probe timed out for flow {flow_id}"
        ) from error
    log_file.write(
        f"--- Flow {flow_id} idle ping output ---\n{output}\n"
    )
    return _parse_ping_output(output)


def _run_flow(client, server, demand_mbps, duration_seconds, log_file, flow_id):
    idle_latency = _run_idle_ping(
        client,
        server.IP(),
        log_file,
        flow_id,
    )
    server_process = server.popen(
        ["iperf", "-p", "5001", "-u", "-s"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    client_process = None
    ping_process = None
    server_output = ""
    client_output = ""
    ping_output = ""
    ping_output_collected = False
    try:
        time.sleep(0.2)
        if server_process.poll() is not None:
            server_output = _read_output(server_process, timeout=1)
            raise RuntimeError(
                f"iperf UDP server exited before flow {flow_id} started"
            )

        ping_process = client.popen(
            [
                "ping",
                "-n",
                "-i",
                "0.1",
                "-w",
                str(duration_seconds),
                server.IP(),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
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
        try:
            ping_output = _read_output(
                ping_process,
                timeout=duration_seconds + 5,
            )
            ping_output_collected = True
        except subprocess.TimeoutExpired as error:
            ping_output = error.output or ""
            ping_output_collected = True
            raise TimeoutError(
                f"ping latency probe timed out for flow {flow_id} after "
                f"{duration_seconds + 5} seconds"
            ) from error
    finally:
        if client_process is not None and client_process.poll() is None:
            client_process.kill()
            client_output = _read_output(client_process, timeout=1)
        if ping_process is not None and not ping_output_collected:
            if ping_process.poll() is None:
                ping_process.kill()
            ping_output = _read_output(ping_process, timeout=1)
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
            f"--- Flow {flow_id} ping output ---\n"
            f"{ping_output}\n"
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
    return (
        server_rates[-1],
        client_rates[-1],
        idle_latency,
        _parse_ping_output(ping_output),
    )


def run(net, traffic_matrix, host_names, duration_seconds, output_dir):
    """Measure UDP throughput and concurrent ICMP latency for each flow."""
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
            *(
                f"{phase}_ping_{metric}"
                for phase in ("idle", "loaded")
                for metric in PING_METRIC_NAMES
            ),
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
                server_rate, client_rate, idle_latency, loaded_latency = _run_flow(
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
                "idle_latency": idle_latency,
                "loaded_latency": loaded_latency,
            }
            results.append(result)
            row = {
                key: value
                for key, value in result.items()
                if key not in {"idle_latency", "loaded_latency"}
            }
            for phase, latency in (
                ("idle", idle_latency),
                ("loaded", loaded_latency),
            ):
                row.update(
                    {
                        f"{phase}_ping_{metric}": latency[metric]
                        for metric in PING_METRIC_NAMES
                    }
                )
            writer.writerow(row)
            csv_file.flush()
            print(
                f"*** Results: server {server_rate}, client {client_rate}"
            )

    return {
        "protocol": "UDP",
        "latency_protocol": "ICMP ping",
        "latency_conditions": ["idle_baseline", "during_udp_load"],
        "duration_seconds": duration_seconds,
        "flow_count": len(results),
        "log_file": log_path.name,
        "csv_file": csv_path.name,
        "flows": results,
    }
