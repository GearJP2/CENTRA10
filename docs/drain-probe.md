# Controlled streaming drain probe

Pause policy calibration while investigating serving lifecycle. Preserve the
original B0.2 and C1 results; do not silently replace them. No queue/QPS trigger
is changed by this probe.

`experiments.drain` requires no autoscalers and exactly one desired vLLM
replica. It waits for one Ready pod, begins one streaming completion through
the normal proxy, and requests scale-to-zero only after receiving nonempty
content. It records deployment/pod configuration, follows serving logs, and
writes `probe.json` on EFS. It asks for 512 output tokens with ignore_eos and
requires [DONE], finish_reason=length, and usage completion_tokens=512 before
calling the stream complete. A truncated stream is a completed *observation*,
not a successful streamed response. Check `stream_complete`, not Job status
alone. Timeouts or preparation failures are marked failed.

Run two arms sequentially with unique Job/output/settings/code names:

| Arm | shutdown_timeout_seconds | Pod termination grace | Expected diagnostic outcome |
|---|---:|---:|---|
| Control | 0 | 30 s | Test whether an active stream is aborted |
| Drain | 120 | 150 s | Test whether this bounded request completes |

Both settings are opt-in; rendering existing configs without the setting keeps
the original behavior. This changes lifecycle settings for a diagnostic, not
for the main comparison. The 120-second bound is a probe candidate, not a
proven safe timeout for arbitrary backlogs. It remains within the existing
300-second runner cleanup wait. A larger drain budget would also require
revisiting those waits.

Before each arm, confirm no other experimental Job/process is running and no
HPA/ScaledObject targets vLLM. Prepare the control (no cloud changes):

```bash
.venv/bin/python -m experiments.drain --prepare --shutdown-timeout 0 \
  --config config.validation-c1.local.json --output generated/drain-control-0-1
kubectl apply -f generated/drain-control-0-1/bundle.yaml
kubectl -n centra10 logs -f job/centra10-drain-0 -c runner
```

The bundle updates only the runner Role, dedicated code/settings ConfigMaps,
vLLM Deployment/Service and the new diagnostic Job. It assumes the existing
runner service account/bindings, EFS PVC, proxy and experiment-results reader.
It starts one serving replica; the probe scales it to zero after its first
content token. No image or policy version is changed. The Job's 40-minute
backstop can abort cleanup; inspect after any failure.

After reviewing control evidence, prepare the second arm with
`--shutdown-timeout 120` and a fresh directory `generated/drain-control-120-1`.
Apply that bundle only after control finishes. Its Job is `centra10-drain-120`.
It records into `drain-control-120-1` on EFS. Do not overwrite a preparation
folder or rerun the same Job/output to hide a failure.

The image already contains PyYAML; this probe does not install matplotlib.
CloudShell is needed only to submit and inspect the Job.

Inspect through experiment-results:

```text
/models/experiment-results/UNIQUE_ARM_NAME/probe.json
/models/experiment-results/UNIQUE_ARM_NAME/serving.log
/models/experiment-results/UNIQUE_ARM_NAME/pod-before.json
/models/experiment-results/UNIQUE_ARM_NAME/deployment.json
```

The probe captures the observed serving pod identity but does not implement
request-to-backend tracing. One Ready replica makes assignment less ambiguous.
An SSE success on this short-input request does not validate draining long
queued requests, endpoint removal races, proxy timeouts, all repeated
scale-down patterns, or other hardware.

Only adopt a common lifecycle revision for B/C after checking the actual
results and logs. Then amend the calibration plan and rerun all four candidates
under the same revision. Archive the original diagnosis and raw outcomes.

## Recorded outcome

Saved control evidence shows stream_complete=false and serving logs explicitly
reporting mode=abort timeout=0s and force-killing EngineCore. The client stream
ended approximately 30.1 seconds after scale request; this delay is not engine
drain time. The 120-second arm reports [DONE], finish_reason=length and all
512 tokens; it ended approximately 28.8 seconds after scale request. These
observations support using a common bounded-drain revision for subsequent
calibration, not a guarantee for arbitrary queued work. See the revision-2
amendment in calibration-protocol.md.
