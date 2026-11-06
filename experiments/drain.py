"""Observe one streamed request while scaling a single serving replica to zero."""
import argparse
import json
import subprocess
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

import yaml

from experiments.config import load
from experiments.render import manifests
from experiments.run import kubectl, save


def prepare(c, seconds, output):
    if c["namespace"] != "centra10":
        raise ValueError("Drain Job template supports namespace centra10 only")
    c = {**c, "fixed_replicas": 1, "shutdown_timeout_seconds": seconds,
         "kube_context": "", "k8s_url": "http://vllm-proxy:8000"}
    name = f"drain-control-{seconds}-1"
    docs = list(yaml.safe_load_all(Path("aws/runner.example.yaml").read_text()))
    role = next(d for d in docs if d["kind"] == "Role")
    job = next(d for d in docs if d["kind"] == "Job")
    job["metadata"]["name"] = f"centra10-drain-{seconds}"
    job["spec"]["activeDeadlineSeconds"] = 2400
    spec = job["spec"]["template"]["spec"]
    spec["containers"][0]["args"] = [
        "set -euo pipefail\nexec python3 -u -m experiments.drain "
        f"--config /settings/config.json --output /models/experiment-results/{name}\n"]
    code_name, settings_name = f"drain-code-{seconds}", f"drain-settings-{seconds}"
    for volume in spec["volumes"]:
        if volume["name"] == "code":
            volume["configMap"]["name"] = code_name
        elif volume["name"] == "settings":
            volume["configMap"]["name"] = settings_name
    code = {p.name: p.read_text() for p in Path(__file__).parent.glob("*.py")}
    maps = [{"apiVersion": "v1", "kind": "ConfigMap",
             "metadata": {"name": n, "namespace": c["namespace"]}, "data": data}
            for n, data in [(code_name, code), (settings_name, {"config.json": json.dumps(c)})]]
    output.mkdir(parents=True, exist_ok=False)
    # Operator applies this only after confirming no other experiment or autoscaler is active.
    (output / "bundle.yaml").write_text(yaml.safe_dump_all(
        [role, *maps, *manifests(c)["serving.yaml"], job], sort_keys=False))
    print(f"Prepared {output / 'bundle.yaml'}; no cluster changes made")


def consume(response, first_token):
    result = {"done": False, "finish_reason": None, "completion_tokens": None}
    for line in response:
        line = line.decode().strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            result["done"] = True
            break
        event = json.loads(payload)
        for choice in event.get("choices", []):
            if choice.get("text"):
                first_token.set()
            if choice.get("finish_reason"):
                result["finish_reason"] = choice["finish_reason"]
        if event.get("usage"):
            result["completion_tokens"] = event["usage"]["completion_tokens"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--shutdown-timeout", type=int, choices=[0, 120], default=0)
    args = parser.parse_args()
    c = load(args.config)
    if args.prepare:
        prepare(c, args.shutdown_timeout, args.output)
        return
    args.output.mkdir(parents=True, exist_ok=False)
    result = {"status": "preparing", "requested_tokens": 512}
    first = threading.Event()
    streamed = {}
    thread = None
    log_process = None
    log_file = None
    try:
        controllers = json.loads(kubectl(c, "get", "hpa,scaledobject", "-o", "json", capture_output=True).stdout)
        if controllers["items"]:
            raise RuntimeError("Remove autoscalers before a controlled drain test")
        deployment = json.loads(kubectl(c, "get", "deployment/vllm", "-o", "json", capture_output=True).stdout)
        save(args.output / "deployment.json", deployment)
        if deployment["spec"].get("replicas") != 1:
            raise RuntimeError("Drain probe requires exactly one desired replica")
        deadline = time.monotonic() + c["ready_timeout_seconds"]
        while time.monotonic() < deadline:
            pods = json.loads(kubectl(c, "get", "pods", "-l", "app=vllm", "-o", "json", capture_output=True).stdout)
            if len(pods["items"]) == 1:
                pod = pods["items"][0]
                if not pod["metadata"].get("deletionTimestamp") and any(
                    condition["type"] == "Ready" and condition["status"] == "True"
                    for condition in pod["status"].get("conditions", [])
                ):
                    save(args.output / "pod-before.json", pod)
                    break
            time.sleep(5)
        else:
            raise TimeoutError("Single serving pod did not become Ready")

        context = ["--context", c["kube_context"]] if c.get("kube_context") else []
        log_file = (args.output / "serving.log").open("w")
        log_process = subprocess.Popen(["kubectl", *context, "-n", c["namespace"], "logs",
            "-f", pod["metadata"]["name"], "--timestamps=true"], stdout=log_file, stderr=subprocess.STDOUT)

        def request():
            try:
                payload = json.dumps({"model": c["model"], "prompt": "Explain how computers work.",
                    "max_tokens": 512, "ignore_eos": True, "temperature": 0,
                    "stream": True, "stream_options": {"include_usage": True}}).encode()
                req = Request(c["k8s_url"].rstrip("/") + "/v1/completions", data=payload,
                              headers={"Content-Type": "application/json"})
                with urlopen(req, timeout=180) as response:
                    streamed.update(consume(response, first))
            except Exception as exc:
                streamed["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                streamed["ended_at"] = time.time()

        thread = threading.Thread(target=request, daemon=True)
        result["request_started_at"] = time.time()
        thread.start()
        if not first.wait(timeout=120):
            raise TimeoutError(f"No first content token: {streamed}")
        result["scale_requested_at"] = time.time()
        kubectl(c, "scale", "deployment/vllm", "--replicas=0")
        thread.join(timeout=200)
        if thread.is_alive():
            raise TimeoutError("Stream did not end within the probe budget")
        result.update(status="complete", stream=streamed)
        result["stream_complete"] = (streamed.get("done") is True
            and streamed.get("finish_reason") == "length"
            and streamed.get("completion_tokens") == 512)
        print(json.dumps(result, indent=2), flush=True)
    except BaseException as exc:
        result.update(status="failed", error=str(exc), stream=streamed)
        raise
    finally:
        if log_process is not None:
            if log_process.poll() is None:
                log_process.terminate()
            try:
                log_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log_process.kill()
                log_process.wait()
        if log_file is not None:
            log_file.close()
        result["ended_at"] = time.time()
        save(args.output / "probe.json", result)


if __name__ == "__main__":
    main()
