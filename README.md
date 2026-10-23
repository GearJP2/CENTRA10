# CENTRA10: QPS versus queue autoscaling for LLM serving

Implementation of **Project Brief Autoscaling LLM (1).pdf**. The experiment compares fixed capacity (A), KEDA completed-QPS scaling (B), KEDA queue scaling (C), and SkyServe QPS scaling (D). The hypothesis is that queue scaling improves TTFT SLO attainment on mixed request lengths without increasing GPU-hours. Results must determine whether that hypothesis holds.

This repository contains deployment YAML, a Grafana dashboard, a 48-run experiment runner, raw-result validation, analysis and plotting, and report/poster outlines. **No GPU measurements have been collected.** Example configuration values are intentionally incomplete; tests use synthetic data only in temporary directories.

## Requirements and configuration

- A reserved Kubernetes cluster with at least three identical GPUs, each with ≥24 GB memory; NVIDIA device plugin and matching drivers.
- Prometheus Operator, kube-state-metrics, KEDA, and Grafana already installed. Cluster installation is outside this repository.
- A 50 GiB **ReadWriteMany** storage class accessible from all GPU nodes. A single-node ReadWriteOnce volume will not support this experiment.
- `kubectl`, SkyPilot with Kubernetes support, and a compatible current vLLM release on the benchmark host. The host must reach the serving endpoints and Prometheus.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install 'skypilot[kubernetes]' vllm
cp config.example.json config.local.json
vllm --version
sky --version
sky check
vllm bench serve --help
```

Edit `config.local.json`: use the installed vLLM version (without `v`), a full Hugging Face model commit SHA, your RWX storage class, the correct GPU node labels, and reachable endpoints. Pin the same vLLM release and model revision for every treatment; generated images use `vllm/vllm-openai:v<VERSION>`. Check that tag exists for your chosen release. Keep the resulting environment (`pip freeze`) with the experiment records. The runner downloads only tokenizer files at the pinned revision on the load host.

`prometheus_internal` is reachable by KEDA inside the cluster; `prometheus_url` is reachable by the runner. `k8s_url` must reach the Service or a request-balancing ingress. **Do not benchmark through `kubectl port-forward deployment/vllm` or `service/vllm`: port-forward selects one pod and bypasses replica balancing.** Use port-forward only for inspection. Check request distribution across replicas: persistent TCP connections to a plain Kubernetes Service can concentrate requests; use an ingress with per-request upstream balancing if needed, identically for A–C, and document it.

```bash
make render
python3 -m experiments.run --config config.local.json  # print the 48-run plan only
make test
```

`manifests/` contains checked-in examples rendered from `config.example.json`. They have placeholders and are **not deployable unchanged**. `generated/` contains your rendered configuration. Regenerate after changing thresholds or deployment settings. Do not commit credentials, tokens, kubeconfig, or local configuration.

## Days 1–3: SkyPilot walkthrough

```bash
sky launch -c centra10-hello generated/sky-hello.yaml
sky status
sky logs centra10-hello
sky down centra10-hello

sky launch -c centra10-single generated/sky-single.yaml
sky status --endpoint 8000 centra10-single
# Use the returned endpoint to inspect /v1/models and send a chat completion.
sky down centra10-single

