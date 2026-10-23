"""Validate raw results and produce CSV tables and the three required figures."""
import argparse
import csv
import json
import math
import statistics
from datetime import datetime
from pathlib import Path


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * p / 100
    lo, hi = math.floor(index), math.ceil(index)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def timings(result, expected):
    times, errors = result["ttfts"], result["errors"]
    if len(times) != expected or len(errors) != expected:
        raise ValueError("Missing per-request data: use --save-detailed and verify the vLLM schema")
    successful = [float(t) for t, error in zip(times, errors) if not error]
    if len(successful) != result["completed"]:
        raise ValueError("Completed count and per-request errors disagree")
    if any(not math.isfinite(t) or t <= 0 for t in successful):
        raise ValueError("Successful request has invalid TTFT (expected positive seconds)")
    return successful


def gpu_hours(samples, start, end, interval):
    if len(samples) < 2 or end <= start:
        raise ValueError("Insufficient metric samples or invalid measurement window")
    if any(not math.isfinite(s["timestamp"]) or not math.isfinite(s["replicas"]) or s["replicas"] < 0 for s in samples):
        raise ValueError("Invalid timestamp or allocated replica count")
    if samples[0]["timestamp"] > start + interval * 2 or samples[-1]["timestamp"] < end - interval * 2:
        raise ValueError("Metrics do not cover the measurement window")
    area = 0
    for a, b in zip(samples, samples[1:]):
        gap = b["timestamp"] - a["timestamp"]
        if gap <= 0 or gap > max(30, interval * 3):
            raise ValueError("Non-monotonic timestamps or a gap in telemetry")
        area += a["replicas"] * max(0, min(end, b["timestamp"]) - max(start, a["timestamp"]))
    # Sampling boundaries are at most one scrape interval from the run boundaries.
    area += samples[0]["replicas"] * max(0, samples[0]["timestamp"] - start)
    area += samples[-1]["replicas"] * max(0, end - samples[-1]["timestamp"])
    return area / 3600


def cold_starts(snapshots, start):
    found = {}
    for snapshot in snapshots:
        for pod in snapshot["pods"]["items"]:
            status = pod.get("status", {})
            began = status.get("startTime")
            if not began:
                continue
            began = datetime.fromisoformat(began.replace("Z", "+00:00")).timestamp()
            if began < start:
                continue
            for condition in status.get("conditions", []):
                if condition["type"] == "Ready" and condition["status"] == "True":
                    ready = datetime.fromisoformat(condition["lastTransitionTime"].replace("Z", "+00:00")).timestamp()
                    found.setdefault(pod["metadata"]["uid"], {
                        "pod": pod["metadata"]["name"], "start": began, "ready": ready,
                        "cold_start_seconds": ready - began})
    return list(found.values())


