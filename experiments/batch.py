"""Run a prepared measured plan sequentially inside one durable Kubernetes Job."""
import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from copy import deepcopy
from pathlib import Path

import yaml


def prepare(root, output, attempt=1):
    if type(attempt) is not int or not 1 <= attempt <= 999:
        raise ValueError("attempt must be an integer between 1 and 999")
    plan = json.loads((root / "plan.json").read_text())
    expected = {(m, w, r) for m in "ABCD" for w in ("short", "long", "mixed", "bursty") for r in (1, 2, 3)}
    if len(plan) != 48 or {(p["mode"], p["workload"], p["repeat"]) for p in plan} != expected:
        raise ValueError("Batch requires the complete 48-condition measured plan")
    trials, support, serving, sources = [], {}, None, {}
    for row in plan:
        path = (root / row["bundle"]).resolve()
        if not path.is_relative_to(root.resolve()) or not re.fullmatch(r"[A-Za-z0-9-]+", row["results"]):
            raise ValueError("Unsafe prepared artifact path")
        docs = list(yaml.safe_load_all(path.read_text()))
        maps = [d for d in docs if d["kind"] == "ConfigMap"]
        settings = next(d["data"] for d in maps if "config.json" in d["data"])
        code = next(d["data"] for d in maps if "run.py" in d["data"])
        if any(Path(name).name != name or not name.endswith(".py") for name in code):
            raise ValueError("Unsafe prepared code filename")
        c = json.loads(settings["config.json"])
        if c.get("study_phase") != "measured" or c.get("diagnostic_only") is not False or c["namespace"] != "centra10":
            raise ValueError("Batch only accepts measured centra10 configurations")
        code_id = hashlib.sha256(json.dumps(code, sort_keys=True).encode()).hexdigest()
        sources.setdefault(code_id, code)
        trials.append({"plan": row, "config": c, "code_id": code_id})
        for doc in docs:
            if doc["kind"] in ("Role", "RoleBinding", "PodMonitor"):
                key = (doc["kind"], doc["metadata"]["namespace"], doc["metadata"]["name"])
                if key in support and support[key] != doc:
                    raise ValueError("Conflicting prepared support resources")
                support[key] = doc
            if doc["kind"] == "Deployment":
                candidate = doc["spec"]["template"]["spec"]
                if serving is not None and serving != candidate:
                    raise ValueError("Prepared serving lifecycle differs between trials")
                serving = candidate
    if serving is None:
        raise ValueError("Missing serving baseline")
    payload = gzip.compress(json.dumps({"trials": trials, "serving": serving, "sources": sources}).encode(), mtime=0)
    if len(payload) > 900 * 1024:
        raise ValueError("Prepared payload is too large for a ConfigMap")
    tag = f"measured-batch-v1-a{attempt}"
    template = list(yaml.safe_load_all(Path("aws/runner.example.yaml").read_text()))
    job = deepcopy(next(d for d in template if d["kind"] == "Job"))
    job["metadata"]["name"] = f"centra10-{tag}"
    job["spec"]["activeDeadlineSeconds"] = 86400
    spec = job["spec"]["template"]["spec"]
    spec["containers"][0]["args"] = [
        "set -euo pipefail\nexec python3 -u -m experiments.batch "
        f"--execute --payload /settings/plan.json.gz --output /models/experiment-results/{tag} "
        f"--job-name centra10-{tag}\n"]
    for volume in spec["volumes"]:
        if volume["name"] in ("code", "settings"):
            volume["configMap"]["name"] = f"{tag}-{volume['name']}"
    code = {p.name: p.read_text() for p in Path(__file__).parent.glob("*.py")}
    observer = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
                "metadata": {"name": "measured-batch-observer", "namespace": "centra10"},
                "rules": [{"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["get", "list"]}]}
    binding = {"apiVersion": observer["apiVersion"], "kind": "RoleBinding", "metadata": observer["metadata"],
               "subjects": [{"kind": "ServiceAccount", "name": "experiment-runner", "namespace": "centra10"}],
               "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": observer["metadata"]["name"]}}
    code_map = {"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
                "metadata": {"name": f"{tag}-code", "namespace": "centra10"}, "data": code}
    settings_map = {"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
                    "metadata": {"name": f"{tag}-settings", "namespace": "centra10"},
                    "binaryData": {"plan.json.gz": base64.b64encode(payload).decode()}}
    output.mkdir(parents=True, exist_ok=False)
    (output / "bundle.yaml").write_text(yaml.safe_dump_all(
        [*support.values(), observer, binding, code_map, settings_map, job], sort_keys=False))
    print(f"Prepared {job['metadata']['name']} with 48 conditions; existing completed runs are verified and skipped.")


def jobs():
    result = subprocess.run(["kubectl", "-n", "centra10", "get", "jobs", "-o", "json"],
                            check=True, capture_output=True, text=True, timeout=60)
    return {j["metadata"]["name"]: j for j in json.loads(result.stdout)["items"]}


def verify(trial, root):
    from experiments.analyze import summarize
    row = trial["plan"]
    folder = root / row["results"] / f'{row["workload"]}_{row["mode"]}_run{row["repeat"]}'
    actual = json.loads((folder / "config.json").read_text())
    actual.pop("tokenizer_path", None)
    if actual != trial["config"]:
        raise ValueError(f"Configuration mismatch in {folder}")
    meta = json.loads((folder / "run.json").read_text())
    if meta.get("cleanup_error"):
        raise ValueError(f"Cleanup failed in {folder}")
    result, _, _ = summarize(folder)
    if (result["mode"], result["workload"], result["repeat"], result["requests"]) != (
            row["mode"], row["workload"], row["repeat"], row["requests"]):
        raise ValueError(f"Condition or request count mismatch in {folder}")
    return result


def baseline(expected):
    result = subprocess.run(["kubectl", "-n", "centra10", "get", "deployment", "vllm", "-o", "json"],
                            check=True, text=True, capture_output=True, timeout=60)
    deployment = json.loads(result.stdout)
    if deployment["spec"].get("replicas", 0) != 0:
        raise RuntimeError("vllm deployment is still allocated; inspect cleanup before continuing")
    actual = deployment["spec"]["template"]["spec"]
    for key in ("nodeSelector", "enableServiceLinks", "terminationGracePeriodSeconds"):
        if actual.get(key) != expected.get(key):
            raise ValueError(f"Serving baseline mismatch: {key}")
    a = next(c for c in actual["containers"] if c["name"] == "vllm")
    e = next(c for c in expected["containers"] if c["name"] == "vllm")
    for key in ("image", "args"):
        if a[key] != e[key]:
            raise ValueError(f"Serving baseline mismatch: {key}")
    if {v["name"]: v.get("value") for v in a["env"]} != {v["name"]: v.get("value") for v in e["env"]}:
        raise ValueError("Serving baseline environment differs")
    pods = subprocess.run(["kubectl", "-n", "default", "get", "pods", "-o", "json"],
                          check=True, text=True, capture_output=True, timeout=60)
    for pod in json.loads(pods.stdout)["items"]:
        if pod.get("status", {}).get("phase") in ("Pending", "Running") and any(
                float(c.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0)) > 0
                for c in pod["spec"]["containers"]):
            raise RuntimeError(f"SkyPilot GPU pod remains: {pod['metadata']['name']}; inspect cleanup")


