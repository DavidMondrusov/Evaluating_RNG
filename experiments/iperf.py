import csv
import json
import re
import signal
import subprocess
import time
from collections.abc import Mapping
from contextlib import nullcontext
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


class IperfResultError(RuntimeError):
    """Raised when iperf3 did not produce usable result counters."""


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
        raise IperfResultError(
            f"iperf3 returned no JSON result for flow {flow_id}; "
            f"see {log_name}"
        )
    try:
        data, _ = json.JSONDecoder().raw_decode(output[start:])
    except json.JSONDecodeError as error:
        raise IperfResultError(
            f"iperf3 returned invalid JSON for flow {flow_id}; "
            f"see {log_name}"
        ) from error
    if not isinstance(data, dict):
        raise IperfResultError(
            f"iperf3 returned an unexpected JSON result for flow {flow_id}; "
            f"see {log_name}"
        )
    if "error" in data:
        raise IperfResultError(
            f"iperf3 error for flow {flow_id}: {data['error']}"
        )
    return data


def _iperf_rates(data):
    if not isinstance(data, dict):
        raise IperfResultError("iperf3 result is not a JSON object")
    end = data.get("end")
    if not isinstance(end, dict):
        raise IperfResultError("iperf3 result has no end summary")
    sent = end.get("sum_sent") or end.get("sum")
    server_output = data.get("server_output_json")
    server_end = (
        server_output.get("end")
        if isinstance(server_output, dict)
        else None
    )
    received = end.get("sum_received")
    if isinstance(server_output, dict):
        received_bytes = 0
        received_seconds = 0
        intervals = server_output.get("intervals")
        if isinstance(intervals, list):
            for interval in intervals:
                if not isinstance(interval, dict) or interval.get("omitted"):
                    continue
                interval_sum = interval.get("sum")
                if not isinstance(interval_sum, dict) or interval_sum.get(
                    "omitted"
                ):
                    continue
                bytes_received = interval_sum.get("bytes")
                seconds = interval_sum.get("seconds")
                if (
                    isinstance(bytes_received, (int, float))
                    and not isinstance(bytes_received, bool)
                    and isinstance(seconds, (int, float))
                    and not isinstance(seconds, bool)
                    and seconds > 0
                ):
                    received_bytes += bytes_received
                    received_seconds += seconds
        if received_seconds > 0:
            received = {
                "bits_per_second": (
                    received_bytes * 8 / received_seconds
                )
            }
    if received is None and isinstance(server_end, dict):
        received = server_end.get("sum_received") or server_end.get("sum")
    missing = []
    if not isinstance(received, dict) or not isinstance(
        received.get("bits_per_second"), (int, float)
    ):
        missing.append("receiver sum")
    if not isinstance(sent, dict) or not isinstance(
        sent.get("bits_per_second"), (int, float)
    ):
        missing.append("sum_sent/sum")
    if missing:
        end_fields = ", ".join(sorted(end)) or "none"
        raise IperfResultError(
            "iperf3 result is missing rate summaries "
            f"({', '.join(missing)}); end fields: {end_fields}"
        )
    return (
        received["bits_per_second"] / 1e6,
        sent["bits_per_second"] / 1e6,
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
        ["iperf3", "-s", "-1", "-J", "-p", port],
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
                "--get-server-output",
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
        None,
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
    net, flows, host_names, duration_seconds, log_file, idle_latency=None,
    status=None,
):
    """Run every flow at once, one port per flow, and return per-flow results."""
    if idle_latency is None:
        idle_latency = measure_idle(net, flows, host_names)

    servers = {
        flow_id: net.get(host_names[destination]).popen(
            ["iperf3", "-s", "-1", "-J", "-p", str(_port(flow_id))],
            **PROCESS_KWARGS,
        )
        for flow_id, (_, destination, _) in flows.items()
    }
    if status:
        status.note(f"servers started ({len(servers)})")
    clients, pings, outputs = {}, {}, {}
    client_read_errors = {}
    ping_read_errors = {}
    server_output = {}
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
                    "--get-server-output",
                ],
                **PROCESS_KWARGS,
            )
            pings[flow_id] = client.popen(
                _ping_command(destination_ip, duration_seconds),
                **PROCESS_KWARGS,
            )
        if status:
            status.note(f"clients started ({len(clients)})")
        for flow_id in flows:
            try:
                client_output = _read_output(
                    clients[flow_id], duration_seconds + 15
                )
            except subprocess.TimeoutExpired as error:
                client_output = error.output or ""
                client_read_errors[flow_id] = (
                    f"iperf3 client timed out after "
                    f"{duration_seconds + 15} seconds"
                )
            try:
                ping_output = _read_output(
                    pings[flow_id], duration_seconds + 5
                )
            except subprocess.TimeoutExpired as error:
                ping_output = error.output or ""
                ping_read_errors[flow_id] = (
                    f"ping probe timed out after {duration_seconds + 5} "
                    "seconds"
                )
            outputs[flow_id] = (client_output, ping_output)
        if status:
            status.note(f"flows collected ({len(outputs)})")
    finally:
        for process in (*clients.values(), *pings.values()):
            if process.poll() is None:
                process.kill()
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
        client_exit_code = clients[flow_id].poll()
        log_file.write(
            f"--- Flow {flow_id} details ---\n"
            f"source={source}, destination={destination}, "
            f"offered_mbps={demand_mbps}, duration_seconds={duration_seconds}, "
            f"port={_port(flow_id)}, "
            f"client_exit_code={client_exit_code}, "
            f"client_read_error={client_read_errors.get(flow_id)}, "
            f"ping_exit_code={pings[flow_id].poll()}, "
            f"ping_read_error={ping_read_errors.get(flow_id)}, "
            f"server_exit_code={servers[flow_id].poll()}\n"
            f"--- Flow {flow_id} client output ---\n{client_output}\n"
            f"--- Flow {flow_id} ping output ---\n{ping_output}\n"
            f"--- Flow {flow_id} server output ---\n{server_output[flow_id]}\n"
        )
        log_file.flush()
        failure_reason = client_read_errors.get(flow_id)
        try:
            server_rate, client_rate = _iperf_rates(
                _iperf_json(client_output, flow_id, log_file.name)
            )
        except IperfResultError as error:
            if client_exit_code in (None, 0) and flow_id not in client_read_errors:
                raise
            server_rate = client_rate = 0.0
            failure_reason = failure_reason or str(error)
        if client_exit_code not in (None, 0):
            failure_reason = failure_reason or (
                f"iperf3 client exited with status {client_exit_code}"
            )
        if failure_reason is not None:
            log_file.write(
                f"--- Flow {flow_id} measurement failure ---\n"
                f"client_exit_code={client_exit_code}\n"
                f"reason={failure_reason}\n"
            )
            log_file.flush()
        try:
            loaded_latency = _parse_ping_output(ping_output)
        except RuntimeError as error:
            loaded_latency = {
                metric_name: None for metric_name in PING_METRIC_NAMES
            }
            log_file.write(
                f"--- Flow {flow_id} ping measurement failure ---\n"
                f"reason={ping_read_errors.get(flow_id, str(error))}\n"
            )
            log_file.flush()
        measured[flow_id] = (
            server_rate,
            client_rate,
            idle_latency[flow_id],
            loaded_latency,
            failure_reason,
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
    "flow_failed",
    *(
        f"{phase}_ping_{metric}"
        for phase in ("idle", "loaded")
        for metric in PING_METRIC_NAMES
    ),
)