def summarize(folder):
    meta = json.loads((folder / "run.json").read_text())
    if meta["status"] != "complete":
        raise ValueError(f"{folder}: incomplete run ({meta['status']})")
    config = json.loads((folder / "config.json").read_text())
    samples = json.loads((folder / "metrics.json").read_text())
    times, requested = [], 0
    for phase in meta["phases"]:
        for stream in phase["streams"]:
            times.extend(timings(json.loads((folder / stream["file"]).read_text()), stream["requests"]))
            requested += stream["requests"]
    if requested == 0:
        raise ValueError("No benchmark requests recorded")
    response = None
    if meta["workload"] == "bursty":
        high = meta["phases"][1]["start"]
        before = [s["ready"] for s in samples if s["timestamp"] <= high]
        baseline = before[-1] if before else samples[0]["ready"]
        increased = [s["timestamp"] for s in samples if s["timestamp"] >= high and s["ready"] > baseline]
        response = increased[0] - high if increased else None
    row = {
        "mode": meta["mode"], "workload": meta["workload"], "repeat": meta["repeat"],
        "requests": requested, "completed": len(times), "failed": requested - len(times),
        "ttft_p50_seconds": percentile(times, 50), "ttft_p99_seconds": percentile(times, 99),
        "slo_attainment": sum(t <= 2 for t in times) / requested,
        "gpu_hours": gpu_hours(samples, meta["start"], meta["end"], config["sample_seconds"]),
        "response_seconds": response,
    }
    snapshots = json.loads((folder / "pods.json").read_text())
    return row, samples, cold_starts(snapshots, meta["start"])


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def plots(rows, series, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = dict(zip("ABCD", ["#666666", "#0072B2", "#D55E00", "#009E73"]))
    fig, ax = plt.subplots(figsize=(7, 5), layout="constrained")
    for mode in "ABCD":
        group = [r for r in rows if r["mode"] == mode and r["workload"] == "mixed"]
        if group:
            ax.scatter([r["gpu_hours"] for r in group], [r["slo_attainment"] * 100 for r in group], label=mode, color=colors[mode])
    ax.set(xlabel="GPU-hours per measured run", ylabel="TTFT ≤ 2 s (% of all requests)", ylim=(0, 101), title="Mixed workload: quality versus cost (each point is one run)")
    if ax.collections:
        ax.legend(title="Configuration")
    fig.savefig(output / "mixed-slo-cost.png", dpi=200)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(12, 4), layout="constrained")
    for ax, mode in zip(axes, "BC"):
        twin = ax.twinx()
        for row, samples in series:
            if row["mode"] == mode and row["workload"] == "bursty":
                elapsed = [(s["timestamp"] - samples[0]["timestamp"]) / 60 for s in samples]
                ax.step(elapsed, [s["ready"] for s in samples], where="post", color=colors[mode], alpha=.65)
                twin.plot(elapsed, [s["queue"] for s in samples], color="#777777", linestyle="--", alpha=.65)
        ax.set(xlabel="Elapsed time (min)", ylabel="Ready replicas (solid)", title=f"{mode}: bursty, all repeats", ylim=(0, 3.5))
        twin.set_ylabel("Waiting requests (dashed)")
    fig.savefig(output / "bursty-replicas-queue.png", dpi=200)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 5), layout="constrained")
    workloads = ["short", "long", "mixed", "bursty"]
    for i, mode in enumerate("ABCD"):
        xs, ys = [], []
        for j, workload in enumerate(workloads):
            for row in rows:
                if row["mode"] == mode and row["workload"] == workload and row["ttft_p99_seconds"] is not None:
                    xs.append(j + (i - 1.5) * .15)
                    ys.append(row["ttft_p99_seconds"])
        ax.scatter(xs, ys, color=colors[mode], label=mode)
    ax.axhline(2, color="black", linestyle="--", label="2 s SLO")
    ax.set(xticks=range(4), xticklabels=workloads, ylabel="TTFT p99 (seconds, successful requests)", title="Tail latency: each point is one run")
    ax.legend()
    fig.savefig(output / "ttft-p99.png", dpi=200)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--output", type=Path, default=Path("results/analysis"))
    parser.add_argument("--require-complete", action="store_true", help="Require exactly 48 unique conditions")
    args = parser.parse_args()
    rows, series, cold = [], [], []
    for path in sorted(args.results.glob("*/run.json")):
        row, samples, starts = summarize(path.parent)
        rows.append(row)
        series.append((row, samples))
        cold.extend({"mode": row["mode"], "workload": row["workload"], "repeat": row["repeat"], **s} for s in starts)
    if not rows:
        parser.error("No completed result folders found")
    actual = {(r["mode"], r["workload"], r["repeat"]) for r in rows}
    expected = {(m, w, r) for m in "ABCD" for w in ("short", "long", "mixed", "bursty") for r in (1, 2, 3)}
    if len(actual) != len(rows) or (args.require_complete and actual != expected):
        parser.error(f"Expected 48 unique runs; missing {sorted(expected - actual)}, extra {sorted(actual - expected)}")
    args.output.mkdir(parents=True, exist_ok=True)
    write_csv(args.output / "runs.csv", rows)
    write_csv(args.output / "cold-starts.csv", cold)
    groups = []
    for mode, workload in sorted({(r["mode"], r["workload"]) for r in rows}):
        group = [r for r in rows if (r["mode"], r["workload"]) == (mode, workload)]
        summary = {"mode": mode, "workload": workload, "repeats": len(group)}
        for key in ("slo_attainment", "gpu_hours", "ttft_p99_seconds"):
            values = [r[key] for r in group if r[key] is not None]
            summary[key + "_mean"] = statistics.mean(values) if values else None
            summary[key + "_std"] = statistics.stdev(values) if len(values) > 1 else None
        groups.append(summary)
    write_csv(args.output / "summary.csv", groups)
    plots(rows, series, args.output)
    print(f"Analyzed {len(rows)}/48 runs into {args.output}; no missing runs were imputed.")


if __name__ == "__main__":
    main()