sky serve up -n centra10-llm generated/sky-fixed-two.yaml
sky serve status centra10-llm
sky serve logs centra10-llm 1
sky serve update centra10-llm generated/sky-autoscale.yaml
```

Send traffic and capture **checkpoint 1**: one to at least two ready replicas. Then `sky serve down centra10-llm` before the Kubernetes experiments. The automated runner owns this configured service name and creates a fresh service for each D run; do not reuse an unrelated service name.

The D reference downloads the pinned model during SkyPilot setup on each new replica, as in the brief's walkthrough. A–C share a preloaded volume. This cold-start difference is why **B versus C is the controlled comparison**. Record D's provisioning/download time separately; do not attribute all B/D differences to the scaling metric. SkyServe setup time and initial warm-up are outside the measured run; scale-out during load is inside it.

## Days 4–6: Kubernetes, monitoring, and calibration

```bash
# Initialize resources and pause the deployment while the model downloads.
kubectl apply -f generated/cluster.yaml --dry-run=client
kubectl apply -f generated/cluster.yaml
kubectl -n centra10 scale deployment/vllm --replicas=0
kubectl apply -f generated/download.yaml
kubectl -n centra10 wait --for=condition=complete job/download-model --timeout=1800s
kubectl -n centra10 scale deployment/vllm --replicas=1
kubectl -n centra10 rollout status deployment/vllm --timeout=1800s
kubectl apply -f generated/monitor.yaml
```

Use your configured namespace if different. The offline serving container may initially retry until the model download finishes; the download Job uses no GPU. The PVC is retained during cleanup. If you change the model revision, remove only the completed `download-model` Job, regenerate, and rerun it before serving the new revision.

Ensure your Prometheus instance selects this ServiceMonitor's namespace and labels (Helm installations often require a release label). Check that each pod is scraped once every 5 seconds and series carry `namespace` and `service="vllm"`. Check kube-state-metrics exposes GPU resource requests, scheduling status, and deployment availability. Adjust queries in `experiments/config.py` to actual metric names if your vLLM or kube-state-metrics release differs; regenerate afterward.

```bash
kubectl -n centra10 port-forward deployment/vllm 8000:8000
# In another terminal; diagnostic only:
curl -s localhost:8000/metrics
```

Import `generated/dashboard.json` into Grafana and select your Prometheus datasource. Capture **checkpoint 2** showing ready replicas, waiting requests, and completed QPS over time. QPS uses `vllm:request_success_total`, **not arrival rate**; completed throughput can saturate and hide overload. Missing telemetry fails the runner and KEDA scaler rather than being interpreted as zero.

The B and C manifests use the same ScaledObject name, 1–3 replicas, 15-second polling and 120-second scale-down stabilization. The runner deletes the controller and waits for its HPA to disappear before resetting to one replica. Only one scaler may target this deployment. SkyServe also uses a 120-second downscale delay and no upscale stabilization, but its controller mechanics differ from KEDA.

Calibrate before final runs: run **A only** at increasing request rates, using a separate output directory per rate. Find the point where TTFT exceeds 2 seconds, then inspect QPS and queue length there. Select both thresholds with the same tuning effort and record every candidate in `docs/calibration.csv`. Pick a final load at which A clearly misses the SLO; use the same final rate for all four treatments. If queues stay zero despite high latency, inspect running requests and GPU occupancy; any change to concurrency limits must be applied equally to all configurations before rerunning calibration.

```bash
python3 -m experiments.run --modes A --workloads mixed --repeats 1 --output results/calibration-rate4 --execute
```

## SkyServe telemetry for D

SkyPilot's pod labels and namespace depend on your installation. During the walkthrough, inspect its replica pods with `kubectl get pods -A --show-labels`. Configure `sky_namespace` and `sky_pod_selector` to select **only this service's GPU replicas**, across replica IDs and service updates. Arrange Prometheus discovery of each replica's `/metrics`; do not scrape only the load-balancer endpoint. Reuse your operator's PodMonitor discovery with the observed labels.

Set the four `sky_queries` to single-series aggregates restricted to these pods: allocated GPUs, ready replicas, waiting requests, and completed-QPS rate. Use kube-state-metrics for allocated/ready counts. Allocated GPUs should include scheduled Pending and Running pods, including loading replicas, while excluding terminal pods and the CPU controller. Do not equate ready count with allocated cost. These cluster-specific selectors cannot be guessed safely; the runner refuses D while placeholders remain and waits for one ready, allocated replica with an empty queue before each measured run. Preserve your D PodMonitor/query setup with the submitted artifacts.

## Days 7–10: collect results

Reserve the GPUs with your supervisor first, shut down walkthrough services, and ensure no competing workloads occupy the three experiment GPUs. Execute from a host with enough CPU and network capacity that load generation is not the bottleneck.

```bash
# Checkpoint 3: first mixed-workload round, all four configurations.
python3 -m experiments.run --workloads mixed --repeats 1 --execute
python3 -m experiments.analyze results --output results/analysis
# Obtain supervisor review of checkpoint 3 before continuing.

