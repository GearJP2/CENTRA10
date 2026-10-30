# Autoscaling LLM serving by QPS or queue length

Status: report template; no measurements collected. Target length: 4–6 pages.

## 1. Research question and hypothesis (½ page)

How do completed-QPS and waiting-queue scaling affect TTFT SLO attainment, allocated GPU-hours, and scaling response under different workloads? Queue scaling may help mixed workloads, but neither signal is assumed to win. Explain prefill/decode cost, admission behavior, and startup delays. Treat B versus C as the controlled comparison and D as an external reference.

## 2. Experimental setup (1–1½ pages)

Fill in: cluster and reservation dates; GPU model/count/memory; driver and CUDA versions; vLLM image tag/digest; model commit; tokenizer commit; SkyPilot and KEDA versions; Prometheus scrape and HPA sync intervals; model-cache storage; request routing and connection reuse; all nondefault vLLM flags.

Describe A/B/C/D, fixed versus 1–3 replicas, 120-second downscale settings and remaining controller differences. Include the shared-PV versus SkyPilot download distinction. State that QPS counts completions. Explain calibration using `calibration.csv`, including unsuccessful threshold candidates and equal tuning effort.

## 3. Workloads and measurement (1 page)

Describe short 100/100, long 6,000/200, mixed 80:20 and bursty 1×/4×/1×. State the final request rate, seeds, repeat order, approximate input lengths, phase drain gaps, warm-up conditions and failure accounting. Attach the 48-cell completeness check.

Define TTFT percentiles over successful requests and SLO fraction over all attempted requests. Define the allocated-GPU cost interval and sampling method. Separate cold-start timing from load-to-ready response; document censored pods, restarts and benchmark initialization overhead. Identify load-generator limitations and competing workloads, if any.

Use `runtime.csv` to compare measured client dispatch QPS with completed QPS and waiting/running requests. State whether KV-cache telemetry was available (maximum replica occupancy, not average utilization). Show target versus observed load; dispatch attempts do not establish server receipt. Use `scaling.json`, pod readiness timestamps, and D's `sky-controller.log` to explain delays. State clock synchronization, sampling uncertainty, and unavailable decision events. Mark burst start and new replica readiness on the burst figure.

## 4. Results (1–1½ pages)

Insert the three generated figures and per-condition mean/standard-deviation table. Show all three repetitions, failures, cost, and cold-start observations. Do not fill this section with synthetic test outputs.

## 5. Interpretation and limitations (½–1 page)

Answer the hypothesis using mixed-workload results, then compare homogeneous and bursty workloads. Discuss completed-throughput saturation, queue admission behavior, cold starts, readiness, routing, small sample size, and D's additional differences. Explain results that contradict the hypothesis. Do not claim causal B/D differences from scaling policy alone.

## Reproducibility and checkpoint evidence

Link the repository commit, environment lock, calibration ledger, archived raw results, analysis command, and four dated checkpoint approvals/screenshots. List any deviations from the PDF brief.
