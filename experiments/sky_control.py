"""SkyPilot commands through the existing operator, with machine-readable status."""
import json
import subprocess
import sys


def argv(c, args, python=False):
    if not c.get("sky_operator_pod"):
        return [sys.executable if python else "sky", *args]
    context = ["--context", c["kube_context"]] if c.get("kube_context") else []
    return ["kubectl", *context, "-n", c["namespace"], "exec", c["sky_operator_pod"],
            "-c", "operator", "--", "env",
            "PATH=/opt/sky/bin:/tools:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "/opt/sky/bin/python" if python else "/opt/sky/bin/sky", *args]


def status(c, command):
    script = """import json, sys, sky
from sky import serve
try:
    records = sky.get(serve.status(sys.argv[1]))
except sky.exceptions.ClusterNotUpError as exc:
    if str(exc).strip() != 'No live services.':
        raise
    records = []
def name(value):
    return value.name if hasattr(value, 'name') else str(value)
print(json.dumps([{
    'name': s['name'], 'active_versions': s['active_versions'],
    'status': name(s['status']), 'endpoint': s['endpoint'],
    'replica_info': [{k: name(r[k]) if k == 'status' else r.get(k)
        for k in ('replica_id', 'name', 'status', 'version', 'launched_at', 'endpoint')}
        for r in s['replica_info']]
} for s in records]))
"""
    try:
        result = command(argv(c, ["-c", script, c["sky_service"]], python=True), capture_output=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"SkyServe status failed:\n{exc.stderr or exc.stdout or str(exc)}") from exc
    records = json.loads(result.stdout)
    if len(records) > 1 or any(s["name"] != c["sky_service"] for s in records):
        raise ValueError("SkyPilot returned unexpected services")
    return records[0] if records else None


def allocated(pods):
    total = 0
    for pod in pods["items"]:
        state = pod.get("status", {})
        if state.get("phase") not in ("Pending", "Running"):
            continue
        if not any(c["type"] == "PodScheduled" and c["status"] == "True"
                   for c in state.get("conditions", [])):
            continue
        total += sum(float(c.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0))
                     for c in pod["spec"]["containers"])
    return total


def ready(state):
    if state is None:
        raise ValueError("SkyServe service disappeared")
    return sum(r["status"] == "READY" for r in state["replica_info"])
