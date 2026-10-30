# QPS or Queue? Autoscaling LLM Serving under Mixed Request Lengths

**CENTRA 10 · Authors: [names] · Affiliation: [institution]**

Draft outline only. Replace bracketed fields after real experiments; do not present synthetic test data.

## Research question

How do completed-QPS and waiting-queue scaling affect TTFT SLO attainment, allocated GPU-hours, and response to different workloads?

## Why this matters

Short questions and long documents can arrive at the same request rate but require very different GPU work. Waiting queues can stay short while requests are running, and completed QPS can saturate. Neither signal is assumed to win; running requests, client dispatch rate, KV-cache usage (if available), and startup delays help explain the observations.

## Controlled experiment

One pinned 7B model and vLLM version; identical GPUs; 1–3 replicas; 120-second downscale window; each round starts with one warm replica. Four configurations × four workloads × three repeats = 48 runs.

| Configuration | Scaling signal |
|---|---|
| A | One fixed replica |
| B | Completed requests per second, KEDA |
| C | Waiting requests, KEDA |
| D | SkyServe QPS reference |

Short: ~100/100 input/output tokens. Long: ~6,000/200. Mixed: 80% short, 20% long. Bursty: low → 4× → low.

## Main result — largest figure

Insert `mixed-slo-cost.png`. Headline: **[Measured B-versus-C result, including uncertainty.]**

Horizontal axis: GPU-hours. Vertical axis: requests meeting TTFT ≤ 2 s. Each point represents one run; failed requests count as SLO misses.

## Response to bursts

Insert `bursty-replicas-queue.png`. Caption: [Explain readiness/queue dynamics and measured cold starts. Dotted lines mark first high-phase dispatch; triangles mark new pods becoming Ready. Distinguish controller detection, pod start, and Ready times.]

## Tail latency across workloads

Insert `ttft-p99.png`. Caption: [Compare all configurations, show failures separately.]

## Takeaway and limits

[One evidence-supported recommendation, even if the hypothesis is rejected.]

B/C isolate the scaling signal. SkyServe differs in routing and provisioning. Three repeats, sampled cost, sequential draining burst phases, and successful-request latency percentiles limit interpretation.

Repository/QR: [URL] · Raw data: [archive] · Versions: [vLLM / SkyPilot / KEDA]
