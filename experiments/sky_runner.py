"""Prepare one D-short collector validation Job; no cluster changes."""
import argparse
import json
import re
from copy import deepcopy
from pathlib import Path

import yaml

from experiments.config import load


def prepare(base, output, attempt=1):
    if type(attempt) is not int or not 1 <= attempt <= 999:
        raise ValueError("attempt must be an integer between 1 and 999")
    if base["namespace"] != "centra10" or base["sky_namespace"] != "default":
        raise ValueError("D validation supports centra10 with SkyPilot replicas in default")
    if not re.fullmatch(r"https?://[^\s]+", base["prometheus_internal"]):
        raise ValueError("Configure prometheus_internal before preparing the Job")
    name, service = f"sky-collector-validation-{attempt}", f"centra10-d{attempt}"
    c = {**base, "kube_context": "", "sky_operator_pod": "sky-operator",
         "sky_service": service, "sky_pod_selector": f"centra10-sky-service={service}",
         "benchmark_pod": "benchmark-client", "fixed_replicas": 1,
         "prometheus_url": base["prometheus_internal"], "request_rate": 1,
         "duration_seconds": 300, "qps_threshold": 0.2,
         "shutdown_timeout_seconds": 120, "sky_measurement_revision": 1,
         "lifecycle_revision": "bounded-drain-r2", "diagnostic_only": True,
         "ready_timeout_seconds": max(base["ready_timeout_seconds"], 1800)}
    c.pop("benchmark_url", None)  # D always uses the endpoint discovered from SkyServe.
    c.pop("tokenizer_path", None)
    template = list(yaml.safe_load_all(Path("aws/runner.example.yaml").read_text()))
    job = deepcopy(next(d for d in template if d["kind"] == "Job"))
    job["metadata"]["name"] = f"centra10-sky-collector-{attempt}"
    job["spec"]["activeDeadlineSeconds"] = 7200
    spec = job["spec"]["template"]["spec"]
    spec["containers"][0]["args"][0] = (
        spec["containers"][0]["args"][0].replace("full-validation-C-3", name)
        .replace("--modes C --workloads long", "--modes D --workloads short"))
    names = {"code": f"sky-collector-code-{attempt}", "settings": f"sky-collector-settings-{attempt}"}
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
               "metadata": {"name": f"centra10-sky-{attempt}", "namespace": "monitoring"},
               "spec": {"namespaceSelector": {"matchNames": ["default"]},
                        "selector": {"matchLabels": {"centra10-sky-service": service}},
                        "podMetricsEndpoints": [{"port": "http", "path": "/metrics", "interval": "5s",
                            "relabelings": [{"targetLabel": "centra10_service", "replacement": service}]}]}}
    output.mkdir(parents=True, exist_ok=False)
    role = next(d for d in template if d["kind"] == "Role")
    (output / "bundle.yaml").write_text(yaml.safe_dump_all([role, reader, binding, monitor, *maps, job], sort_keys=False))
    plan = {"job": job["metadata"]["name"], "service": service, "results": name,
            "requests": 300, "workload": "short", "diagnostic_only": True}
    (output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    print(json.dumps(plan, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--attempt", type=int, default=1)
    args = parser.parse_args()
    prepare(load(args.config), args.output, args.attempt)


if __name__ == "__main__":
    main()
