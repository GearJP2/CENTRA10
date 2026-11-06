# B/C calibration protocol — bounded screening

This protocol is prospective for the four trials below. Earlier validation and
fixed-three runs informed its design; they are not independent confirmation.
It selects one global threshold per policy for later workload comparisons.
It does not establish optimal thresholds or equal policy expressiveness.

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
| 1 | calibration-B-qps02-long-1 | B | 0.2 completed req/s |
| 2 | calibration-C-queue1-long-1 | C | 1 waiting request |
| 3 | calibration-C-queue2-long-1 | C | 2 waiting requests |
| 4 | calibration-B-qps01-long-1 | B | 0.1 completed req/s |

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
