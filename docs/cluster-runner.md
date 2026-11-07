# Run A/B/C collection independently of CloudShell

`aws/runner.example.yaml` runs one C-long validation as a Kubernetes Job on
an existing CPU node. It uses internal Service DNS for warm-up and telemetry,
executes the benchmark in `benchmark-client`, and writes results under
`/models/experiment-results/full-validation-C-3` on the existing EFS PVC.
It creates no additional AWS instances. CPU/memory contention and EFS writes
are part of this collector placement and must be considered in measurements.
For SkyServe D, use the separate [D collector validation](sky-collector.md),
which reuses the existing SkyPilot operator and keeps its state in that pod.

Before launch, archive evidence for interrupted runs, verify that no other
runner or benchmark process is active, remove its ScaledObject, wait for the
owned HPA to disappear, and scale vLLM to zero. Do not overlap experiments.

From CloudShell in the repository root:

```bash
python3 - <<'PY'
import json
from pathlib import Path
c = json.loads(Path('config.validation-c1.local.json').read_text())
c['kube_context'] = ''
c['k8s_url'] = 'http://vllm-proxy:8000'
c['prometheus_url'] = c['prometheus_internal']
c['benchmark_pod'] = 'benchmark-client'
c['benchmark_url'] = 'http://vllm-proxy:8000'
Path('generated').mkdir(exist_ok=True)
Path('generated/runner-config.json').write_text(json.dumps(c, indent=2) + '\n')
PY
kubectl -n centra10 create configmap runner-code --from-file=experiments/ --dry-run=client -o yaml | kubectl apply -f -
kubectl -n centra10 create configmap runner-settings --from-file=config.json=generated/runner-config.json --from-file=requirements.txt --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f aws/runner.example.yaml
kubectl -n centra10 get pods -l job-name=centra10-runner-c3
```

Keep these ConfigMaps unchanged during a Job. Versioned ConfigMaps or a pinned
runner image are needed for concurrent executions, which this procedure does
not support. Backoff is zero so failed runs are not silently repeated. The
90-minute Job deadline can interrupt cleanup; after failure, inspect benchmark
processes, scaler/HPA and serving pods before restarting. A new run requires a
new Job name and output directory. Do not delete the existing Job to hide a
failure or overwrite the original result directory.

Once its init container completes, follow logs:

```bash
kubectl -n centra10 logs -f job/centra10-runner-c3 -c runner
```

Closing CloudShell does not stop this Kubernetes Job. A changed CloudShell IP
can still prevent viewing logs until EKS endpoint access is updated; it does
not affect the runner's internal API connection. Loss of the CPU node, EFS,
cluster or benchmark pod can still interrupt the experiment. There is no Job
TTL: preserve the Job until results have been archived.

After completion, retrieve the results through the included `experiment-results`
reader pod (it mounts EFS read-only and sleeps for 24 hours):

```bash
kubectl -n centra10 get job centra10-runner-c3
kubectl -n centra10 exec experiment-results -- cat /models/experiment-results/full-validation-C-3/long_C_run1/run.json
mkdir -p results
(set -o pipefail; kubectl -n centra10 exec experiment-results -- tar -C /models/experiment-results -czf - full-validation-C-3 > results/full-validation-C-3.tar.gz)
tar -tzf results/full-validation-C-3.tar.gz
```

Inspect `run.json` and verify `complete` before including the run in analysis.
The collector's files are written to EFS, including intermediate snapshots on
failure. Keep an archive outside EFS before teardown. If the reader has exited,
recreate only that pod; do not recreate the experiment Job. Logs from pip/init
failures may exist only in Kubernetes; archive them before deleting the Job.

The Role permits observing pods/events/controllers, exec into client pods,
scaling vLLM, and managing namespace ScaledObjects. A read-only ClusterRole
allows deployment listing for the existing cross-namespace version inventory;
it grants no secret access. Remove the two cluster-scoped RBAC resources during
teardown, along with the namespace resources. EFS remains independently billed
until explicitly deleted.

## Fixed-three diagnostic

Set `fixed_replicas: 3` in a separate A-only config, with the same long-workload
rate, duration, tokenizer, and seed as B/C validation. The runner scales to and
warms all three before measurement, then releases serving pods on exit. Default
A and smoke remain fixed at one. This diagnostic is not a substitute for A in
the planned 48-run matrix. Keep its output separate and label figures as
fixed-three, since its internal mode field remains A. Use distinct code/settings
ConfigMap names for this Job to preserve the completed B/C configurations.
