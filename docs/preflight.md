# Preparation while waiting for GPU quota

These steps create no AWS resources and need no AWS credentials. Run from the repository root on Linux (CloudShell or WSL is suitable):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
make test
make validate
python3 -m experiments.run --config aws/config.example.json
```

`make test` uses temporary files, fake `kubectl`/`sky`/`vllm` child processes, and a localhost HTTP server. It needs permission to bind localhost, but no external network or model download. The integration suite covers all 16 workload/mode combinations with one repeat, result analysis, resume, failed requests, missing telemetry, nonzero benchmark exit, timeout, interruption, malformed results, and cleanup failure. Existing analysis tests cover a synthetic 48-run dataset. Synthetic timings are never performance evidence.

`make validate` downloads checksum-verified tools and chart archives into `generated/validation/`, renders and lints both Helm charts, checks the EKS config against `eksctl utils schema`, and checks Kubernetes resources against Kubernetes 1.35 schemas plus chart CRD schemas. Missing schemas fail validation. Logs and rendered YAML remain in that directory. Downloads require internet; AWS credentials and kubeconfig are excluded from child commands. No `apply`, cluster creation, or cloud API calls are performed. `eksctl create cluster --dry-run` still requires AWS identity access, so it belongs in the later CloudShell preflight.

## Version baseline

`versions.json` records the proposed baseline. Both example profiles and `requirements-serving.txt` use these pins:

| Component | Version |
| --- | --- |
| Python | 3.12 |
| EKS/Kubernetes | 1.35 |
| eksctl | 0.231.0 |
| Helm / kubeconform | 4.3.1 / 0.8.0 |
| vLLM | 0.32.0 |
| SkyPilot | 0.14.1 |
| KEDA chart | 2.21.0 |
| kube-prometheus-stack chart | 92.14.0 |
| Qwen2.5-7B-Instruct revision | `a09a35458c702b33eeacc393d103063234e8bc28` |

Release metadata comes from [vLLM](https://github.com/vllm-project/vllm/releases/tag/v0.32.0), [SkyPilot](https://github.com/skypilot-org/skypilot/releases/tag/v0.14.1), the chart repositories recorded in `versions.json`, and the [model revision](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct/tree/a09a35458c702b33eeacc393d103063234e8bc28). Pins establish a reproducible starting point; they are not proof of GPU runtime compatibility. Python transitive dependencies and container digests are not fully locked: archive `pip freeze` and actual image digests during the live smoke test.

## After quota approval

Follow the [AWS guide](../aws/README.md). On the Linux benchmark host, install `requirements-serving.txt`, check `vllm bench serve --help` and `sky check`, and archive the installed environment. Check the region still supports EKS 1.35, run the authenticated EKS dry-run, then provision for the booked test window.

First validate one GPU: driver/device plugin, EFS mounts, model startup, actual vLLM metric names, and a short streaming request. Run `--smoke` for A, B, C, and D, confirming traffic distribution, scale-up/down, SkyServe selectors, and cleanup. Calibrate load and thresholds from real observations before starting 48 measured runs. Schema checks cannot prove these behaviors or capacity availability.

Runner cleanup releases serving pods; it does **not** stop billing for EKS nodes or other AWS infrastructure. Use the AWS guide's full teardown after the test window and verify remaining resources.
