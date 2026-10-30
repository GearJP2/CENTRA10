"""Render all manifests from one experimental configuration."""
import argparse
import json
from copy import deepcopy
from pathlib import Path

import yaml

from experiments.config import load, queries


def manifests(c):
    ns, model, version = c["namespace"], c["model"], c["vllm_version"]
    image = f"vllm/vllm-openai:v{version}"
    args = ["--model", model, "--revision", c["model_revision"], "--max-model-len", "8192",
            "--no-enable-prefix-caching"]
    volume = {"name": "models", "persistentVolumeClaim": {"claimName": "model-cache"}}
    mount = {"name": "models", "mountPath": "/models"}
    container = {
        "name": "vllm", "image": image, "args": args,
        "env": [{"name": "HF_HOME", "value": "/models"}, {"name": "HF_HUB_OFFLINE", "value": "1"}],
        "ports": [{"name": "http", "containerPort": 8000}],
        "resources": {"limits": {"nvidia.com/gpu": 1}},
        "readinessProbe": {"httpGet": {"path": "/health", "port": "http"}, "periodSeconds": 5},
        "volumeMounts": [mount, {"name": "shm", "mountPath": "/dev/shm"}],
    }
    def resource(api, kind, name, **fields):
        return {"apiVersion": api, "kind": kind, "metadata": {"name": name, "namespace": ns}, **fields}

    cluster = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}},
        resource("v1", "PersistentVolumeClaim", "model-cache", spec={
            "accessModes": ["ReadWriteMany"], "storageClassName": c["storage_class"],
            "resources": {"requests": {"storage": "50Gi"}}}),
        resource("apps/v1", "Deployment", "vllm", spec={
            "replicas": 1, "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {"app": "vllm"}},
            "template": {"metadata": {"labels": {"app": "vllm"}}, "spec": {
                "nodeSelector": c["node_selector"],
                "containers": [container], "volumes": [volume, {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "8Gi"}}]}}}),
        resource("v1", "Service", "vllm", spec={"selector": {"app": "vllm"},
            "ports": [{"name": "http", "port": 8000, "targetPort": "http"}]}),
    ]
    cluster[-1]["metadata"]["labels"] = {"app": "vllm"}
    download = resource("batch/v1", "Job", "download-model", spec={"backoffLimit": 2, "template": {
        "spec": {"restartPolicy": "Never", "containers": [{
            "name": "download", "image": image, "command": ["python3", "-c",
                "from huggingface_hub import snapshot_download; import os; "
                "snapshot_download(os.environ['MODEL'], revision=os.environ['REVISION'])"],
            "env": [{"name": "HF_HOME", "value": "/models"}, {"name": "MODEL", "value": model},
                    {"name": "REVISION", "value": c["model_revision"]}],
            "volumeMounts": [mount]}], "volumes": [volume]}}})
    monitor = resource("monitoring.coreos.com/v1", "ServiceMonitor", "vllm", spec={
        "selector": {"matchLabels": {"app": "vllm"}}, "namespaceSelector": {"matchNames": [ns]},
        "endpoints": [{"port": "http", "path": "/metrics", "interval": "5s"}]})
    out = {"cluster.yaml": cluster, "download.yaml": [download], "monitor.yaml": [monitor]}
    # Separate bootstrap files avoid starting offline serving before the cache exists.
    out["storage.yaml"] = cluster[:2]
    out["serving.yaml"] = cluster[2:]
    proxy_config = """events {}
http {
  server {
    listen 8000;
    client_max_body_size 8m;
    location / {
      proxy_pass http://vllm:8000;
      proxy_http_version 1.1;
      proxy_set_header Connection close;
      proxy_buffering off;
      proxy_read_timeout 3600s;
    }
  }
}
"""
    out["proxy.yaml"] = [
        resource("v1", "ConfigMap", "vllm-proxy", data={"nginx.conf": proxy_config}),
        resource("apps/v1", "Deployment", "vllm-proxy", spec={
            "replicas": 1, "selector": {"matchLabels": {"app": "vllm-proxy"}},
            "template": {"metadata": {"labels": {"app": "vllm-proxy"}}, "spec": {
                "nodeSelector": c.get("control_node_selector", {}),
                "containers": [{"name": "proxy", "image": "nginx:1.28-alpine",
                    "ports": [{"containerPort": 8000}],
                    "resources": {"requests": {"cpu": "100m", "memory": "64Mi"},
                                  "limits": {"cpu": "1", "memory": "256Mi"}},
                    "readinessProbe": {"tcpSocket": {"port": 8000}},
                    "volumeMounts": [{"name": "config", "mountPath": "/etc/nginx/nginx.conf", "subPath": "nginx.conf"}]}],
                "volumes": [{"name": "config", "configMap": {"name": "vllm-proxy"}}]}}}),
        resource("v1", "Service", "vllm-proxy", spec={"selector": {"app": "vllm-proxy"},
            "ports": [{"name": "http", "port": 8000, "targetPort": 8000}]}),
    ]
    for mode, metric, threshold in (("B", "qps", c["qps_threshold"]), ("C", "queue", c["queue_threshold"])):
        out[f"{mode}.yaml"] = [resource("keda.sh/v1alpha1", "ScaledObject", "vllm-autoscaler", spec={
            "scaleTargetRef": {"name": "vllm"}, "minReplicaCount": 1, "maxReplicaCount": 3,
            "pollingInterval": 15, "advanced": {"horizontalPodAutoscalerConfig": {"behavior": {
                "scaleUp": {"stabilizationWindowSeconds": 0}, "scaleDown": {"stabilizationWindowSeconds": 120}}}},
            "triggers": [{"type": "prometheus", "metricType": "AverageValue", "metadata": {
                "serverAddress": c["prometheus_internal"], "query": queries(c, mode)[metric],
                "threshold": str(threshold), "ignoreNullValues": "false"}}]})]
    sky = {
        "resources": {"infra": f'k8s/{c["kube_context"]}' if c.get("kube_context") else "k8s",
                      "accelerators": f'{c["gpu"]}:1', "ports": 8000},
        "envs": {"MODEL_NAME": model, "MODEL_REVISION": c["model_revision"], "VLLM_VERSION": version},
        "setup": 'pip install "vllm==$VLLM_VERSION"\n'
                 'hf download "$MODEL_NAME" --revision "$MODEL_REVISION"\n',
        "run": 'HF_HUB_OFFLINE=1 vllm serve "$MODEL_NAME" --revision "$MODEL_REVISION" '
               '--port 8000 --max-model-len 8192 --no-enable-prefix-caching\n',
    }
    out["sky-hello.yaml"] = [{"resources": sky["resources"], "run": "nvidia-smi"}]
    out["sky-single.yaml"] = [deepcopy(sky)]
    sky["service"] = {"readiness_probe": {"path": "/v1/models", "initial_delay_seconds": 600}, "replicas": 2}
    out["sky-fixed-two.yaml"] = [deepcopy(sky)]
    sky["service"]["replicas"] = 1
    out["sky-fixed-one.yaml"] = [deepcopy(sky)]
    del sky["service"]["replicas"]
    sky["service"]["replica_policy"] = {"min_replicas": 1, "max_replicas": 3,
        "target_qps_per_replica": c["qps_threshold"], "upscale_delay_seconds": 0, "downscale_delay_seconds": 120}
    out["sky-autoscale.yaml"] = [sky]
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--output", default="generated")
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    for name, docs in manifests(load(args.config)).items():
        (output / name).write_text(yaml.safe_dump_all(docs, sort_keys=False))
    qs = queries(load(args.config), "B")
    dashboard = {
        "title": "CENTRA10 — autoscaling LLM", "uid": "centra10-llm", "schemaVersion": 39,
        "refresh": "5s", "time": {"from": "now-30m", "to": "now"},
        "panels": [{"id": 1, "type": "timeseries", "title": "Ready replicas, waiting/running requests, completed QPS",
            "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 14},
            "targets": [{"refId": key, "expr": qs[key], "legendFormat": key} for key in ("ready", "queue", "running", "qps")],
            "fieldConfig": {"defaults": {"min": 0}, "overrides": [{
                "matcher": {"id": "byName", "options": "qps"},
                "properties": [{"id": "unit", "value": "reqps"}, {"id": "custom.axisPlacement", "value": "right"}]}]}}],
        "__inputs": [{"name": "DS_PROMETHEUS", "label": "Prometheus", "type": "datasource", "pluginId": "prometheus", "pluginName": "Prometheus"}],
    }
    dashboard["panels"].append({"id": 2, "type": "timeseries", "title": "Maximum replica KV-cache usage (if available)",
        "description": "No data means unavailable, not zero usage. Verify the metric name on the deployed vLLM version.",
        "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
        "gridPos": {"x": 0, "y": 14, "w": 24, "h": 8},
        "targets": [{"refId": "kv_cache", "expr": qs["kv_cache"], "legendFormat": "max KV-cache usage"}],
        "fieldConfig": {"defaults": {"unit": "percentunit", "min": 0, "max": 1}, "overrides": []}})
    (output / "dashboard.json").write_text(json.dumps(dashboard, indent=2) + "\n")
    print(f"Wrote manifests to {output}")


if __name__ == "__main__":
    main()
