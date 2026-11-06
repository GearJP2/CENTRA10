# Pause checkpoint — 6 November 2026

Latest completed calibration: revision 2 B, completed-QPS target 0.2,
300 long requests at 0.5 req/s for 600 seconds, repeat 1 / seed 100.
Result commit: `244e230`.

- Run status complete; no run or cleanup error; 300 completed, zero failed.
- TTFT p50 31.444 s; p99 111.365 s; two-second SLO attainment 27%.
- Allocated pod GPU-hours 0.42657, not an AWS invoice estimate.
- Saved config and serving manifest confirm shutdown timeout 120 seconds and
  Kubernetes termination grace 150 seconds.
- [Raw run](../results/calibration-r2-B-qps02-long-1-a2/long_B_run1/run.json)
- [Analysis](../results/calibration-r2-B-qps02-long-1-a2-analysis/runs.csv)

## Resume without repeating completed work

Recreate infrastructure after teardown. Old cluster/EFS/subnet/security-group
IDs and CloudShell endpoint IP allowlists are not reusable. Restore model
storage, monitoring, KEDA, CPU benchmark pod and in-cluster collector. Use the
common bounded-drain configuration for B and C; confirm the deployed manifest
matches the saved lifecycle settings. No additional drain experiment is needed
unless lifecycle software/configuration changes or evidence contradicts it.

Remaining revision-2 screening order:

1. C waiting queue target 1.
2. C waiting queue target 2.
3. B completed QPS target 0.1.

All retain 0.5 req/s, 600 seconds, long workload, seed 100. Use distinct Jobs,
ConfigMaps and outputs. Preparing attempt 2 again in a fresh environment
produces the completed B0.2 bundle too: **do not apply it**. It is already
backed up. Keep old original-lifecycle screening separate from revision 2.
Freeze thresholds only after the remaining candidates are evaluated according
to [the prospective protocol](calibration-protocol.md). No screening result is
a measured repeat. The calibrated 48-run matrix and SkyServe validation remain
pending. No routing optimization or semantic router was added to the scope.

## Evidence and cleanup boundary

Earlier smoke, B/C validation, fixed-three diagnostics, original-lifecycle
screening, and both controlled drain probes are archived in Git. The C3 RBAC
and first revision-2 packaging failures are setup failures, not policy results.
The interrupted C1 collector run remains incomplete even though its benchmark
JSON was recovered. Generated Kubernetes Jobs have no automatic retries.

The user requested a full teardown and pause after backing up this last run.
At the time this checkpoint was written, teardown had **not** yet been verified.
Remove EFS access points/mount targets/filesystem and the EFS security group,
then node-group, cluster and addon IAM stacks; inventory billable leftovers.
Namespace deletion also removes the CPU client, collector Jobs and EFS reader.
Export anything else needed before deleting the namespace. Worker EC2 instances
continue billing even when serving replicas are zero.

The cluster-scoped collector RBAC was created directly, not by CloudFormation.
Delete ClusterRoleBinding and ClusterRole named
`centra10-runner-version-inventory` before removing the Kubernetes API.
IAM roles/stacks and EFS are not all owned by the cluster stack. Earlier
charges can still appear in billing after a successful teardown.
