import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from experiments.config import load
from experiments.measured import prepare


class MeasuredTests(unittest.TestCase):
    def test_48_separate_jobs_match_frozen_thresholds_loads_and_seed_plan(self):
        with tempfile.TemporaryDirectory() as folder, patch("builtins.print"):
            output = Path(folder) / "matrix"
            prepare(load("aws/config.example.json"), output)
            plan = json.loads((output / "plan.json").read_text())
            self.assertEqual(len(plan), 48)
            expected = {(m, w, r) for m in "ABCD" for w in ("short", "long", "mixed", "bursty") for r in (1, 2, 3)}
            self.assertEqual({(p["mode"], p["workload"], p["repeat"]) for p in plan}, expected)
            for field in ("job", "results", "bundle"):
                self.assertEqual(len({p[field] for p in plan}), 48)
            for repeat, order in ((1, "ABCD"), (2, "BCDA"), (3, "CDAB")):
                self.assertEqual("".join(p["mode"] for p in plan if p["repeat"] == repeat and p["workload"] == "short"), order)
            for trial in plan:
                docs = list(yaml.safe_load_all((output / trial["bundle"]).read_text()))
                settings = next(d["data"] for d in docs if d["kind"] == "ConfigMap" and "config.json" in d["data"])
                c = json.loads(settings["config.json"])
                self.assertFalse(c["diagnostic_only"])
                self.assertEqual(c["seed_offset"], 1000)
                self.assertEqual(trial["seed"], 1000 + trial["repeat"] * 100)
                self.assertEqual((c["qps_threshold"], c["queue_threshold"]), (0.1, 2))
                self.assertEqual((c["request_rate"], c["duration_seconds"], c["burst_phase_seconds"]), (0.5, 600, 180))
                self.assertEqual(trial["requests"], 540 if trial["workload"] == "bursty" else 300)
                job = next(d for d in docs if d["kind"] == "Job")
                self.assertEqual(job["spec"]["backoffLimit"], 0)
                script = job["spec"]["template"]["spec"]["containers"][0]["args"][0]
                self.assertIn(f'--repeats 1 --repeat-start {trial["repeat"]}', script)
                self.assertIn(f'--modes {trial["mode"]} --workloads {trial["workload"]}', script)
                self.assertTrue(all(d["immutable"] for d in docs if d["kind"] == "ConfigMap"))
                if trial["mode"] == "D":
                    self.assertEqual(c["sky_operator_pod"], "sky-operator")
                    self.assertNotIn("benchmark_url", c)
                    self.assertTrue(any(d["kind"] == "PodMonitor" for d in docs))
                else:
                    deployment = next(d for d in docs if d["kind"] == "Deployment")
                    self.assertEqual(deployment["spec"]["replicas"], 0)
                    self.assertEqual(deployment["spec"]["template"]["spec"]["terminationGracePeriodSeconds"], 150)
                    self.assertEqual(c["benchmark_url"], "http://vllm-proxy:8000")
            with self.assertRaises(FileExistsError):
                prepare(load("aws/config.example.json"), output)

    def test_single_repeat_cli_does_not_replay_earlier_repetitions(self):
        result = subprocess.run(["python3", "-m", "experiments.run", "--config", "config.example.json",
                                 "--modes", "A", "--workloads", "short", "--repeats", "1", "--repeat-start", "3"],
                                check=True, text=True, capture_output=True)
        plan = json.loads(result.stdout)
        self.assertEqual([(p["mode"], p["repeat"]) for p in plan], [("A", 3)])

    def test_seed_offset_rejects_invalid_input(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for value in (-1, True, 0.1, "1000", 2147483648):
                path.write_text(json.dumps({**load("config.example.json"), "seed_offset": value}))
                with self.assertRaisesRegex(ValueError, "seed_offset"):
                    load(path)
