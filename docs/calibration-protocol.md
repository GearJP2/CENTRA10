# B/C calibration protocol — bounded screening

This protocol is prospective for the four trials below. Earlier validation and
fixed-three runs informed its design; they are not independent confirmation.
It selects one global threshold per policy for later workload comparisons.
It does not establish optimal thresholds or equal policy expressiveness.

## Revision 2 amendment: common bounded draining

The original B0.2 and C1 trials are retained as original-lifecycle screening;
they must not be ranked with revision-2 trials. The C1 run had ten truncated
streams during a 3-to-1 downscale with running work. Controlled probes then
observed failure with shutdown timeout 0 / pod grace 30 seconds and a complete
512-token stream with timeout 120 / grace 150 seconds. Evidence:
`results/drain-control-0-1` and `results/drain-control-120-1`.

Revision 2 applies `--shutdown-timeout 120` and
`terminationGracePeriodSeconds: 150` to **both B and C**, retaining the original
trigger definitions and HPA stabilization settings. Rerun all four candidates
below with distinct `calibration-r2-...` IDs; the original screening is not
silently replaced. The short drain probe does not guarantee completion of all
queued long requests. Record every failure and any termination-budget expiry.
No per-policy lifecycle tuning is permitted.

Prepare all four frozen bundles without contacting the cluster:

```bash
.venv/bin/python -m experiments.calibration \
  --config config.validation-c1.local.json \
  --output generated/calibration-r2
```

The printed plan maps each bundle to its Job/output name. Apply only one at a
time, in order `b02.yaml`, `c1.yaml`, `c2.yaml`, `b01.yaml`, after confirming the
previous Job and cleanup completed and there is no competing scaler/runner.
Each bundle explicitly applies the shared serving settings and uses its own
immutable code/settings ConfigMaps. It assumes the existing runner service
account/bindings, proxy, PVC, benchmark-client and result reader. It creates
no new AWS instances. Preparing again requires a new local directory;
repeating a failed trial also requires new Job/output/ConfigMap names, not
reapplying a completed Job. Archive results before deleting resources.

## Evidence and operating point

Verified saved fixed-three runs all had three ready replicas throughout:

| Offered rate | Duration | Requests | TTFT p50 | TTFT p99 | 2-second SLO attainment |
|---|---:|---:|---:|---:|---:|
| 0.2 req/s | 300 s | 60 | 1.740 s | 3.239 s | 91.67% |
| 0.5 req/s | 300 s | 150 | 1.837 s | 4.450 s | 72.67% |
| 1 req/s | 600 s | 600 | 7.634 s | 39.125 s | 16.33% |

Different durations and one run per rate limit capacity inference. These are
not matched repetitions. The middle rate is a candidate operating point, not
a demonstrated sustainable-capacity bound or application-approved SLO regime.
Even fixed-three misses the deadline for some low-rate requests.

## Four-trial budget

All four use long requests (target 6000 input / 200 output tokens), 0.5 req/s,
600 seconds (300 requests), seed 100 (repeat 1), and the in-cluster collector.
Each starts from one warm ready replica, can scale to three, and uses the same
existing B/C timing and routing settings. Fixed-three is not a calibration
competitor and `fixed_replicas` must be 1. No other experiment may overlap.

| Order | Trial ID | Mode | Scaling target |
|---|---|---|---|
| 1 | calibration-r2-B-qps02-long-1 | B | 0.2 completed req/s |
| 2 | calibration-r2-C-queue1-long-1 | C | 1 waiting request |
| 3 | calibration-r2-C-queue2-long-1 | C | 2 waiting requests |
| 4 | calibration-r2-B-qps01-long-1 | B | 0.1 completed req/s |

This gives each policy two candidates at the same load and duration. The
B-C-C-B order partly balances session order but does not eliminate cache,
startup, temporal, or seed effects. This is a small screening budget; one run
per candidate cannot establish robust ranking. Record every candidate in
`docs/calibration.csv`, including failures. Keep output names unique.

## Selection rule, fixed before these trials

1. A usable trial must have `run.json` status complete, successful cleanup,
   valid benchmark timings, and complete required telemetry. Request failures
   remain counted against SLO attainment; do not discard an unfavorable trial.
   Infrastructure interruptions are retained separately and may be retried
   once with a new ID and the same candidate, after diagnosing the fault.
2. Within each policy, select the candidate with higher all-attempted-request
   two-second TTFT SLO attainment. If attainment differs by no more than two
   percentage points, prefer lower measured allocated GPU-hours. Break any
   remaining tie with lower TTFT p99, then lower numeric threshold.
3. Report the selected setting and its neighbor. Do not interpret a tiny
   screening difference as statistically established superiority. If neither
   candidate reaches acceptable application performance, report the limitation
   rather than changing the SLO or silently extending the candidate search.
4. Freeze the selected per-policy thresholds for subsequent workload runs.
   Calibration observations must not be included as measured repetitions.
   The two-second deadline is an experimental target; no application-level
   required attainment percentage has yet been justified.

GPU-hours integrate allocated pod GPU capacity across the measured interval,
including drain. They do not measure EC2 savings; all three workers remain
billed. For interpretation also report observation duration, replicas over
time, and any downscale while backlog remains. A shorter drain can reduce
measured GPU-hours without indicating an intrinsically cheaper policy.

## Timing and scope

Retain first client dispatch, HPA SuccessfulRescale events, pod creation and
readiness, per-replica metrics where available, and timestamp uncertainty.
The KEDA 15-second polling setting is not the HPA evaluation interval when
minReplicaCount is 1 and metric caching is disabled. Do not label it as the
scale-decision cadence. Current burst phases drain between commands.

After screening, review short/mixed/bursty load construction and validate D
separately before committing to the complete 48-run matrix. These four long
trials alone do not calibrate SkyServe or prove suitability for all workloads.

### Revision-2 packaging failure (before workload)

The first `centra10-cal-r2-b02` Job failed installing requirements because its
settings ConfigMap omitted `requirements.txt`. No calibration workload began.
Keep that Job/log as a preparation failure; it is not a measured policy result.
The generator now includes that file. Prepare a fresh set using `--attempt 2`
and a new local directory. This suffixes Job, ConfigMap and result names with
`-a2`, avoiding immutable ConfigMap replacement and result overwrites. The
candidate settings and four-trial order remain unchanged. Before-workload
packaging recovery is not an additional threshold candidate.
