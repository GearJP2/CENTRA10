"""Plan or execute the 4 x 4 x 3 experiment; no cluster changes without --execute."""
import argparse
import json
import math
import subprocess
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError

import yaml

from experiments.config import load, queries
from experiments.render import manifests


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def http_json(url, data=None):
    request = Request(url, data=None if data is None else json.dumps(data).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def metric(c, query, optional=False):
    if query is None and optional:
        return None
    result = http_json(c["prometheus_url"].rstrip("/") + "/api/v1/query?" + urlencode({"query": query}))
    if optional and result.get("status") == "success" and result["data"]["result"] == []:
        return None
    if result.get("status") != "success" or len(result["data"]["result"]) != 1:
        raise ValueError(f"Expected exactly one Prometheus series: {query}")
    stamp, value = result["data"]["result"][0]["value"]
    value = float(value)
    if not math.isfinite(value) or value < 0 or time.time() - float(stamp) > 60:
        raise ValueError(f"Invalid or stale metric: {query}")
    return value


def phases(c, workload):
    rate, duration = c["request_rate"], c["duration_seconds"]
    if workload == "bursty":
        # shortcut: phases drain between benchmark commands; use a timed trace for exact wall-clock bursts.
        return [[("short", rate * factor, c["burst_phase_seconds"])] for factor in (1, 4, 1)]
    if workload == "mixed":
        return [[("short", rate * .8, duration), ("long", rate * .2, duration)]]
    return [[(workload, rate, duration)]]


def bench(c, url, kind, rate, duration, seed, folder, filename):
    count = round(rate * duration)
    if count < 1:
        raise ValueError("Each load stream must contain at least one request")
    return ["vllm", "bench", "serve", "--backend", "openai-chat", "--endpoint", "/v1/chat/completions",
            "--base-url", url, "--model", c["model"], "--tokenizer", c.get("tokenizer_path", c["model"]),
            "--dataset-name", "random", "--random-input-len", "6000" if kind == "long" else "100",
            "--random-output-len", "200" if kind == "long" else "100", "--random-range-ratio", "1",
            "--request-rate", str(rate), "--num-prompts", str(count), "--seed", str(seed),
            "--ignore-eos", "--save-result", "--save-detailed", "--result-dir", str(folder),
            "--result-filename", filename, "--percentile-metrics", "ttft", "--metric-percentiles", "50,99"]


def command(argv, **kwargs):
    return subprocess.run(argv, check=True, text=True, timeout=1800, **kwargs)


def kubectl(c, *args, namespace=None, **kwargs):
    context = ["--context", c["kube_context"]] if c.get("kube_context") else []
    return command(["kubectl", *context, "-n", namespace or c["namespace"], *args], **kwargs)


def remove_scaler(c):
    kubectl(c, "delete", "scaledobject", "vllm-autoscaler", "--ignore-not-found=true", "--wait=true")
    # Wait for the owned HPA to disappear before manually setting replicas.
    deadline = time.monotonic() + c["ready_timeout_seconds"]
    while True:
        hpas = json.loads(kubectl(c, "get", "hpa", "-o", "json", capture_output=True).stdout)["items"]
        targeting = [h for h in hpas if h["spec"]["scaleTargetRef"]["name"] == "vllm"]
        if not targeting:
            return
        if time.monotonic() > deadline:
            raise TimeoutError("An HPA still targets vllm; remove the competing controller")
        time.sleep(2)


def release_k8s(c):
    remove_scaler(c)
    kubectl(c, "scale", "deployment/vllm", "--replicas=0")
    kubectl(c, "wait", "--for=delete", "pod", "-l", "app=vllm", "--timeout=300s")


def reset(c, mode, folder):
    if mode == "D":
        command(["sky", "serve", "up", "-y", "-n", c["sky_service"], str(folder / "sky-fixed-one.yaml")])
    else:
        remove_scaler(c)
        kubectl(c, "scale", "deployment/vllm", "--replicas=1")


def activate(c, mode, folder):
    if mode in ("B", "C"):
        kubectl(c, "apply", "-f", str(folder / f"{mode}.yaml"))
    elif mode == "D":
        command(["sky", "serve", "update", c["sky_service"], str(folder / "sky-autoscale.yaml")])


def warm(c, mode, url):
    qs = queries(c, mode)
    deadline = time.monotonic() + c["ready_timeout_seconds"]
    stable_since = None
    while time.monotonic() < deadline:
        try:
            ready = metric(c, qs["ready"])
            allocated = metric(c, qs["replicas"])
            queue = metric(c, qs["queue"])
        except (ValueError, URLError, TimeoutError):
            stable_since = None
            time.sleep(c["sample_seconds"])
            continue
        if ready == allocated == 1 and queue == 0:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= 65:
                result = http_json(url.rstrip("/") + "/v1/chat/completions", {
                    "model": c["model"], "messages": [{"role": "user", "content": "Say hello."}],
                    "max_tokens": 16, "temperature": 0})
                if not result.get("choices"):
                    raise ValueError("Warm-up returned no completion")
                return
        else:
            stable_since = None
        time.sleep(c["sample_seconds"])
    raise TimeoutError("Did not reach one warm replica with an empty queue")


def sample(c, mode):
    values = {key: metric(c, query, optional=key == "kv_cache") for key, query in queries(c, mode).items()}
    values["timestamp"] = time.time()
    return values


def pod_snapshot(c, mode):
    ns = c["sky_namespace"] if mode == "D" else c["namespace"]
    selector = c["sky_pod_selector"] if mode == "D" else "app=vllm"
    return json.loads(kubectl(c, "get", "pods", "-l", selector,
                             "-o", "json", namespace=ns, capture_output=True).stdout)


def scaling_snapshot(c, mode, pods):
    ns = c["sky_namespace"] if mode == "D" else c["namespace"]
    events = json.loads(kubectl(c, "get", "events", "-o", "json", namespace=ns,
                               capture_output=True).stdout)
    uids = {p["metadata"]["uid"] for p in pods["items"]}
    events["items"] = [e for e in events["items"] if
        e.get("involvedObject", {}).get("uid") in uids or
        (mode != "D" and e.get("involvedObject", {}).get("kind") in
         ("HorizontalPodAutoscaler", "ScaledObject", "Deployment", "ReplicaSet"))]
    state = {"timestamp": time.time(), "events": events}
    if mode == "D":
        state["sky_status"] = command(["sky", "serve", "status", c["sky_service"]], capture_output=True).stdout
    else:
        state["controllers"] = json.loads(kubectl(c, "get", "hpa,scaledobject", "-o", "json",
                                                  capture_output=True).stdout)
    return state


def run_one(c, mode, workload, repeat, root):
    folder = root / f"{workload}_{mode}_run{repeat}"
    folder.mkdir(parents=True, exist_ok=False)
    manifest_dir = folder / "manifests"
    manifest_dir.mkdir()
    for name, docs in manifests(c).items():
        (manifest_dir / name).write_text(yaml.safe_dump_all(docs, sort_keys=False))
    save(folder / "config.json", c)
    meta = {"mode": mode, "workload": workload, "repeat": repeat, "status": "preparing", "phases": [],
            "measurement_schema": 2}
    save(folder / "run.json", meta)
    url = c["k8s_url"]
    samples, pods, scaling, processes = [], [], [], []
    sky_created = False
    try:
        if mode == "D":
            release_k8s(c)
        reset(c, mode, manifest_dir)
        if mode == "D":
            sky_created = True
            url = command(["sky", "serve", "status", c["sky_service"], "--endpoint"], capture_output=True).stdout.strip()
            if not url.startswith(("http://", "https://")):
                url = "http://" + url
        meta["endpoint"] = url
        warm(c, mode, url)
        activate(c, mode, manifest_dir)
        # SkyServe updates asynchronously; require the same warm baseline again.
        if mode == "D":
            warm(c, mode, url)
        # vLLM 0.32 detailed start_times use perf_counter, shared by local processes.
        meta.update(status="running", start=time.time(), clock_offset=time.time() - time.perf_counter())
        save(folder / "queries.json", queries(c, mode))
        save(folder / "run.json", meta)
        deadline = time.monotonic() + c["run_timeout_seconds"]
        for index, streams in enumerate(phases(c, workload)):
            phase = {"start": time.time(), "streams": []}
            meta["phases"].append(phase)
            processes = []
            for stream_index, (kind, rate, duration) in enumerate(streams):
                filename = f"bench_{index}_{kind}.json"
                argv = bench(c, url, kind, rate, duration, repeat * 100 + index * 10 + stream_index, folder, filename)
                phase["streams"].append({"file": filename, "requests": round(rate * duration),
                                         "target_qps": rate, "target_duration_seconds": duration, "command": argv})
                with (folder / f"bench_{index}_{kind}.log").open("w") as log:
                    processes.append(subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT))
            save(folder / "run.json", meta)
            while True:
                samples.append(sample(c, mode))
                save(folder / "metrics.json", samples)
                pods.append({"timestamp": time.time(), "pods": pod_snapshot(c, mode)})
                save(folder / "pods.json", pods)
                scaling.append(scaling_snapshot(c, mode, pods[-1]["pods"]))
                save(folder / "scaling.json", scaling)
                codes = [p.poll() for p in processes]
                if any(code not in (None, 0) for code in codes):
                    raise RuntimeError("Benchmark failed; see bench logs")
                if all(code is not None for code in codes):
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("Run exceeded run_timeout_seconds")
                time.sleep(c["sample_seconds"])
            phase["end"] = time.time()
        samples.append(sample(c, mode))
        save(folder / "metrics.json", samples)
        from experiments.analyze import timings, request_arrivals, traffic_samples
        arrivals = []
        for phase in meta["phases"]:
            for stream in phase["streams"]:
                result = json.loads((folder / stream["file"]).read_text())
                timings(result, stream["requests"])
                starts = request_arrivals(result, stream["requests"], meta["clock_offset"], phase["start"], phase["end"])
                stream["first_request_at"], stream["last_request_at"] = min(starts), max(starts)
                arrivals.extend({"timestamp": stamp, "file": stream["file"]} for stamp in starts)
            phase["first_request_at"] = min(s["first_request_at"] for s in phase["streams"])
        save(folder / "arrivals.json", sorted(arrivals, key=lambda a: a["timestamp"]))
        save(folder / "traffic.json", traffic_samples(arrivals, samples, meta["start"]))
        meta.update(status="complete", end=time.time())
        if mode == "D":
            with (folder / "sky-controller.log").open("w") as log:
                command(["sky", "serve", "logs", c["sky_service"], "--controller", "--no-follow"],
                        stdout=log, stderr=subprocess.STDOUT)
    except BaseException as exc:
        meta.update(status="failed", error=str(exc), end=time.time())
        raise
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        save(folder / "run.json", meta)
        # Retain the model cache; release only this experiment's serving GPUs.
        try:
            if mode == "D":
                if sky_created:
                    command(["sky", "serve", "down", "-y", c["sky_service"]])
                    kubectl(c, "wait", "--for=delete", "pod", "-l", c["sky_pod_selector"],
                            "--timeout=300s", namespace=c["sky_namespace"])
                else:
                    print("SkyServe launch did not finish; inspect sky serve status for partial provisioning.")
            else:
                release_k8s(c)
        except Exception as exc:
            meta.update(status="failed", cleanup_error=str(exc))
            save(folder / "run.json", meta)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--output", default="results")
    parser.add_argument("--modes", nargs="+", choices=list("ABCD"), default=list("ABCD"))
    parser.add_argument("--workloads", nargs="+", choices=["short", "long", "mixed", "bursty"],
                        default=["short", "long", "mixed", "bursty"])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--smoke", action="store_true", help="One short A run at 0.2 req/s for 30 seconds; no research conclusions")
    parser.add_argument("--resume", action="store_true", help="Skip completed runs with identical configuration")
    args = parser.parse_args()
    c = load(args.config)
    if args.smoke:
        args.modes, args.workloads, args.repeats = ["A"], ["short"], 1
        c.update(request_rate=.2, duration_seconds=30)
        if args.output == "results":
            args.output = "results/smoke"
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    # Rotate treatment order across repeats to reduce systematic time-of-day bias.
    plan = [(mode, workload, repeat) for repeat in range(1, args.repeats + 1)
            for workload in args.workloads
            for mode in args.modes[(repeat - 1) % len(args.modes):] + args.modes[:(repeat - 1) % len(args.modes)]]
    if not args.execute:
        print(json.dumps([{"mode": m, "workload": w, "repeat": r, "phases": phases(c, w)} for m, w, r in plan], indent=2))
        return
    needed = [c[k] for k in ("model_revision", "vllm_version", "storage_class")]
    needed += [c.get("kube_context", "")]
    if "D" in args.modes:
        queries(c, "D")
        needed += [c["sky_pod_selector"], *c["sky_queries"].values()]
    if any("SET_" in str(value) for value in needed):
        parser.error("Replace configuration placeholders before executing")
    version = command(["vllm", "--version"], capture_output=True).stdout.strip()
    if c["vllm_version"] not in version.split():
        parser.error(f"Benchmark vLLM version differs from server configuration: {version}")
    from huggingface_hub import snapshot_download
    c["tokenizer_path"] = snapshot_download(c["model"], revision=c["model_revision"],
        allow_patterns=["*.json", "*.txt", "*.model", "*.jinja"])
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    versions = {"vllm": version}
    versions["kubernetes"] = kubectl(c, "version", "-o", "json", capture_output=True).stdout
    versions["keda"] = kubectl(c, "get", "deployments", "-A", "-l", "app=keda-operator", "-o", "json", capture_output=True).stdout
    if "D" in args.modes:
        versions["skypilot"] = command(["sky", "--version"], capture_output=True).stdout
    save(root / "versions.json", versions)
    for mode, workload, repeat in plan:
        folder = root / f"{workload}_{mode}_run{repeat}"
        if args.resume and (folder / "run.json").exists():
            old = json.loads((folder / "run.json").read_text())
            if (old["status"] == "complete" and old.get("measurement_schema") == 2
                    and json.loads((folder / "config.json").read_text()) == c):
                print(f"Skipping completed {folder}")
                continue
        print(f"Running {workload} / {mode} / {repeat}", flush=True)
        run_one(c, mode, workload, repeat, root)


if __name__ == "__main__":
    main()