# Remaining conditions; completed matching runs are preserved.
python3 -m experiments.run --execute --resume
python3 -m experiments.analyze results --output results/analysis --require-complete
```

Every run begins with one allocated and ready replica, an empty queue stable for 65 seconds, and a successful warm-up completion. Treatment order rotates across repeats. Seeds match across treatments. The runner executes mixed short/long streams concurrently, with rates split 80:20. Short is approximately 100 input/100 output tokens; long is 6,000/200. EOS is ignored to preserve output lengths; prefix caching is disabled on all servers. Chat template tokens can make input lengths approximate.

Bursty uses short requests at 1×, 4×, 1× load, each targeting 180 seconds of arrivals. As in the brief's three-command approach, **each phase drains before the next begins**; process startup, Poisson arrival variation, and outstanding completions extend wall-clock duration. Exact phase process boundaries are recorded, and plots use measured time. The response-time estimate begins at high-phase process launch, so it includes benchmark initialization; use detailed request `start_times` when reporting a more precise arrival-based response time. This is not an exact wall-clock traffic replay.

Each folder stores `run.json`, pinned configuration/manifests, detailed vLLM JSON and logs, sampled Prometheus totals (`metrics.json`), and pod snapshots (`pods.json`). `versions.json` records client vLLM, Kubernetes, KEDA deployment metadata, and SkyPilot versions. Verify the KEDA deployment selector matches your installation; add its actual image version if that query returns no items. Also record actual GPU/driver/image digest information in the report.

The runner stops on command/telemetry errors, terminates active benchmark processes, and attempts GPU cleanup. Existing output directories are never overwritten. `--resume` skips only completed runs with matching configuration; move a failed run to an archive outside the result root before retrying. The analyzer refuses incomplete/corrupt runs and never invents missing observations. Do not mix calibration runs into the final results root.

## Analysis and deliverables

- `runs.csv`: per-run TTFT p50/p99 in **seconds**, SLO attainment, failures, GPU-hours, and burst response estimates.
- `summary.csv`: means and sample standard deviations across repeats; p99 is summarized per run, not computed by averaging request percentiles.
- `cold-starts.csv`: pod start-to-first-observed-Ready durations for pods started inside the measured window, when any are observed. Non-ready or deleted-between-samples pods are not assigned a fictional duration; inspect snapshots for censoring/restarts.
- `mixed-slo-cost.png`, `bursty-replicas-queue.png`, `ttft-p99.png`: the three required poster figures, with individual repeats visible.

SLO attainment uses **all issued requests** as the denominator; failed requests miss the SLO. TTFT percentiles use successful requests only. GPU-hours integrate sampled allocated-GPU counts over the entire measured interval, including benchmark startup and drain, excluding initial preparation and post-run cleanup. Integration uses a left-held step function; sampling makes transitions approximate. Gaps over three intervals (at least 30 seconds) invalidate the run's cost calculation. Report failed counts alongside latency; a failed request must never appear as a fast success.

Complete the 4–6 page report in `docs/report.md` and the English poster outline in `docs/poster.md` using measured results. Preserve all 48 raw result folders and their versions/configuration in an archive for submission; `results/` is gitignored to avoid accidentally committing large outputs. Send **checkpoint 4**, the poster draft, at least two days before submission. Report a negative or inconclusive result honestly.

## Cleanup and limitations

```bash
kubectl -n centra10 delete scaledobject vllm-autoscaler --ignore-not-found
kubectl -n centra10 scale deployment/vllm --replicas=0
sky serve down centra10-llm
# Also sky down any remaining walkthrough clusters shown by sky status.
```

The runner cleans up successful starts but cannot guarantee cleanup after a killed process, interrupted SkyServe provisioning, or a cluster/API outage. Always inspect `sky status`, `sky serve status`, and GPU pods after a session. Cleanup retains the model PVC and raw results. Never delete the namespace/PVC unless you intend to discard the model cache.

Local tests validate manifests, workload construction, failure cleanup, metric rejection, aggregation, and all three figures on synthetic 48-run data. Live YAML acceptance, GPU serving, SkyPilot integration, label discovery, and threshold quality require the target cluster and are not verified by these tests.

References: [vLLM benchmark CLI](https://docs.vllm.ai/en/stable/cli/bench/serve/), [SkyPilot YAML](https://docs.skypilot.ai/en/latest/reference/yaml-spec.html), [SkyServe service updates](https://docs.skypilot.ai/en/stable/serving/update.html), [KEDA Prometheus scaler](https://keda.sh/docs/2.20/scalers/prometheus/).