def execute(payload, output, job_name, root=Path("/models/experiment-results")):
    output.mkdir(parents=True, exist_ok=False)
    lock = root / ".measured-batch.lock"
    # O_EXCL is atomic on the shared EFS filesystem; stale locks require inspection.
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(fd, json.dumps({"job": job_name, "progress": str(output / "progress.json")}).encode())
    os.close(fd)
    progress = {"status": "running", "current": None, "completed": [], "started_at": time.time()}

    def save():
        temp = output / "progress.json.tmp"
        temp.write_text(json.dumps(progress, indent=2) + "\n")
        temp.replace(output / "progress.json")

    try:
        data = json.loads(gzip.decompress(payload.read_bytes()))
        planned = {t["plan"]["job"] for t in data["trials"]}
        for trial in data["trials"]:
            row = trial["plan"]
            progress["current"] = row["results"]
            save()
            while True:
                inventory = jobs()
                active = [name for name, j in inventory.items() if name != job_name and j.get("status", {}).get("active", 0)]
                if len(active) > 1 or any(name not in planned for name in active):
                    raise RuntimeError(f"Conflicting active Jobs: {active}")
                job = inventory.get(row["job"])
                conditions = {c["type"] for c in (job or {}).get("status", {}).get("conditions", []) if c["status"] == "True"}
                if "Failed" in conditions:
                    raise RuntimeError(f"Existing Job failed: {row['job']}; inspect before retrying")
                result_dir = root / row["results"]
                if "Complete" in conditions or (result_dir / "batch-complete.json").exists():
                    result = verify(trial, root)
                    print(f"Verified existing {row['results']}", flush=True)
                    break
                if job is not None:
                    print(f"Waiting for existing Job {row['job']}", flush=True)
                    time.sleep(15)
                    continue
                if active:
                    raise RuntimeError(f"Later planned Job already active: {active}")
                if result_dir.exists():
                    raise RuntimeError(f"Unverified existing results: {result_dir}; inspect before retrying")
                baseline(data["serving"])
                result_dir.mkdir()
                with tempfile.TemporaryDirectory(prefix="centra-measured-") as temp:
                    work = Path(temp)
                    code = work / "experiments"
                    code.mkdir()
                    for name, contents in data["sources"][trial["code_id"]].items():
                        (code / name).write_text(contents)
                    config = work / "config.json"
                    config.write_text(json.dumps(trial["config"]))
                    argv = [sys.executable, "-u", "-m", "experiments.run", "--config", str(config),
                            "--output", str(result_dir), "--modes", row["mode"], "--workloads", row["workload"],
                            "--repeats", "1", "--repeat-start", str(row["repeat"]), "--execute"]
                    print(f"Starting {row['results']}", flush=True)
                    with (result_dir / "runner.log").open("w") as log:
                        subprocess.run(argv, cwd=work, env={**os.environ, "PYTHONPATH": str(work)},
                                       stdout=log, stderr=subprocess.STDOUT, check=True, timeout=7200)
                result = verify(trial, root)
                break
            (root / row["results"] / "batch-complete.json").write_text(json.dumps(result, indent=2) + "\n")
            progress["completed"].append(result)
            save()
            print(f"Completed {len(progress['completed'])}/48: {row['results']}; failed requests={result['failed']}", flush=True)
        progress.update(status="complete", current=None, ended_at=time.time())
    except BaseException as exc:
        progress.update(status="failed", error=str(exc), ended_at=time.time())
        raise
    finally:
        save()
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--plan-root", type=Path)
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--job-name")
    args = parser.parse_args()
    if args.prepare == args.execute:
        parser.error("Choose exactly one of --prepare and --execute")
    if args.prepare:
        if not args.plan_root:
            parser.error("--prepare requires --plan-root")
        prepare(args.plan_root, args.output, args.attempt)
    else:
        if not args.payload or not args.job_name:
            parser.error("--execute requires --payload and --job-name")
        execute(args.payload, args.output, args.job_name)


if __name__ == "__main__":
    main()
