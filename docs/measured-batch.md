# Run the measured queue without keeping CloudShell open

The batch controller is a Kubernetes Job on the existing CPU node. It reads the
already prepared `generated/measured-v1/plan.json` and runs its 48 conditions
sequentially. CloudShell restart, browser closure or PC sleep does not stop it.
No new EC2 instances are created.

The controller verifies and skips existing completed individual Jobs such as
A-short/B-short. It waits for an existing current Job such as C-short, verifies
its results after completion, and then continues. New conditions run as child
runner processes inside the batch Job, **not as 48 additional Kubernetes Jobs**.
The original source/settings embedded in each prepared bundle are retained and
used; preparation does not regenerate the treatment configs or seeds.

## Start once

From the repository root, using the existing measured plan:

```bash
.venv/bin/python -m experiments.batch --prepare \
  --plan-root generated/measured-v1 \
  --output generated/measured-batch-v1
kubectl apply -f generated/measured-batch-v1/bundle.yaml
kubectl -n centra10 logs -f job/centra10-measured-batch-v1-a1 -c runner
```

Preparation only writes files. Applying the batch bundle installs the prepared
D PodMonitors/read permissions, a read-only Job-observer Role, immutable code
and compressed-plan ConfigMaps, and one batch Job. It does **not** apply serving
Deployments or scale down a currently running condition. The batch observer
only adds Job get/list permission to the existing experiment-runner identity;
there is no Job deletion or arbitrary Job creation permission.

Once it is running, the terminal can close. Do not manually apply any remaining
individual bundles while the batch owns the queue. Do not delete the cluster,
EFS or support pods before archiving results.

## Checks and stop behavior

Before skipping, the controller compares the stored config with its prepared
config (excluding the runtime tokenizer path), checks successful cleanup, and
runs the existing analysis validator against the raw result. It checks condition
identity/request count. A Kubernetes Job marked Complete alone is insufficient.

Before new work, it requires no other active Job, a zero-replica vLLM Deployment
matching the prepared image/arguments/environment/lifecycle, and no leftover
SkyPilot GPU pods in `default`. A live unrelated Job, failed prepared Job,
unverified result directory, config mismatch or invalid telemetry stops the
queue. The current completed data are preserved. Request failures within a
valid completed experiment are recorded and do not automatically stop or rerun
that condition; this preserves the fixed research selection rules.

An exclusive EFS lock prevents two batch controllers from owning the queue.
Each condition has a two-hour process timeout, and the batch has a 24-hour Job
deadline with no automatic retry. A terminated child may need manual cleanup;
the controller does not hide that failure by moving on. If the CPU node/pod
dies, the last EFS progress and raw results survive but execution is not
automatically resumed. A stale lock requires inspection, not blind deletion.
Normal completion or a caught failure releases the lock.

To resume a diagnosed interruption, prepare a new batch output with
`--attempt 2`; it uses the same measured plan and does not change individual
result IDs or seeds. Completed results with a validated batch completion marker
are skipped even if they did not have individual Kubernetes Jobs. A partial or
failed condition remains a stop until it is reviewed. Do not clear a lock or
launch a second batch while the previous controller or a child is still active.

## After a session restart

Restore EKS endpoint access if CloudShell's IP changed, then inspect rather
than starting another batch:

```bash
kubectl -n centra10 get job centra10-measured-batch-v1-a1
kubectl -n centra10 logs job/centra10-measured-batch-v1-a1 -c runner --tail=20
kubectl -n centra10 exec experiment-results -- python3 -c \
  'import json; p=json.load(open("/models/experiment-results/measured-batch-v1-a1/progress.json")); print("Status:",p["status"]); print("Completed:",len(p["completed"]),"/48"); print("Current:",p["current"]); print("Error:",p.get("error"))'
```

The progress JSON is replaced atomically on EFS at each transition. Per-condition
stdout/stderr is saved as `runner.log` alongside its `versions.json` and raw run
directory. The batch log reports starts, verified skips and completed counts;
it need not stream all the current condition's inference logs to CloudShell.

Results retain the original EFS roots such as `measured-r1-short-A-a1`.
Archive those and `measured-batch-v1-a1` before teardown. Analyze only the chosen
measured attempts with `experiments.analyze --require-complete` at the end;
failed attempts are retained separately, not discarded or counted twice.

The existing benchmark/operator/reader pods still have their original 24-hour
sleep lifetimes. Session restart does not reset those lifetimes. Check their age
before a long batch; EFS durability does not guarantee that a support pod or the
cluster remains available. The batch's 24-hour deadline is a bound, not an
estimate or guarantee of total experiment duration.
