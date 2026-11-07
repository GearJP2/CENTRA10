# Prospective measured workload plan

Threshold screening is complete: B completed-QPS target 0.1, C waiting target
2, and D SkyServe incoming-QPS target 0.1. Their evidence is in the two freeze
records. No additional threshold search is planned. This document specifies the
workload design for preparing measured Jobs; it does not start the matrix.

Use a common baseline **0.5 requests/s** to separate request-count demand from
token work across short, long and mixed inputs. Long calibration and the fixed
three-replica capacity check already used this rate. Its use for short/mixed
is an experimental design choice, not a demonstrated saturation point for
every workload. A lightly loaded short workload is useful: unnecessary scaling
and capacity consumption remain outcomes even if waiting queues stay empty.

| Workload | Target token lengths | Offered traffic | Requests per run |
|---|---|---|---:|
| Short | 100 input / 100 output | 0.5 req/s, 600 s | 300 |
| Long | 6000 input / 200 output | 0.5 req/s, 600 s | 300 |
| Mixed | 80% short, 20% long | Concurrent 0.4 + 0.1 req/s, 600 s | 240 + 60 |
| Bursty | Short | 0.5, 2, 0.5 req/s; 180 s per phase | 90 + 360 + 90 |

These rates and durations use the existing runner's `request_rate=0.5`,
`duration_seconds=600`, `burst_phase_seconds=180` and fixed 4x burst factor.
Arrival gaps remain Poisson; requested duration specifies count/rate and is not
a guarantee of exact wall-clock duration. Each burst phase drains before the
next one starts, as in the current implementation. Describe it as sequential
load phases, not a continuous burst trace. Do not change a workload's rate
after seeing a policy's measured result.

Each workload is applied unchanged to A/B/C/D, with three measured repetitions:
48 unique conditions. A uses one fixed replica; B/C/D start from one warm
replica and may scale to three. The fixed-three diagnostic is not an additional
mode or a replacement for A. Keep model/revision, vLLM image, token lengths,
sampling parameters and lifecycle settings pinned. Report D as a system
reference, including its model download, routing, timeout and readiness
observation differences.

## Before emitting measured Jobs

Use fresh result IDs, immutable code/settings and one Job per condition so a
CloudShell interruption cannot lose a whole matrix. Never run Jobs concurrently
against the shared GPUs. Rotate treatment order across repetitions as the
current runner does. Preserve the actual submitted order and versions.

Use fresh measured traffic seeds rather than reusing calibration's seed 100;
the current runner's seed calculation must be extended before preparing these
Jobs. The seed change is prospective and must be common across policies for
each workload/repetition. Keep calibration/validation out of measured repeats.
Verify fresh configs against both freeze records before launch. No additional
hardware calibration is required solely to implement this seed separation.

Raw D calibration results have been independently checked in commit `aec8e7c`.
Its eleven D0.1 load-balancer read timeouts are recorded limitations, not a reason
to raise the timeout after selection. Report failure counts and all-attempted
SLO with successful-request p99. Capture load-balancer logs for measured D runs.

The next implementation step is measured Job preparation with the common seed
offset and fixed per-workload settings above. Do not launch the existing
calibration bundles as measured runs or invoke an unattended 48-run process
before those artifacts are prepared and checked.
