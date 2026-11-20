import csv
import json
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


def _iperf_json(output, flow_id, log_name):
    start = output.find("{")
    if start < 0:
        raise RuntimeError(
            f"iperf3 returned no JSON result for flow {flow_id}; "
            f"see {log_name}"
        )
    data, _ = json.JSONDecoder().raw_decode(output[start:])
    if "error" in data:
        raise RuntimeError(
            f"iperf3 error for flow {flow_id}: {data['error']}"
        )
    return data


def _iperf_rates(data):
    end = data["end"]
    received = end.get("sum_received") or end["sum"]
    return (
        received["bits_per_second"] / 1e6,
        end["sum"]["bits_per_second"] / 1e6,
    )


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
    port = str(_port(flow_id))
    server_process = server.popen(
        ["iperf3", "-s", "-1", "-p", port],
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
                f"iperf3 UDP server exited before flow {flow_id} started"
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
                "iperf3",
                "-c",
                server.IP(),
                "-u",
                "-p",
                port,
                "-t",
                str(duration_seconds),
                "-b",
                f"{demand_mbps * 1e6:.0f}",
                "-l",
                str(_block_size(demand_mbps)),
                "-J",
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
                f"iperf3 client timed out for flow {flow_id} after "
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

    server_rate, client_rate = _iperf_rates(
        _iperf_json(client_output, flow_id, log_file.name)
    )
    return (
        server_rate,
        client_rate,
        idle_latency,
        _parse_ping_output(ping_output),
    )


PROCESS_KWARGS = {
    "stdout": subprocess.PIPE,
    "stderr": subprocess.STDOUT,
    "text": True,
}


def _port(flow_id):
    return 5201 + flow_id - 1


def _block_size(demand_mbps):
    # iperf3 paces whole datagrams; ~100 packets/s keeps the rate accurate.
    return int(min(1460, max(200, demand_mbps * 1e6 / 800)))


def _ping_command(destination_ip, duration_seconds=None):
    command = ["ping", "-n", "-i", "0.1"]
    if duration_seconds is None:
        command += ["-c", "10"]
    else:
        command += ["-w", str(duration_seconds)]
    return command + [destination_ip]


def measure_idle(net, flows, host_names):
    processes = {
        flow_id: net.get(host_names[flow[0]]).popen(
            _ping_command(net.get(host_names[flow[1]]).IP()), **PROCESS_KWARGS
        )
        for flow_id, flow in flows.items()
    }
    return {
        flow_id: _parse_ping_output(_read_output(process, 5))
        for flow_id, process in processes.items()
    }


def run_flows_concurrently(
    net, flows, host_names, duration_seconds, log_file, idle_latency=None
):
    """Run every flow at once, one port per flow, and return per-flow results."""
    if idle_latency is None:
        idle_latency = measure_idle(net, flows, host_names)

    servers = {
        flow_id: net.get(host_names[destination]).popen(
            ["iperf3", "-s", "-1", "-p", str(_port(flow_id))],
            **PROCESS_KWARGS,
        )
        for flow_id, (_, destination, _) in flows.items()
    }
    clients, pings, outputs = {}, {}, {}
    try:
        time.sleep(0.2)
        for flow_id, (source, destination, demand_mbps) in flows.items():
            if servers[flow_id].poll() is not None:
                raise RuntimeError(
                    f"iperf3 UDP server exited before flow {flow_id} started"
                )
            client = net.get(host_names[source])
            destination_ip = net.get(host_names[destination]).IP()
            clients[flow_id] = client.popen(
                [
                    "iperf3",
                    "-c",
                    destination_ip,
                    "-u",
                    "-p",
                    str(_port(flow_id)),
                    "-t",
                    str(duration_seconds),
                    "-b",
                    f"{demand_mbps * 1e6:.0f}",
                    "-l",
                    str(_block_size(demand_mbps)),
                    "-J",
                ],
                **PROCESS_KWARGS,
            )
            pings[flow_id] = client.popen(
                _ping_command(destination_ip, duration_seconds),
                **PROCESS_KWARGS,
            )
        for flow_id in flows:
            outputs[flow_id] = (
                _read_output(clients[flow_id], duration_seconds + 15),
                _read_output(pings[flow_id], duration_seconds + 5),
            )
    finally:
        for process in (*clients.values(), *pings.values()):
            if process.poll() is None:
                process.kill()
        server_output = {}
        for flow_id, process in servers.items():
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            try:
                server_output[flow_id] = _read_output(process, 5)
            except subprocess.TimeoutExpired:
                process.kill()
                server_output[flow_id] = _read_output(process, 1)

    measured = {}
    for flow_id, (source, destination, demand_mbps) in flows.items():
        client_output, ping_output = outputs[flow_id]
        server_rate, client_rate = _iperf_rates(
            _iperf_json(client_output, flow_id, log_file.name)
        )
        measured[flow_id] = (
            server_rate,
            client_rate,
            idle_latency[flow_id],
            _parse_ping_output(ping_output),
        )
        log_file.write(
            f"--- Flow {flow_id} client output ---\n{client_output}\n"
            f"--- Flow {flow_id} ping output ---\n{ping_output}\n"
            f"--- Flow {flow_id} server output ---\n{server_output[flow_id]}\n"
        )
    return measured


COLUMNS = (
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


def _result(flow_id, source, destination, host_names, demand_mbps, measured):
    server_rate, client_rate, idle_latency, loaded_latency = measured
    return {
        "flow_id": flow_id,
        "source_node": str(source),
        "destination_node": str(destination),
        "source_host": host_names[source],
        "destination_host": host_names[destination],
        "offered_mbps": float(demand_mbps),
        "server_received_rate": server_rate,
        "client_reported_rate": client_rate,
        "idle_latency": idle_latency,
        "loaded_latency": loaded_latency,
    }


def _csv_row(result):
    row = {
        key: value
        for key, value in result.items()
        if key not in {"idle_latency", "loaded_latency"}
    }
    for phase, latency in (
        ("idle", result["idle_latency"]),
        ("loaded", result["loaded_latency"]),
    ):
        row.update(
            {
                f"{phase}_ping_{metric}": latency[metric]
                for metric in PING_METRIC_NAMES
            }
        )
    return row


def run(
    net,
    traffic_matrix,
    host_names,
    duration_seconds,
    output_dir,
    concurrent=False,
):
    """Measure UDP throughput and concurrent ICMP latency for each flow."""
    output_dir = Path(output_dir)
    log_path = output_dir / "iperf.log"
    csv_path = output_dir / "iperf.csv"
    flows = {
        flow_id: (source, destination, float(demand_mbps))
        for flow_id, ((source, destination), demand_mbps) in enumerate(
            traffic_matrix.flows().items(), start=1
        )
    }
    for source, destination, _ in flows.values():
        if source not in host_names or destination not in host_names:
            raise ValueError(
                f"Traffic flow {source!r} -> {destination!r} references "
                "a node without a Mininet host mapping"
            )

    results = []
    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=COLUMNS)
        writer.writeheader()

        if concurrent:
            with log_path.open("w", encoding="utf-8") as log_file:
                print(
                    f"*** iperf3: {len(flows)} concurrent UDP flows "
                    f"({duration_seconds}s)"
                )
                measured = run_flows_concurrently(
                    net, flows, host_names, duration_seconds, log_file
                )
            for flow_id, (source, destination, demand_mbps) in flows.items():
                results.append(
                    _result(
                        flow_id,
                        source,
                        destination,
                        host_names,
                        demand_mbps,
                        measured[flow_id],
                    )
                )
                writer.writerow(_csv_row(results[-1]))
                print(
                    f"*** Results: server "
                    f"{results[-1]['server_received_rate']:.2f} Mbps, client "
                    f"{results[-1]['client_reported_rate']:.2f} Mbps"
                )
            csv_file.flush()
        else:
            for flow_id, (source, destination, demand_mbps) in flows.items():
                source_host = host_names[source]
                destination_host = host_names[destination]
                with log_path.open("a", encoding="utf-8") as log_file:
                    print(
                        f"*** iperf3: testing UDP bandwidth between "
                        f"{source_host} and {destination_host} "
                        f"({duration_seconds}s, {demand_mbps:g} Mbps)"
                    )
                    measured = _run_flow(
                        net.get(source_host),
                        net.get(destination_host),
                        demand_mbps,
                        duration_seconds,
                        log_file,
                        flow_id,
                    )
                results.append(
                    _result(
                        flow_id,
                        source,
                        destination,
                        host_names,
                        demand_mbps,
                        measured,
                    )
                )
                writer.writerow(_csv_row(results[-1]))
                csv_file.flush()
                print(
                    f"*** Results: server "
                    f"{results[-1]['server_received_rate']:.2f} Mbps, client "
                    f"{results[-1]['client_reported_rate']:.2f} Mbps"
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
