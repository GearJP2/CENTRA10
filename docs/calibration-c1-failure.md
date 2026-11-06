# C queue=1 calibration: scale-down and truncated streams

Run: `results/calibration-C-queue1-long-1/long_C_run1`.
The run completed collection and cleanup, but only 290/300 requests succeeded.
Ten requests have identical aiohttp ClientPayloadError / TransferEncodingError
messages indicating truncated response payloads. These are failed requests,
not an interrupted collector and not proof of an HTTP 400 response status.

## Timeline from saved evidence

Times below are seconds after the first benchmark request, mapped using the
saved remote-clock offset (calibration uncertainty approximately ±0.049 s).

| Time | Evidence |
|---|---|
| +36.57 | HPA SuccessfulRescale to 3 |
| +501.57 | HPA SuccessfulRescale to 1; vLLM container Killing events |
| +500.81 | Nearest prior aggregate sample: 3 allocated/ready, queue 0, running 13 |
| about +502.182 | All ten failed streams' reconstructed last observed token times |
| +546.57 | HPA SuccessfulRescale back to 3; nearest sample has queue 3, running 13 |

Last-token times are computed as start_times + clock_offset + TTFT + sum(ITLs).
They are proxies for the last observed streamed token, not measured disconnect
timestamps. Failed `latencies` are zero in these files, so adding those values
to request starts would give invalid failure times. No request-to-pod mapping
was collected; the exact ten requests cannot be assigned to deleted pods.

## Deployment semantics

Saved pod snapshots show Kubernetes terminationGracePeriodSeconds=30 and no
preStop lifecycle handler. The generated vLLM arguments do not specify a
shutdown timeout. In the upstream pinned v0.32.0 CLI source, `--shutdown-timeout`
defaults to 0 with semantics `0 = abort, >0 = wait`. The engine configuration
and shutdown utilities also describe aborting in-flight requests on SIGTERM
when this timeout is zero. A Kubernetes grace interval alone does not make an
application drain requests if the application elects to abort immediately.

Primary sources inspected:
- https://raw.githubusercontent.com/vllm-project/vllm/v0.32.0/vllm/engine/arg_utils.py
- https://raw.githubusercontent.com/vllm-project/vllm/v0.32.0/vllm/config/vllm.py
- https://raw.githubusercontent.com/vllm-project/vllm/v0.32.0/vllm/v1/engine/utils.py
- https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/

## Conclusion and limits

The synchronized stream truncations immediately following scale-down, together
with the no-drain default, strongly support scale-down-triggered abortion of
in-flight work. This is not independently proven for each request: deleted
serving-container logs and request/backend identity are unavailable. The
waiting-only trigger can legitimately fall when requests are running; the
result reflects interaction between that signal, the HPA policy, and serving
termination behavior. Do not report it as a universal failure of queue scaling.

## Next step before completing calibration

Retain this run and the B candidate as original-setup screening evidence.
Test bounded request draining during a controlled pod termination, without
changing waiting-only versus completed-QPS semantics. Any adopted shutdown
setting must apply to both B and C with a Kubernetes grace budget sufficient
for that drain and cleanup waits consistent with the new budget. Capture
termination logs and completed/error counts in that test.

If a new lifecycle configuration is adopted, amend the calibration protocol
prospectively, version the serving manifest, and rerun all four candidates
under that common configuration. Never mix original and amended candidates
when selecting thresholds. A successful short drain test cannot guarantee
completion of arbitrarily long queued work; retain a bounded timeout and
report failures. Existing calibration outcomes remain reportable evidence.

No serving code or AWS resources were changed while producing this diagnosis.
