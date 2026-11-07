# D threshold calibration — two trials, then freeze

This protocol is fixed before D-long threshold screening. B and C remain frozen
at completed-QPS target 0.1 and waiting target 2, respectively. Do not reopen
their search. D is a SkyServe system reference with incoming-request QPS and
least-load routing; it is not a controlled replacement of the B/C trigger alone.

## Evidence available before screening

`sky-collector-validation-2` completed 300 short requests at 1 req/s without
errors, with p99 TTFT 0.227 s, all-attempted-request two-second SLO attainment
100%, and successful cleanup. Its 25 samples reached three allocated and three
SkyServe READY replicas; maximum sample gap was 14.13 seconds despite nominal
five-second sleeps. This validates the collection path, not a D-long threshold.
The two new replicas first appeared READY approximately 227 and 231 seconds
after pod start. They contributed only near the end of that short validation.
Keep all short validation and failed collector attempts outside calibration
ranking and measured repetitions.

## Fixed two-trial budget

| Order | Candidate | SkyServe target QPS per replica | Workload | Offered rate | Requests |
|---|---|---:|---|---:|---:|
| 1 | D0.2 | 0.2 | Long, 6000 input / 200 output tokens | 0.5 req/s for 600 s | 300 |
| 2 | D0.1 | 0.1 | Same | Same | 300 |

Use seed 100 and one run per candidate, matching the B/C revision-2 calibration
operating point and candidate QPS values. Start each from one warm READY replica
after its rolling update settles; allow at most three replicas. Preserve
SkyServe upscale delay 0, downscale delay 120, default QPS estimator and
least-load routing, pinned vLLM/model revision, shutdown timeout 120 seconds,
pod grace 150 seconds, and the validated collector. Do not change those settings
between candidates. SkyServe/Ray draining under load remains a system-specific
limitation; record truncated responses rather than discarding them.

Image caching can affect startup. The existing three workers have already run
the pinned image; record any node replacement or image pull in events. Each D
replica still downloads the pinned model into its own filesystem. Do not change
D to EFS model loading midway. Collect the same controller/status/metrics
evidence for both candidates. Do not overlap any other runner, benchmark or
autoscaler. Run D0.2 followed by D0.1, archiving and checking cleanup between them.
This two-run order does not eliminate cache or time effects.

## Selection and stop rule

Use the same rule as B/C bounded screening:

1. Require `status: complete`, no cleanup error, valid benchmark timestamps and
   required telemetry, monotonic samples and no gap above the current analyzer
   limit of `max(30 s, 3 * sample_seconds)`. Request failures count against SLO;
   they do not justify discarding an otherwise valid candidate.
2. Prefer higher **all-attempted-request TTFT <= 2 s** attainment. With 300
   requests each, a difference of at most **six successful SLO requests** is the
   two-percentage-point tie band; use integer counts at this boundary.
3. Within the band, prefer lower measured allocated GPU-hours. Remaining ties
   use lower successful-request TTFT p99, then lower numeric threshold.
4. After these two usable candidates, record the winner and neighbor and freeze
   D's threshold for every measured workload. Do not add thresholds or rerun a
   valid unfavorable result. One infrastructure/setup failure may be retried
   once after diagnosis with a new attempt ID; retain the failure. If it fails
   again, stop and review the infrastructure before extending the budget.

Low attainment is a result, not permission to change the SLO or expand the
search. This is screening, not an optimality or significance claim. GPU-hours
cover the measured run including request drain, not EC2 billing or initial setup.
READY transitions have collector/status-refresh uncertainty; keep actual sample
spacing and the clock uncertainty with the results. Do not claim 5 s cadence.

## Prepare once, apply one bundle at a time

```bash
.venv/bin/python -m experiments.sky_runner \
  --config config.resume-20261107.local.json \
  --output generated/sky-calibration-r2 --calibrate
cat generated/sky-calibration-r2/plan.json
kubectl apply -f generated/sky-calibration-r2/d02/bundle.yaml
kubectl -n centra10 logs -f job/centra10-sky-cal-d02-a1 -c runner
```

Only after D0.2 completes and its results are archived and checked:

```bash
kubectl apply -f generated/sky-calibration-r2/d01/bundle.yaml
kubectl -n centra10 logs -f job/centra10-sky-cal-d01-a1 -c runner
```

Results reside on EFS under
`/models/experiment-results/calibration-r2-D-qps02-long-1-a1` and
`/models/experiment-results/calibration-r2-D-qps01-long-1-a1`.
Retrieve them through `experiment-results` using the archive/analysis procedure
in [the D collector guide](sky-collector.md). Never overwrite an existing result
directory or retry by reapplying a completed Job. Preparation changes no cluster
resources. Applying a bundle starts work on the existing GPU nodes; EC2 billing
continues independently of GPU pod allocation.

## Gate before the measured matrix

After ranking, write a frozen D selection referencing both saved runs, including
failure counts and limitations. Then lock the short/long/mixed/bursty rates and
their corresponding configs before measured repetitions. Long 0.5 req/s is an
evidence-backed calibration operating point, not a universal rate for the other
workloads. Workload-rate decisions are still pending. The existing burst generator
uses sequential drained phases rather than a continuous wall-clock burst trace;
state that design explicitly rather than implying seamless bursts.

Do not count any calibration trial as one of the 48 measured runs, and do not
interpret D/B differences as caused solely by waiting versus QPS signals.

## D0.2 diagnostic note before D0.1

The reported D0.2 result completed collection and cleanup: 288/300 successful
requests, 12 incomplete streamed-response errors, SLO attainment 37%, successful
TTFT p99 119.387 s, and allocated GPU-hours 0.482006. Its maximum sample gap was
14.65 s. The supplied status transitions only show growth from one to three
READY replicas, with no observed downscale during measurement; the controller
log excerpt likewise shows no executed downscale. This does not establish
the cause of the truncated streams.

Pinned SkyPilot 0.14.1 source sets `DEFAULT_LB_STREAM_TIMEOUT = 120` in
`sky/serve/constants.py` and applies that timeout to proxied HTTP requests in
`sky/serve/load_balancer.py`. A read timeout while waiting for stream data is
therefore a plausible explanation, not a confirmed finding. The load-balancer
log was not retained before D0.2 cleanup, so it cannot establish that link.

Retain D0.2 with all 12 failures. Do not rerun it or change the timeout, SLO,
lifecycle settings or candidate budget. The collector now also saves
`sky-load-balancer.log` after the measured interval and before cleanup. Regenerate
the unstarted D0.1 bundle to include this extra evidence; this adds a log, not a
new treatment or candidate. Keep code provenance with each run. D0.1 remains
the second and final threshold candidate.