def _result(flow_id, source, destination, host_names, demand_mbps, measured):
    (
        server_rate,
        client_rate,
        idle_latency,
        loaded_latency,
        failure_reason,
    ) = measured
    return {
        "flow_id": flow_id,
        "source_node": str(source),
        "destination_node": str(destination),
        "source_host": host_names[source],
        "destination_host": host_names[destination],
        "offered_mbps": float(demand_mbps),
        "server_received_rate": server_rate,
        "client_reported_rate": client_rate,
        "flow_failed": failure_reason is not None,
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
    status=None,
):
    """Measure UDP throughput and concurrent ICMP latency for each flow."""
    output_dir = Path(output_dir)
    phase = status.phase if status else (lambda *args, **kwargs: nullcontext())
    log_path = output_dir / "iperf.log"
    csv_path = output_dir / "iperf.csv"
    if isinstance(traffic_matrix, Mapping):
        flows = {
            int(flow_id): (source, destination, float(demand_mbps))
            for flow_id, (source, destination, demand_mbps) in
            traffic_matrix.items()
        }
    else:
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
                with phase("concurrent flows", flows=len(flows)):
                    run_kwargs = {"status": status} if status else {}
                    measured = run_flows_concurrently(
                        net,
                        flows,
                        host_names,
                        duration_seconds,
                        log_file,
                        **run_kwargs,
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
                    with phase(
                        "flow",
                        id=flow_id,
                        source=source_host,
                        destination=destination_host,
                    ):
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
