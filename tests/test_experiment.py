import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from experiments import analyze, run
from experiments.config import load, queries
from experiments.render import manifests


CONFIG = load("config.example.json")


class ExperimentTests(unittest.TestCase):
    def test_workloads(self):
        mixed = run.phases(CONFIG, "mixed")[0]
        self.assertEqual(sum(rate for _, rate, _ in mixed), 4)
        self.assertEqual([round(rate * duration) for _, rate, duration in mixed], [1920, 480])
        self.assertEqual([phase[0][1] for phase in run.phases(CONFIG, "bursty")], [4, 16, 4])
        self.assertEqual(sum(p[0][2] for p in run.phases(CONFIG, "bursty")), 540)

    def test_manifest_scalers_differ_only_in_metric(self):
        files = manifests(CONFIG)
        b, c = deepcopy(files["B.yaml"]), deepcopy(files["C.yaml"])
        self.assertEqual(b[0]["metadata"]["name"], c[0]["metadata"]["name"])
        b[0]["spec"]["triggers"][0]["metadata"]["query"] = c[0]["spec"]["triggers"][0]["metadata"]["query"]
        self.assertEqual(b, c)
        for docs in files.values():
            self.assertEqual(list(yaml.safe_load_all(yaml.safe_dump_all(docs))), docs)
        self.assertIn('namespace="centra10",service="vllm"', queries(CONFIG, "B")["qps"])

    def test_shared_model_and_version(self):
        files = manifests(CONFIG)
        deployment = files["cluster.yaml"][2]["spec"]["template"]["spec"]
        self.assertEqual(deployment["nodeSelector"], CONFIG["node_selector"])
        container = deployment["containers"][0]
        self.assertIn(CONFIG["model_revision"], container["args"])
        self.assertEqual(files["sky-autoscale.yaml"][0]["envs"]["VLLM_VERSION"], CONFIG["vllm_version"])

    def test_bench_command_records_details_and_fixed_length(self):
        cmd = run.bench(CONFIG, "http://example", "long", 1, 10, 42, Path("results"), "raw.json")
        self.assertIn("--save-detailed", cmd)
        self.assertIn("--ignore-eos", cmd)
        self.assertEqual(cmd[cmd.index("--random-input-len") + 1], "6000")
        with self.assertRaises(ValueError):
            run.bench(CONFIG, "http://example", "short", .001, 1, 1, Path("."), "x.json")

    def test_failed_requests_never_pass_slo(self):
        result = {"ttfts": [1, 0, 3, 2], "errors": ["", "timeout", "", ""], "completed": 3}
        times = analyze.timings(result, 4)
        self.assertEqual(sum(t <= 2 for t in times) / 4, .5)
        self.assertEqual(analyze.percentile(times, 50), 2)
        self.assertAlmostEqual(analyze.percentile(times, 99), 2.98)

    def test_reject_corrupt_timings(self):
        for result in [
            {"ttfts": [0], "errors": [""], "completed": 1},
            {"ttfts": [float("nan")], "errors": [""], "completed": 1},
            {"ttfts": [1], "errors": [], "completed": 1},
            {"ttfts": [1], "errors": [""], "completed": 0},
        ]:
            with self.assertRaises(ValueError):
                analyze.timings(result, 1)

    def test_gpu_hours_step_integral(self):
        samples = [{"timestamp": 0, "replicas": 1}, {"timestamp": 10, "replicas": 3},
                   {"timestamp": 20, "replicas": 1}]
        self.assertAlmostEqual(analyze.gpu_hours(samples, 0, 20, 5), 40 / 3600)
        with self.assertRaises(ValueError):
            analyze.gpu_hours([samples[0], {"timestamp": 60, "replicas": 1}], 0, 60, 5)

    def test_metric_rejects_missing_and_nan(self):
        for results in ([], [{"value": [0, "NaN"]}], [{"value": [0, "1"]}]):
            with patch.object(run, "http_json", return_value={"status": "success", "data": {"result": results}}):
                with self.assertRaises(ValueError):
                    run.metric(CONFIG, "test_metric")

    def test_cold_start_excludes_warm_pods_and_deduplicates(self):
        pod = {"metadata": {"name": "p", "uid": "uid"}, "status": {
            "startTime": "2026-01-01T00:00:00Z", "conditions": [{"type": "Ready", "status": "True",
            "lastTransitionTime": "2026-01-01T00:00:45Z"}]}}
        snapshots = [{"pods": {"items": [pod]}}, {"pods": {"items": [pod]}}]
        self.assertEqual(analyze.cold_starts(snapshots, 0)[0]["cold_start_seconds"], 45)
        self.assertEqual(len(analyze.cold_starts(snapshots, 0)), 1)
        self.assertEqual(analyze.cold_starts(snapshots, 9999999999), [])

    def test_run_failure_stops_load_and_releases_gpus(self):
        process = Mock()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(run, "reset"), patch.object(run, "warm"), patch.object(run, "activate"), \
                 patch.object(run, "sample", side_effect=ValueError("missing telemetry")), \
                 patch.object(run.subprocess, "Popen", return_value=process), patch.object(run, "remove_scaler"), \
                 patch.object(run, "kubectl") as kube:
                with self.assertRaisesRegex(ValueError, "missing telemetry"):
                    run.run_one(CONFIG, "B", "short", 1, Path(directory))
                process.terminate.assert_called_once()
                self.assertTrue(any("--replicas=0" in call.args for call in kube.call_args_list))
                meta = json.loads((Path(directory) / "short_B_run1/run.json").read_text())
                self.assertEqual(meta["status"], "failed")

    def test_existing_results_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "short_A_run1").mkdir()
            with self.assertRaises(FileExistsError):
                run.run_one(CONFIG, "A", "short", 1, Path(directory))

    def test_scaler_is_gone_before_manual_scale(self):
        hpa = {"spec": {"scaleTargetRef": {"name": "vllm"}}}
        responses = [Mock(), Mock(stdout=json.dumps({"items": [hpa]})), Mock(stdout='{"items": []}'), Mock()]
        with patch.object(run, "kubectl", side_effect=responses) as kube, patch.object(run.time, "sleep"):
            run.reset(CONFIG, "B", Path("unused"))
            self.assertEqual(kube.call_args_list[-1].args[1:], ("scale", "deployment/vllm", "--replicas=1"))

    def test_plan_has_48_unique_conditions_without_cluster_access(self):
        result = subprocess.run(["python3", "-m", "experiments.run", "--config", "config.example.json"], capture_output=True, text=True, check=True)
        plan = json.loads(result.stdout)
        self.assertEqual(len({(p["mode"], p["workload"], p["repeat"]) for p in plan}), 48)

    def test_aws_context_applies_to_serving_and_cleanup_commands(self):
        config = load("aws/config.example.json")
        config["kube_context"] = "eks-test-context"
        with patch.object(run, "command") as command:
            run.kubectl(config, "scale", "deployment/vllm", "--replicas=0")
            self.assertEqual(command.call_args.args[0][:5],
                             ["kubectl", "--context", "eks-test-context", "-n", "centra10"])
            run.kubectl(config, "get", "pods", namespace="sky")
            self.assertEqual(command.call_args.args[0][4], "sky")
        rendered = manifests(config)
        for name in ("sky-single.yaml", "sky-autoscale.yaml", "sky-fixed-one.yaml"):
            self.assertEqual(rendered[name][0]["resources"]["infra"], "k8s/eks-test-context")

    def test_aws_bootstrap_storage_and_proxy(self):
        rendered = manifests(load("aws/config.example.json"))
        self.assertEqual([d["kind"] for d in rendered["storage.yaml"]], ["Namespace", "PersistentVolumeClaim"])
        self.assertEqual(rendered["storage.yaml"][1]["spec"]["storageClassName"], "centra10-efs")
        self.assertEqual(rendered["cluster.yaml"], rendered["storage.yaml"] + rendered["serving.yaml"])
        proxy = rendered["proxy.yaml"]
        self.assertIn("proxy_buffering off;", proxy[0]["data"]["nginx.conf"])
        self.assertIn("proxy_set_header Connection close;", proxy[0]["data"]["nginx.conf"])
        self.assertEqual(proxy[1]["spec"]["template"]["spec"]["nodeSelector"], {"centra10-role": "system"})
        self.assertNotEqual(proxy[2]["spec"]["selector"], rendered["serving.yaml"][1]["spec"]["selector"])

    def test_aws_smoke_plan_and_placeholder_guard(self):
        argv = ["python3", "-m", "experiments.run", "--config", "aws/config.example.json", "--smoke"]
        plan = json.loads(subprocess.run(argv, capture_output=True, text=True, check=True).stdout)
        self.assertEqual(len(plan), 1)
        self.assertEqual((plan[0]["mode"], plan[0]["workload"], plan[0]["repeat"]), ("A", "short", 1))
        self.assertEqual(plan[0]["phases"], [[["short", .2, 30]]])
        result = subprocess.run([*argv, "--execute"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Replace configuration placeholders", result.stderr)

    def test_aws_infrastructure_templates(self):
        cluster = yaml.safe_load(Path("aws/eks.example.yaml").read_text())
        gpu = next(n for n in cluster["managedNodeGroups"] if n["name"] == "gpu")
        self.assertEqual(gpu["desiredCapacity"], 3)
        self.assertEqual(gpu["maxSize"], 3)
        self.assertEqual(gpu["minSize"], 0)
        self.assertEqual(gpu["instanceType"], "g6.2xlarge")
        self.assertTrue(gpu["privateNetworking"])
        storage = yaml.safe_load(Path("aws/efs-storageclass.example.yaml").read_text())
        self.assertEqual(storage["provisioner"], "efs.csi.aws.com")
        self.assertEqual(storage["reclaimPolicy"], "Retain")
        self.assertIn("tls", storage["mountOptions"])
        for path in Path("aws").glob("*.yaml"):
            self.assertIsInstance(yaml.safe_load(path.read_text()), dict)

    def test_analysis_cli_on_synthetic_48_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mode in "ABCD":
                for workload in ("short", "long", "mixed", "bursty"):
                    for repeat in (1, 2, 3):
                        folder = root / f"{workload}_{mode}_run{repeat}"
                        folder.mkdir()
                        phase = {"start": 0, "streams": [{"file": "bench.json", "requests": 2}]}
                        run.save(folder / "run.json", {"status": "complete", "mode": mode, "workload": workload,
                            "repeat": repeat, "start": 0, "end": 10,
                            "phases": [phase] if workload != "bursty" else [phase, {**phase, "start": 4}, {**phase, "start": 8}]})
                        run.save(folder / "config.json", CONFIG)
                        run.save(folder / "bench.json", {"ttfts": [1, 3], "errors": ["", ""], "completed": 2})
                        run.save(folder / "metrics.json", [{"timestamp": t, "replicas": 1, "ready": 1, "queue": 0, "qps": 1} for t in (0, 5, 10)])
                        run.save(folder / "pods.json", [])
            result = subprocess.run(["python3", "-m", "experiments.analyze", str(root), "--output", str(root / "analysis"), "--require-complete"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(list((root / "analysis").glob("*.png"))), 3)
            self.assertEqual(len((root / "analysis/runs.csv").read_text().splitlines()), 49)


if __name__ == "__main__":
    unittest.main()
