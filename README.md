# Evaluating_RNG
CSE589 project

This repo has a wiki where we can place our research, finding, and any considerations we make.

## Running a network experiment

The interactive runner lets you choose a topology, traffic model, and one or
more experiments. The starter setup provides a 4-ary FNSS fat-tree,
reproducible random host-pair traffic, and UDP iperf throughput measurements.
FNSS builds both the topology and traffic matrix; Mininet runs the experiments.

Run Mininet experiments with root privileges and the Python environment that
has FNSS and Mininet installed:

```bash
sudo /home/rng/mininet-env/bin/python /home/rng/Evaluating_RNG/run_experiment.py
```

The prompts accept defaults by pressing Enter. Results are saved in a
timestamped directory under `test-results/`. Each run includes `results.json`
(configuration, topology, software, and measurements), `traffic-matrix.xml`,
`iperf.csv`, and `iperf.log`. The results directory is created automatically
when the runner is started.

### Running a JSON batch

To run several configurations without answering the menus each time, copy and
edit [experiment-config.example.json](./experiment-config.example.json), then
pass it with `--config`:

```bash
sudo /home/rng/mininet-env/bin/python /home/rng/Evaluating_RNG/run_experiment.py \
  --config /home/rng/Evaluating_RNG/experiment-config.example.json
```

The file contains a `runs` array. Each run can set a `name`, topology name and
parameters, traffic model and parameters, and a list of experiments with
their parameters. Currently supported names are `Clos fat-tree`, `Rail-optimized`
(parameters `scalable_units`, `servers_per_unit`, `gpus_per_server`,
`spine_count`; one host per GPU NIC, one leaf per rail per unit),
`Jupiter direct-connect` (parameters `blocks`, `middle_blocks`,
`tors_per_block`, `hosts_per_tor`, `transit_weight`; aggregation blocks in a
uniform mesh with direct and single-transit routing), `Uniform
random host pairs`, and `UDP iperf throughput`. Omitted fields use the same
defaults as the interactive menus (4-ary topology, 8 flows at 1 Mbps, seed 1,
and 5-second iperf duration). Every run gets its own results directory; if a
run fails, the runner records the error and continues with later runs, then
exits with a non-zero status.

UDP iperf runs each selected traffic flow in turn. Each flow is bounded by its
configured duration plus a 15-second timeout; client and server output is kept
in `iperf.log` to diagnose connectivity or iperf errors.
