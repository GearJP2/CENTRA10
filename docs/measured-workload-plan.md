# Measured workload plan — frozen before the first run

Threshold screening is complete: B completed-QPS target 0.1, C waiting target
2, and D SkyServe incoming-QPS target 0.1. Their evidence is in the two freeze
records. No additional threshold search is planned. This document specifies the
workload design used by `experiments.measured`; preparing Jobs does not start
the matrix. Freeze these choices before collecting the first measured run.

Use a common baseline **0.5 requests/s** to separate request-count demand from
token work across short, long and mixed inputs. Long calibration and the fixed
three-replica capacity check already used this rate. Its use for short/mixed
is an experimental design choice, not a demonstrated saturation point for
every workload. A lightly loaded short workload is useful: unnecessary scaling
and capacity consumption remain outcomes even if waiting queues stay empty.

| Workload | Target token lengths | Offered traffic | Requests per run |
|---|---|---|---:|
| Short | 100 input / 100 output | 0.5 req/s, 600 s | 300 |
| Long | 6000 input / 200 output | 0.5 req/s, 600 s | 300 |
| Mixed | 80% short, 20% long | Concurrent 0.4 + 0.1 req/s, 600 s | 240 + 60 |
| Bursty | Short | 0.5, 2, 0.5 req/s; 180 s per phase | 90 + 360 + 90 |

These rates and durations use the existing runner's `request_rate=0.5`,
`duration_seconds=600`, `burst_phase_seconds=180` and fixed 4x burst factor.
Arrival gaps remain Poisson; requested duration specifies count/rate and is not
a guarantee of exact wall-clock duration. Each burst phase drains before the
next one starts, as in the current implementation. Describe it as sequential
load phases, not a continuous burst trace. Do not change a workload's rate
after seeing a policy's measured result.

Each workload is applied unchanged to A/B/C/D, with three measured repetitions:
48 unique conditions. A uses one fixed replica; B/C/D start from one warm
replica and may scale to three. The fixed-three diagnostic is not an additional
mode or a replacement for A. Keep model/revision, vLLM image, token lengths,
sampling parameters and lifecycle settings pinned. Report D as a system
reference, including its model download, routing, timeout and readiness
observation differences.

## Before emitting measured Jobs

Use fresh result IDs, immutable code/settings and one Job per condition so a
CloudShell interruption cannot lose a whole matrix. Never run Jobs concurrently
against the shared GPUs. Rotate treatment order across repetitions as the
current runner does. Preserve the actual submitted order and versions.

Use `seed_offset=1000`, giving base seeds **1100, 1200 and 1300** for repetitions
one to three. Add ten per phase and one per concurrent stream, as in the
existing seed construction. These seeds are common across policies for each
workload/repetition and separate from calibration's seed 100. The runner's
`--repeat-start` selects one repetition without replaying earlier ones.
Keep calibration/validation out of measured repeats.
Verify fresh configs against both freeze records before launch. No additional
hardware calibration is required solely to implement this seed separation.

Raw D calibration results have been independently checked in commit `aec8e7c`.
Its eleven D0.1 load-balancer read timeouts are recorded limitations, not a reason
to raise the timeout after selection. Report failure counts and all-attempted
SLO with successful-request p99. Capture load-balancer logs for measured D runs.

## Prepare all, launch one

```bash
.venv/bin/python -m experiments.measured \
  --config config.resume-20261107.local.json \
  --output generated/measured-v1
```

The generator checks pinned model/version fields, reads both threshold freeze
records and writes `plan.json` plus 48 separate bundles. No cluster access is
made during preparation. Each bundle has immutable code/settings and its own
Job, output root, and D service/PodMonitor where applicable. A/B/C bundles
configure the shared serving lifecycle at zero replicas; the Job scales it up.
Applying another bundle while an experiment is active can interrupt that run.
**Never apply this directory recursively.**

Inspect active Jobs, benchmark processes, serving deployment and SkyServe
services before starting. For the first condition only:

```bash
kubectl apply -f generated/measured-v1/m1-short-a-a1/bundle.yaml
kubectl -n centra10 logs -f job/centra10-m1-short-a-a1 -c runner
```

This is measured A-short repetition 1, 300 requests, seed 1100. It is not another
calibration trial. Check completion, cleanup and archived analysis before
proceeding to `m1-short-b-a1`, then C and D, following `plan.json`. Treatment
order is ABCD, BCDA and CDAB across repetitions; this is partial rotation,
not perfect counterbalancing of four treatments with three repetitions.

CloudShell resets do not stop a running Job. Reconnect and inspect the existing
Job; do not create a duplicate. Each Job has no automatic retries and a two-hour
deadline. A failure needs diagnosis and a new attempt ID, retaining the old
artifacts; unfavorable successful results are not rerun. `--attempt 2` prepares
fresh names for every condition, but apply only a condition whose retry was
justified. Keep the same seed and treatment for that retry.

The existing benchmark/operator/reader pods were created with 24-hour sleep
commands. Check their age and process lifetime before a long batch; the Job
deadline does not extend those support pods' lifetime. Do not delete the
operator to renew it while its SkyPilot state or controller resources remain.

## Archive each Job, analyze only measured evidence

The first Job writes EFS directory
`/models/experiment-results/measured-r1-short-A-a1`, containing its own
`versions.json` and `short_A_run1` folder. Each subsequent Job has a distinct
root, so version inventories are not overwritten.

After Job completion, `status: complete` and no cleanup error, archive that
directory through `experiment-results`. Extract measured archives under
`results/measured-v1`, retaining one usable attempt per condition there. Keep
failed attempts elsewhere as failure evidence rather than erasing them.
The analyzer searches nested run directories and rejects duplicate conditions.

```bash
.venv/bin/python -m experiments.analyze results/measured-v1 \
  --output results/measured-v1-analysis
```

For final completeness only, add `--require-complete`; it requires all 48 unique
conditions. Do not mix calibration directories into this analysis root or
interpret early partial summaries as completed comparative findings.
