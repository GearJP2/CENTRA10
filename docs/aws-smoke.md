# AWS single-GPU smoke record — 31 October 2026

This session verified deployment and measurement plumbing, not the research
hypothesis. The full 4 × 4 × 3 comparison remains outstanding.

## Environment and evidence

The us-east-1 EKS 1.35 cluster used one g6.2xlarge (L4) and one m6i.2xlarge
system node. Qwen2.5-7B-Instruct, the pinned model revision, and vLLM 0.32.0
served from EFS. A separate benchmark pod ran on the CPU system node alongside
monitoring. CloudShell orchestrated the run and collected telemetry.

- [Manual smoke results](../results/cpu-client-smoke/)
- [Completed automated A/short run](../results/automated-smoke-2/short_A_run1/run.json)
- [Raw benchmark and telemetry](../results/automated-smoke-2/short_A_run1/)
- [Analysis CSV](../results/automated-smoke-2-analysis/runs.csv)

The automated run completed 6/6 requests at configured 0.2 req/s, with no
failures. Its analyzed TTFT p50 was 164 ms and p99 188 ms. These six requests
are a smoke check, not reliable tail-latency or capacity estimates. The recorded
clock calibration uncertainty was approximately ±0.468 seconds.

The separate manual 1 req/s test completed 30/30 requests, mean TTFT 162 ms,
mean TPOT 58.15 ms. The saved 30-minute metric window (including preceding
tests) showed running requests peaking at 9 and waiting requests at 0 at
sampled times. It cannot establish that no transient queue occurred.

## Runtime fixes now in the code

- Disable Kubernetes service environment injection for vLLM pods: the `vllm`
  Service otherwise injects a URI-valued `VLLM_PORT` that prevents engine startup.
- Read CPU-client package metadata for the version check; `vllm --version`
  initializes serving device detection and failed on the CPU node.
- Use `--random-range-ratio 0` for fixed token lengths. A ratio of 1 allowed
  zero-length inputs and was rejected by the pinned CLI.
- Execute benchmarks in the CPU pod, retrieve JSON results, and record remote
  clock calibration uncertainty. Local tests complement, but do not replace,
  the saved live run.

The NVIDIA plugin initially failed on the CPU node; selecting
`centra10-role=gpu` on its DaemonSet resolved that placement issue. Do not
install a second plugin when eksctl has already installed one.

## Remaining validation

No B/C scaling, multi-replica routing, SkyServe, saturation calibration, or full
research matrix was tested. KEDA was installed without an active scaling rule.
The CPU client shared monitoring resources; assess client contention before
using it for final measurements. Recheck clock uncertainty for scale-response
claims. Smoke plots are not comparative research figures.

## Teardown outcome

User-provided CloudShell output confirmed both EC2 instances terminated, the
EKS and node-group stacks deleted, both addon/IAM stacks deleted, no EKS
clusters, NAT state `deleted`, no matching EFS filesystem, and empty regional
EIP and EBS inventories. These teardown outputs were supplied in the session,
not archived alongside the benchmark JSON. Earlier usage can still appear in
billing later.

In this session eksctl deletion failed refreshing CloudShell credentials while
AWS CLI worked. The successful fallback deleted node-group CloudFormation
stacks first, then the cluster stack, then the addon/IAM stacks. EFS access
points, mount targets, filesystem, and its security group were removed before
cluster networking. Re-inventory actual IDs before repeating cleanup; this is
not authorization to delete unrelated account resources.
