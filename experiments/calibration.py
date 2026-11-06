"""Prepare four bounded-drain revision-2 calibration Jobs; does not contact AWS."""
import argparse
import json
from copy import deepcopy
from pathlib import Path

import yaml

from experiments.config import load
from experiments.render import manifests


TRIALS = [("b02", "B", "qps02", 0.2, 2),
          ("c1", "C", "queue1", 0.2, 1),
          ("c2", "C", "queue2", 0.2, 2),
          ("b01", "B", "qps01", 0.1, 2)]


def prepare(base, output):
    if base["namespace"] != "centra10":
        raise ValueError("Calibration Job template supports namespace centra10 only")
    template = list(yaml.safe_load_all(Path("aws/runner.example.yaml").read_text()))
    role = next(d for d in template if d["kind"] == "Role")
    job_template = next(d for d in template if d["kind"] == "Job")
    code = {p.name: p.read_text() for p in Path(__file__).parent.glob("*.py")}
    output.mkdir(parents=True, exist_ok=False)
    plan = []
    for key, mode, target, qps, queue in TRIALS:
        name = f"calibration-r2-{mode}-{target}-long-1"
        job_name = f"centra10-cal-r2-{key}"
        c = {**base, "fixed_replicas": 1, "shutdown_timeout_seconds": 120,
             "lifecycle_revision": "bounded-drain-r2", "request_rate": 0.5,
             "duration_seconds": 600, "qps_threshold": qps, "queue_threshold": queue,
             "kube_context": "", "k8s_url": "http://vllm-proxy:8000",
             "prometheus_url": base["prometheus_internal"],
             "benchmark_pod": "benchmark-client", "benchmark_url": "http://vllm-proxy:8000"}
        job = deepcopy(job_template)
        job["metadata"]["name"] = job_name
        spec = job["spec"]["template"]["spec"]
        spec["containers"][0]["args"][0] = (
            spec["containers"][0]["args"][0].replace("full-validation-C-3", name)
            .replace("--modes C", f"--modes {mode}"))
        names = {"code": f"cal-r2-code-{key}", "settings": f"cal-r2-settings-{key}"}
        for volume in spec["volumes"]:
            if volume["name"] in names:
                volume["configMap"]["name"] = names[volume["name"]]
        maps = [{"apiVersion": "v1", "kind": "ConfigMap",
                 "metadata": {"name": names[kind], "namespace": c["namespace"]},
                 "immutable": True, "data": data}
                for kind, data in [("code", code), ("settings", {"config.json": json.dumps(c)})]]
        # Apply only one bundle at a time: it also explicitly installs the shared lifecycle settings.
        (output / f"{key}.yaml").write_text(yaml.safe_dump_all(
            [role, *maps, *manifests(c)["serving.yaml"], job], sort_keys=False))
        plan.append({"order": len(plan) + 1, "mode": mode, "qps_threshold": qps,
                     "queue_threshold": queue, "job": job_name, "results": name,
                     "bundle": f"{key}.yaml"})
    (output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    print(json.dumps(plan, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(load(args.config), args.output)


if __name__ == "__main__":
    main()
