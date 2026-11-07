# Resumed revision-2 C1: cold-image startup confound

Run `calibration-r2-C-queue1-long-1-a2` completed 300/300 requests without
failures or cleanup error. TTFT p50=103.236 s, p99=221.515 s, SLO attainment
12%, allocated GPU-hours=0.62199. It ran on the recreated November 7 cluster.

Both added replicas were scheduled promptly and had zero container restarts.
Kubernetes events explicitly report image pulls of 233.269 and 230.938 seconds
for the 9,205,781,514-byte vLLM image. Their creation-to-Ready durations were
454 and 449 seconds; container-start-to-Ready was approximately 218 and
216 seconds. Thus the long startup is not evidence that C delayed its initial
scaling decision: HPA requested two and three replicas approximately 21 and
36 seconds after first client dispatch. The last of the new pods became Ready
around +485 seconds by pod conditions (the aggregate scrape saw it later).

Image IDs in both this run and the November 6 revision-2 B0.2 run match:
`sha256:befccf0e7e4089aa23926bb6f887a85c0d46c2a99e6de5c5ffdf5cb7957c5b34`.
Matching image bytes does not imply matching cache, startup or cluster state.

## Prospective handling before the remaining candidates

Retain this run as cold-image evidence, not as the sole C1 threshold-selection
trial against a subsequently image-cached C2 trial. Add one explicitly labeled
warm-image C1 trial (`calibration-r2-C-queue1-long-1-a3`) before C2. Do not
change workload, thresholds, seed, lifecycle or routing. All three GPU nodes
have now run the same image; check that no node replacement occurred before
launch and inspect pull events afterward. If a new node or cold image appears,
record it and stop screening for review rather than silently adding repeats.

This adds one calibration run to address a documented environmental confound;
it does not expand the threshold search. Use the warm-image C1 and C2 outcomes
for within-C threshold selection, preserving the cold run in reports. Keep
B0.2's already completed image-cached result and continue B0.1 after C2.
This is not a matched randomized comparison across sessions: report that
limitation, and use the eventual measured repeats to assess variability.

Prepare into a new local folder with `experiments.calibration --attempt 3`,
and apply **only c1.yaml** from that folder. C2 and B0.1 retain the existing
attempt-2 bundles and output names. No B0.2 rerun is required by this amendment.
Do not regenerate or mutate active immutable ConfigMaps.

The original cold C1 observations are valid serving-system evidence; treating
image-pull delay as if it were solely a trigger effect would be misleading.
No GPU experiment was launched while writing this note.
