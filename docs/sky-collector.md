# SkyServe D collector validation

The manual SkyServe check completed 300/300 requests without errors, scaled
from one to three READY replicas and returned to one after idle. The saved
files are under `results/sky-autoscale-validation`. They establish functionality,
not a complete measurement timeline or a B/C/D comparison.

The next step is one **diagnostic D-short run**, not the 48-run matrix. Prepare
locally, inspect `plan.json`, and apply its bundle only when no other experiment
or benchmark is active:

```bash
python3 -m experiments.sky_runner \
  --config config.resume-20261107.local.json \
  --output generated/sky-collector-1 --attempt 1
cat generated/sky-collector-1/plan.json
kubectl apply -f generated/sky-collector-1/bundle.yaml
kubectl -n centra10 logs -f job/centra10-sky-collector-1 -c runner
```

Preparation does not contact AWS or change the cluster. It generates immutable
code/settings ConfigMaps, a read-only Role/RoleBinding for pods and events in
`default`, a PodMonitor scoped to this service, and one CPU collector Job.
The existing `experiment-runner` ServiceAccount, its bindings/version inventory,
`sky-operator`, `benchmark-client` and `model-cache` PVC must already exist.
The operator must use SkyPilot 0.14.1 with its validated Kubernetes credentials,
networking tools and controller configuration. Both it and the collector must
mount the **same EFS PVC at `/models`**. No extra EC2 nodes are created.

The diagnostic uses 300 short requests at 1 req/s, one to three replicas,
temporary SkyServe target 0.2 req/s/replica, sampling every five seconds by
default, shutdown timeout 120 seconds and pod grace 150 seconds. This is not
D threshold calibration. The frozen B/C thresholds are unchanged.

## Execution and ownership

The Job calls SkyPilot through `kubectl exec sky-operator -c operator`, preserving
the API server, controller identity and state already used for manual validation.
It releases A/B/C serving pods, creates its own service `centra10-d1`, waits for
one warm replica, activates autoscaling with noninteractive confirmation, then
waits for the new version to be READY and the old version to be removed. Only
after another stable warm interval does it start the benchmark.

D traffic and warm-up always use SkyServe's discovered endpoint; an A/B/C
`benchmark_url` cannot redirect D traffic. Benchmarks still run in the CPU
`benchmark-client` with the pinned tokenizer revision and mapped monotonic clock.
Results and generated manifests are shared on EFS, so the operator can read the
same manifest paths as the collector. An existing service with the requested
name causes a failure **without deleting that service**. After a launch attempt
owned by this run, cleanup requests termination and waits for service and pods
to disappear. It does not stop the shared SkyServe controller or EC2 nodes.

The Job has no automatic retries and a two-hour deadline. Failed attempts need
inspection and a new `--attempt` and output directory. CloudShell resets do not
stop the Job. Loss of the operator pod, CPU node, benchmark pod, EFS or cluster
can still interrupt it. Operator SkyPilot state is currently ephemeral; do not
delete/recreate that pod while services or controller resources remain.

Attempt 1 stopped before creating its service because the SkyPilot SDK reports
`ClusterNotUpError: No live services.` instead of an empty list when no controller
is live. The collector now treats only that exact SDK condition as an empty
service inventory; other errors remain failures with their stderr retained.
Keep attempt 1 as failed evidence and prepare attempt 2 in a new directory.

## Measurements

Replica pods carry `centra10-sky-service=<service>`. The PodMonitor scrapes
their named HTTP port and attaches `centra10_service=<service>`, isolating
queue, running requests, completed QPS and optional KV-cache telemetry.
Required missing metrics fail the run; missing optional KV-cache is `null`.

Allocated GPU replicas come from scheduled Pending/Running pods' GPU requests,
including terminating Running pods, as in the A/B/C allocation convention.
READY replicas come from structured SkyPilot SDK status. Kubernetes pod Ready
is insufficient here: SkyPilot can mark SSH ready before vLLM finishes loading.
`queries.json` therefore stores `null` for the two counts derived outside
Prometheus. `pods.json` preserves API snapshots and `scaling.json` preserves
events plus SkyServe version and replica status. Collection calls are sequential;
inspect actual sample spacing and timestamps rather than assuming exact 5 s gaps.

For D, `cold-starts.csv` uses pod `startTime` to **first observed SkyServe READY**
matched by endpoint IP. This is bounded by sampling/status refresh delay, unlike
the A/B/C readiness condition timestamp; interpret cross-system cold starts with
that precision difference. This includes provisioning/setup as far as pod start,
but not time spent requesting a replica before its pod exists. Inspect the raw
controller log and status timeline for that earlier delay. The vLLM shutdown
flag and pod grace are configured equally; graceful drain through SkyPilot/Ray
has not yet been validated under concurrent scale-down traffic.

## Retrieve and verify

```bash
kubectl -n centra10 get job centra10-sky-collector-1
kubectl -n centra10 exec experiment-results -- \
  cat /models/experiment-results/sky-collector-validation-1/short_D_run1/run.json
```

After `status: complete`, no `cleanup_error`, and Job completion:

```bash
mkdir -p results
kubectl -n centra10 exec experiment-results -- \
  tar -C /models/experiment-results -czf - sky-collector-validation-1 \
  > results/sky-collector-validation-1.tar.gz.tmp
tar -tzf results/sky-collector-validation-1.tar.gz.tmp >/dev/null
mv results/sky-collector-validation-1.tar.gz.tmp results/sky-collector-validation-1.tar.gz
test ! -e results/sky-collector-validation-1
tar -xzf results/sky-collector-validation-1.tar.gz -C results
python3 -m experiments.analyze results/sky-collector-validation-1 \
  --output results/sky-collector-validation-1-analysis
```

Check required telemetry, sampling gaps, benchmark errors, READY growth and
cleanup before treating this collector path as live-validated. Retain failures;
do not rerun into an existing result directory. Remove the `centra10-sky-collector`
Role/RoleBinding and validation PodMonitors during final cluster cleanup.
