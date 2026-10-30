import json
import math
from pathlib import Path


def load(path):
    config = json.loads(Path(path).read_text())
    for key in ("qps_threshold", "queue_threshold", "request_rate", "duration_seconds",
                "burst_phase_seconds", "sample_seconds", "ready_timeout_seconds",
                "run_timeout_seconds"):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be a finite positive number")
    return config


def queries(config, mode):
    if mode == "D":
        result = dict(config["sky_queries"])
        if not result.get("running"):
            raise ValueError("Configure sky_queries.running for the revised brief")
        result.setdefault("kv_cache", None)
        return result
    ns = config["namespace"]
    selector = f'namespace="{ns}",service="vllm"'
    return {
        "replicas": f'sum(kube_pod_container_resource_requests{{namespace="{ns}",container="vllm",resource="nvidia_com_gpu"}} and on(namespace,pod) (kube_pod_status_scheduled{{namespace="{ns}",condition="true"}} == 1) and on(namespace,pod) (kube_pod_status_phase{{namespace="{ns}",phase=~"Pending|Running"}} == 1))',
        "ready": f'sum(kube_deployment_status_replicas_available{{namespace="{ns}",deployment="vllm"}})',
        "queue": f'sum(vllm:num_requests_waiting{{{selector}}})',
        "running": f'sum(vllm:num_requests_running{{{selector}}})',
        "kv_cache": f'max(vllm:kv_cache_usage_perc{{{selector}}})',
        "qps": f'sum(rate(vllm:request_success_total{{{selector}}}[1m]))',
    }
