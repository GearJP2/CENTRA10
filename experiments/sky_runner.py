"""Prepare D collector validation or bounded two-candidate calibration; no cluster changes."""
import argparse
import json
import re
from copy import deepcopy
from pathlib import Path

import yaml

from experiments.config import load


def prepare(base, output, attempt=1, calibration_threshold=None):
    if type(attempt) is not int or not 1 <= attempt <= 999:
        raise ValueError("attempt must be an integer between 1 and 999")
    if base["namespace"] != "centra10" or base["sky_namespace"] != "default":
        raise ValueError("D validation supports centra10 with SkyPilot replicas in default")
    if not re.fullmatch(r"https?://[^\s]+", base["prometheus_internal"]):
        raise ValueError("Configure prometheus_internal before preparing the Job")
    if calibration_threshold is not None and calibration_threshold not in (0.2, 0.1):
        raise ValueError("D calibration only screens thresholds 0.2 and 0.1")
    name, service = f"sky-collector-validation-{attempt}", f"centra10-d{attempt}"
    tag, job_name = str(attempt), f"centra10-sky-collector-{attempt}"
    workload, rate, duration, threshold = "short", 1, 300, 0.2
    if calibration_threshold is not None:
        target = "02" if calibration_threshold == 0.2 else "01"
        name = f"calibration-r2-D-qps{target}-long-1-a{attempt}"
        service = f"centra10-d{target}a{attempt}"
        tag, job_name = f"cal-d{target}-a{attempt}", f"centra10-sky-cal-d{target}-a{attempt}"
        workload, rate, duration, threshold = "long", 0.5, 600, calibration_threshold
    c = {**base, "kube_context": "", "sky_operator_pod": "sky-operator",
         "sky_service": service, "sky_pod_selector": f"centra10-sky-service={service}",
         "benchmark_pod": "benchmark-client", "fixed_replicas": 1,
         "prometheus_url": base["prometheus_internal"], "request_rate": rate,
         "duration_seconds": duration, "qps_threshold": threshold,
         "shutdown_timeout_seconds": 120, "sky_measurement_revision": 1, "seed_offset": 0,
         "lifecycle_revision": "bounded-drain-r2", "diagnostic_only": True,
         "ready_timeout_seconds": max(base["ready_timeout_seconds"], 1800)}
    c.pop("benchmark_url", None)  # D always uses the endpoint discovered from SkyServe.
    c.pop("tokenizer_path", None)
    plan = write_job(c, output, name, job_name, tag, "D", workload)
    print(json.dumps(plan, indent=2))
    return plan


def write_job(c, output, name, job_name, tag, mode, workload, repeat=1):
    """Reuse the validated CPU collector template for one condition."""
    service = c["sky_service"]
    template = list(yaml.safe_load_all(Path("aws/runner.example.yaml").read_text()))
    job = deepcopy(next(d for d in template if d["kind"] == "Job"))
    job["metadata"]["name"] = job_name
    job["spec"]["activeDeadlineSeconds"] = 7200
    spec = job["spec"]["template"]["spec"]
    spec["containers"][0]["args"][0] = (
        spec["containers"][0]["args"][0].replace("full-validation-C-3", name)
        .replace("--modes C --workloads long", f"--modes {mode} --workloads {workload}")
        .replace("--repeats 1", f"--repeats 1 --repeat-start {repeat}"))
    names = {"code": f"sky-collector-code-{tag}", "settings": f"sky-collector-settings-{tag}"}
    for volume in spec["volumes"]:
        if volume["name"] in names:
            volume["configMap"]["name"] = names[volume["name"]]
    maps = [{"apiVersion": "v1", "kind": "ConfigMap",
             "metadata": {"name": names[k], "namespace": "centra10"}, "immutable": True, "data": data}
            for k, data in [("code", {p.name: p.read_text() for p in Path(__file__).parent.glob("*.py")}),
                            ("settings", {"config.json": json.dumps(c),
                                          "requirements.txt": Path("requirements.txt").read_text()})]]
    reader = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
              "metadata": {"name": "centra10-sky-collector", "namespace": "default"},
              "rules": [{"apiGroups": [""], "resources": ["pods", "events"], "verbs": ["get", "list", "watch"]}]}
    binding = {"apiVersion": reader["apiVersion"], "kind": "RoleBinding", "metadata": reader["metadata"],
               "subjects": [{"kind": "ServiceAccount", "name": "experiment-runner", "namespace": "centra10"}],
               "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": reader["metadata"]["name"]}}
    monitor = {"apiVersion": "monitoring.coreos.com/v1", "kind": "PodMonitor",
               "metadata": {"name": f"centra10-sky-{tag}", "namespace": "monitoring"},
               "spec": {"namespaceSelector": {"matchNames": ["default"]},
                        "selector": {"matchLabels": {"centra10-sky-service": service}},
                        "podMetricsEndpoints": [{"port": "http", "path": "/metrics", "interval": "5s",
                            "relabelings": [{"targetLabel": "centra10_service", "replacement": service}]}]}}
    output.mkdir(parents=True, exist_ok=False)
    role = next(d for d in template if d["kind"] == "Role")
    support = [reader, binding, monitor] if mode == "D" else []
    if mode != "D":
        from experiments.render import manifests
        support = manifests(c)["serving.yaml"]
        support[0]["spec"]["replicas"] = 0
    (output / "bundle.yaml").write_text(yaml.safe_dump_all([role, *support, *maps, job], sort_keys=False))
    from experiments.run import phases
    plan = {"job": job["metadata"]["name"], "service": service, "results": name,
            "requests": sum(round(rate * duration) for streams in phases(c, workload) for _, rate, duration in streams),
            "workload": workload, "mode": mode, "repeat": repeat,
            "request_rate": c["request_rate"], "duration_seconds": c["duration_seconds"],
            "target_qps_per_replica": c["qps_threshold"],
            "seed": c.get("seed_offset", 0) + repeat * 100,
            "diagnostic_only": c["diagnostic_only"]}
    (output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    return plan


def prepare_calibration(base, output, attempt=1):
    # Reject invalid attempt IDs before creating the parent directory.
    if type(attempt) is not int or not 1 <= attempt <= 999:
        raise ValueError("attempt must be an integer between 1 and 999")
    output.mkdir(parents=True, exist_ok=False)
    plan = []
    for target, threshold in (("d02", 0.2), ("d01", 0.1)):
        trial = prepare(base, output / target, attempt, calibration_threshold=threshold)
        plan.append({"order": len(plan) + 1, "bundle": f"{target}/bundle.yaml", **trial})
    (output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--calibrate", action="store_true", help="Prepare D0.2 then D0.1 long calibration, applied one at a time")
    args = parser.parse_args()
    if args.calibrate:
        prepare_calibration(load(args.config), args.output, args.attempt)
    else:
        prepare(load(args.config), args.output, args.attempt)


if __name__ == "__main__":
    main()
