"""Prepare 48 separate measured Jobs from frozen thresholds; never applies them."""
import argparse
import json
from pathlib import Path

from experiments.config import load
from experiments.sky_runner import write_job


def prepare(base, output, attempt=1):
    if type(attempt) is not int or not 1 <= attempt <= 999:
        raise ValueError("attempt must be an integer between 1 and 999")
    if base["namespace"] != "centra10" or base["sky_namespace"] != "default":
        raise ValueError("Measured Jobs require centra10 and SkyPilot namespace default")
    pins = json.loads(Path("versions.json").read_text())
    for key, pin in (("model", "model"), ("model_revision", "model_revision"), ("vllm_version", "vllm")):
        if base[key] != pins[pin]:
            raise ValueError(f"Measured {key} differs from the pinned experiment")
    bc = json.loads(Path("docs/frozen-calibration-r2.json").read_text())
    d = json.loads(Path("docs/frozen-sky-calibration-r2.json").read_text())
    if any(r["status"] != "frozen_after_bounded_screening" for r in (bc, d)):
        raise ValueError("Both threshold records must be frozen")
    output.mkdir(parents=True, exist_ok=False)
    plan = []
    for repeat in (1, 2, 3):
        order = "ABCD"[repeat - 1:] + "ABCD"[:repeat - 1]
        for workload in ("short", "long", "mixed", "bursty"):
            for mode in order:
                tag = f"m{repeat}-{workload}-{mode.lower()}-a{attempt}"
                name = f"measured-r{repeat}-{workload}-{mode}-a{attempt}"
                service = f"centra10-m{repeat}{workload[0]}{mode.lower()}a{attempt}"
                c = {**base, "kube_context": "", "fixed_replicas": 1,
                     "shutdown_timeout_seconds": bc["shutdown_timeout_seconds"],
                     "lifecycle_revision": bc["lifecycle_revision"], "seed_offset": 1000,
                     "request_rate": 0.5, "duration_seconds": 600, "burst_phase_seconds": 180,
                     "qps_threshold": d["sky_qps_threshold"] if mode == "D" else bc["qps_threshold"],
                     "queue_threshold": bc["queue_threshold"], "sample_seconds": 5,
                     "prometheus_url": base["prometheus_internal"],
                     "k8s_url": "http://vllm-proxy:8000", "benchmark_url": "http://vllm-proxy:8000",
                     "benchmark_pod": "benchmark-client", "sky_service": service,
                     "sky_pod_selector": f"centra10-sky-service={service}",
                     "diagnostic_only": False, "study_phase": "measured",
                     "threshold_evidence": {"B_C": bc["evidence_commit"], "D": d["evidence_commit"]}}
                c.pop("tokenizer_path", None)
                c.pop("sky_operator_pod", None)
                if mode == "D":
                    c.update(sky_operator_pod="sky-operator", sky_measurement_revision=1)
                    c.pop("benchmark_url")
                trial = write_job(c, output / tag, name, f"centra10-{tag}", tag, mode, workload, repeat)
                plan.append({"order": len(plan) + 1, "bundle": f"{tag}/bundle.yaml", **trial})
    (output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    print(f"Prepared {len(plan)} separate Jobs in {output}; apply one bundle at a time, never the directory.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--attempt", type=int, default=1)
    args = parser.parse_args()
    prepare(load(args.config), args.output, args.attempt)


if __name__ == "__main__":
    main()
