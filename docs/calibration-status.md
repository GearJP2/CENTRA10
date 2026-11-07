# Calibration checkpoint — 7 November 2026

Revision-2 B/C screening is complete. The selected thresholds are B=0.1
completed req/s and C=2 waiting requests, with common shutdown timeout=120 s
and Kubernetes termination grace=150 s. See frozen-calibration-r2.json for
exact candidates, result directories and the evidence commit.

| Policy candidate | SLO successes / attempts | Allocated GPU-hours | Selection |
|---|---:|---:|---|
| B 0.2 | 81 / 300 | 0.426570 | Not selected |
| B 0.1 | 119 / 300 | 0.485554 | Selected by SLO attainment |
| C 1, cached | 81 / 300 | 0.518568 | Not selected |
| C 2 | 87 / 300 | 0.504812 | Selected within tie band by GPU-hours |

All four have zero request failures and complete cleanup. These are screening
outcomes, not statistical evidence of policy superiority. Only the added
image-cached C1 attempt 3 participates in within-C selection; cold C1 attempt 2
is retained separately. Different sessions/startup states remain limitations.

## Next steps within the original scope

1. Keep the B/C thresholds frozen and archive their neighbor candidates.
2. Validate the SkyServe D deployment, metrics, private networking, collector
   execution and lifecycle behavior. The current benchmark-pod / collector Job
   path explicitly supports A/B/C, so D is not yet ready through that path.
3. Finalize matched workload rates and document the phase-drain burst behavior.
   The long-workload calibration does not automatically establish suitable
   rates for short, mixed and bursty workloads.
4. Freeze a final environment/configuration checkpoint before the 48-run
   measured matrix. Calibration runs do not count toward measured repetitions.

No new autoscaler, semantic router or routing optimization was added. Do not
launch all four generated calibration Jobs again; their screening is finished.
The November 7 cluster remains active unless subsequently torn down; this
checkpoint does not assert cleanup or stop AWS billing.
