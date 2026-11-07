import base64
import gzip
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from experiments import batch
from experiments.config import load
from experiments.measured import prepare


class BatchTests(unittest.TestCase):
    def trial(self, name, mode="A"):
        return {"plan": {"job": name, "results": name, "mode": mode, "workload": "short", "repeat": 1, "requests": 300},
                "config": {}, "code": {"__init__.py": "", "run.py": "# pinned original source\n"}}

    def payload(self, root, trials):
        path = root / "payload.gz"
        sources = {str(i): trial["code"] for i, trial in enumerate(trials)}
        rows = [{"plan": trial["plan"], "config": trial["config"], "code_id": str(i)} for i, trial in enumerate(trials)]
        path.write_bytes(gzip.compress(json.dumps({"trials": rows, "serving": {}, "sources": sources}).encode()))
        return path

    def completed(self):
        return {"status": {"conditions": [{"type": "Complete", "status": "True"}]}}

    def test_prepared_batch_contains_no_serving_mutations_or_child_jobs(self):
        with tempfile.TemporaryDirectory() as directory, patch("builtins.print"):
            root = Path(directory)
            prepare(load("aws/config.example.json"), root / "matrix")
            batch.prepare(root / "matrix", root / "batch")
            docs = list(yaml.safe_load_all((root / "batch/bundle.yaml").read_text()))
            self.assertEqual(sum(d["kind"] == "Job" for d in docs), 1)
            self.assertFalse(any(d["kind"] in ("Deployment", "Service") for d in docs))
            self.assertEqual(sum(d["kind"] == "PodMonitor" for d in docs), 12)
            settings = next(d for d in docs if "binaryData" in d)
            packed = base64.b64decode(settings["binaryData"]["plan.json.gz"])
            payload = json.loads(gzip.decompress(packed))
            self.assertEqual(len(payload["trials"]), 48)
            self.assertEqual(len(payload["sources"]), 1)
            self.assertLess(len(packed), 900 * 1024)
            observer = next(d for d in docs if d["metadata"]["name"] == "measured-batch-observer" and d["kind"] == "Role")
            self.assertEqual(observer["rules"][0]["verbs"], ["get", "list"])
            self.assertTrue(settings["immutable"])
            job = next(d for d in docs if d["kind"] == "Job")
            self.assertEqual(job["spec"]["backoffLimit"], 0)
            self.assertIn("--execute --payload", job["spec"]["template"]["spec"]["containers"][0]["args"][0])
            with self.assertRaises(FileExistsError):
                batch.prepare(root / "matrix", root / "batch")

    def test_skip_completed_wait_for_active_then_run_next_with_original_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trials = [self.trial(name) for name in ("first", "second", "third")]
            for name in ("first", "second"):
                (root / name).mkdir()
            inventory = {"first": self.completed(), "second": {"status": {"active": 1}}}
            finished = {"first": self.completed(), "second": self.completed()}
            results = [{"mode": "A", "workload": "short", "repeat": i, "failed": 0} for i in (1, 2, 3)]

            def child(argv, **kwargs):
                self.assertEqual((Path(kwargs["cwd"]) / "experiments/run.py").read_text(), "# pinned original source\n")
                self.assertEqual(argv[-1], "--execute")
                return Mock(returncode=0)

            with patch.object(batch, "jobs", side_effect=[inventory, inventory, finished, finished]), \
                    patch.object(batch, "verify", side_effect=results), patch.object(batch, "baseline"), \
                    patch.object(batch.time, "sleep") as sleep, patch.object(batch.subprocess, "run", side_effect=child) as run:
                batch.execute(self.payload(root, trials), root / "progress", "batch-job", root=root)
            self.assertEqual(run.call_count, 1)
            sleep.assert_called_once_with(15)
            state = json.loads((root / "progress/progress.json").read_text())
            self.assertEqual(state["status"], "complete")
            self.assertEqual(len(state["completed"]), 3)
            self.assertTrue((root / "third/runner.log").exists())
            self.assertFalse((root / ".measured-batch.lock").exists())

    def test_failed_job_and_conflicting_job_stop_without_starting_work(self):
        for inventory, message in [
            ({"first": {"status": {"conditions": [{"type": "Failed", "status": "True"}]}}}, "Existing Job failed"),
            ({"unrelated": {"status": {"active": 1}}}, "Conflicting active Jobs")]:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with patch.object(batch, "jobs", return_value=inventory), patch.object(batch.subprocess, "run") as run:
                    with self.assertRaisesRegex(RuntimeError, message):
                        batch.execute(self.payload(root, [self.trial("first")]), root / "progress", "batch-job", root=root)
                run.assert_not_called()
                state = json.loads((root / "progress/progress.json").read_text())
                self.assertEqual(state["status"], "failed")
                self.assertFalse((root / ".measured-batch.lock").exists())

    def test_child_failure_stops_and_preserves_log_without_running_next(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(batch, "jobs", return_value={}), patch.object(batch, "baseline"), \
                    patch.object(batch.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["runner"])) as run:
                with self.assertRaises(subprocess.CalledProcessError):
                    batch.execute(self.payload(root, [self.trial("first"), self.trial("second")]), root / "progress", "batch-job", root=root)
            self.assertEqual(run.call_count, 1)
            self.assertTrue((root / "first/runner.log").exists())
            self.assertFalse((root / "second").exists())

    def test_stale_lock_is_not_removed_by_a_second_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / ".measured-batch.lock"
            lock.write_text("existing owner")
            with self.assertRaises(FileExistsError):
                batch.execute(self.payload(root, []), root / "progress", "batch-job", root=root)
            self.assertEqual(lock.read_text(), "existing owner")

    def test_real_raw_results_are_verified_and_config_changes_rejected(self):
        root = Path("results")
        name = "calibration-r2-D-qps01-long-1-a1"
        c = json.loads((root / name / "long_D_run1/config.json").read_text())
        c.pop("tokenizer_path", None)
        trial = {"plan": {"results": name, "mode": "D", "workload": "long", "repeat": 1, "requests": 300}, "config": c}
        self.assertEqual(batch.verify(trial, root)["failed"], 11)
        trial["config"]["seed_offset"] = 9999
        with self.assertRaisesRegex(ValueError, "Configuration mismatch"):
            batch.verify(trial, root)

    def test_baseline_rejects_unfinished_cleanup_and_live_sky_gpu(self):
        spec = {"nodeSelector": {"centra10-role": "gpu"}, "enableServiceLinks": False,
                "terminationGracePeriodSeconds": 150, "containers": [{"name": "vllm", "image": "pinned",
                    "args": ["--model", "model"], "env": [{"name": "HF_HOME", "value": "/models"}]}]}
        for replicas in (1, 0):
            with self.subTest(replicas=replicas):
                deployment = Mock(stdout=json.dumps({"spec": {"replicas": replicas, "template": {"spec": spec}}}))
                gpu = {"metadata": {"name": "remaining"}, "status": {"phase": "Running"},
                       "spec": {"containers": [{"resources": {"requests": {"nvidia.com/gpu": "1"}}}]}}
                with patch.object(batch.subprocess, "run", side_effect=[deployment, Mock(stdout=json.dumps({"items": [gpu]}))]):
                    with self.assertRaisesRegex(RuntimeError, "still allocated|GPU pod remains"):
                        batch.baseline(spec)
