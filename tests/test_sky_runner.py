import json
import io
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from experiments import analyze, run, sky_control
from experiments.config import load, queries
from experiments.render import manifests
from experiments.sky_runner import prepare


class SkyRunnerTests(unittest.TestCase):
    def config(self):
        return {**load("aws/config.example.json"), "sky_operator_pod": "sky-operator",
                "sky_service": "centra10-d1", "sky_namespace": "default"}

    def test_bundle_is_one_d_job_with_immutable_code_settings_and_scoped_monitor(self):
        with tempfile.TemporaryDirectory() as folder, patch("builtins.print"):
            output = Path(folder) / "d"
            prepare(self.config(), output)
            docs = list(yaml.safe_load_all((output / "bundle.yaml").read_text()))
            maps = [d for d in docs if d["kind"] == "ConfigMap"]
            self.assertTrue(all(d["immutable"] for d in maps))
            settings = next(d["data"] for d in maps if "config.json" in d["data"])
            self.assertEqual(settings["requirements.txt"], Path("requirements.txt").read_text())
            c = json.loads(settings["config.json"])
            self.assertNotIn("benchmark_url", c)
            self.assertEqual(c["sky_pod_selector"], "centra10-sky-service=centra10-d1")
            job = next(d for d in docs if d["kind"] == "Job")
            self.assertEqual(job["spec"]["backoffLimit"], 0)
            script = job["spec"]["template"]["spec"]["containers"][0]["args"][0]
            self.assertIn("--modes D --workloads short", script)
            self.assertIn("/models/experiment-results/sky-collector-validation-1", script)
            monitor = next(d for d in docs if d["kind"] == "PodMonitor")["spec"]
            task = manifests(c)["sky-autoscale.yaml"][0]
            pod = task["config"]["kubernetes"]["pod_config"]
            self.assertEqual(monitor["selector"]["matchLabels"], pod["metadata"]["labels"])
            self.assertEqual(pod["spec"]["terminationGracePeriodSeconds"], 150)
            self.assertIn("--shutdown-timeout 120", task["run"])
            self.assertEqual(task["resources"]["infra"], "k8s")
            self.assertIn('centra10_service="centra10-d1"', queries(c, "D")["queue"])
            reader = next(d for d in docs if d["kind"] == "Role" and d["metadata"]["namespace"] == "default")
            self.assertEqual(reader["rules"][0]["verbs"], ["get", "list", "watch"])
            with self.assertRaises(FileExistsError):
                prepare(self.config(), output)

    def test_operator_command_uses_arguments_without_shell_interpolation(self):
        c = {**self.config(), "kube_context": "eks-context"}
        argv = sky_control.argv(c, ["serve", "up", "-n", "literal;name"])
        self.assertEqual(argv[:5], ["kubectl", "--context", "eks-context", "-n", "centra10"])
        self.assertIn("/opt/sky/bin/sky", argv)
        self.assertEqual(argv[-1], "literal;name")
        self.assertNotIn("/bin/bash", argv)

    def test_gpu_count_excludes_unscheduled_and_finished_pods(self):
        def pod(phase, scheduled, gpu):
            return {"spec": {"containers": [{"resources": {"requests": {"nvidia.com/gpu": gpu}}}]},
                    "status": {"phase": phase, "conditions": [{"type": "PodScheduled", "status": scheduled}]}}
        self.assertEqual(sky_control.allocated({"items": [pod("Running", "True", "1"),
            pod("Pending", "True", 1), pod("Pending", "False", 1), pod("Succeeded", "True", 1)]}), 2)

    def test_update_waits_for_new_version_and_old_replica_removal(self):
        old = {"active_versions": [1], "replica_info": [{"version": 1, "status": "READY"}]}
        rolling = {"active_versions": [2], "replica_info": [
            {"version": 1, "status": "SHUTTING_DOWN"}, {"version": 2, "status": "READY"}]}
        settled = {"active_versions": [2], "replica_info": [{"version": 2, "status": "READY"}]}
        with patch.object(sky_control, "status", side_effect=[old, rolling, settled]) as status, \
                patch.object(run, "command") as command, patch.object(run.time, "sleep") as sleep:
            run.activate(self.config(), "D", Path("/models/run/manifests"))
            self.assertEqual(status.call_count, 3)
            sleep.assert_called_once()
            self.assertIn("-y", command.call_args.args[0])

    def test_router_readiness_replaces_ssh_pod_readiness_for_cold_starts(self):
        pod = {"metadata": {"name": "replica", "uid": "uid"}, "status": {
            "podIP": "192.0.2.1", "startTime": "1970-01-01T00:01:40Z",
            "conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": "1970-01-01T00:01:45Z"}]}}
        snapshots = [{"pods": {"items": [pod]}}]
        scaling = [{"timestamp": 120, "sky_status": {"replica_info": [{"status": "STARTING", "endpoint": "http://192.0.2.1:8000"}]}},
                   {"timestamp": 250, "sky_status": {"replica_info": [{"status": "READY", "endpoint": "http://192.0.2.1:8000"}]}}]
        self.assertEqual(analyze.cold_starts(snapshots, 90)[0]["cold_start_seconds"], 5)
        self.assertEqual(analyze.cold_starts(snapshots, 90, scaling)[0]["cold_start_seconds"], 150)

    def test_missing_or_wrong_service_is_not_valid_telemetry(self):
        with self.assertRaises(ValueError):
            sky_control.ready(None)
        command = Mock(return_value=Mock(stdout='[{"name":"unrelated"}]'))
        with self.assertRaisesRegex(ValueError, "unexpected services"):
            sky_control.status(self.config(), command)

    def test_owned_partial_launch_is_cleaned_up(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(sky_control, "status", return_value=None), \
                patch.object(run, "release_k8s"), patch.object(run, "kubectl"), \
                patch.object(run, "reset", side_effect=RuntimeError("partial launch")), \
                patch.object(run, "command") as command:
            with self.assertRaisesRegex(RuntimeError, "partial launch"):
                run.run_one(self.config(), "D", "short", 1, Path(folder))
            self.assertTrue(any("down" in call.args[0] for call in command.call_args_list))
            meta = json.loads((Path(folder) / "short_D_run1/run.json").read_text())
            self.assertEqual(meta["status"], "failed")
            self.assertEqual(meta["error"], "partial launch")

    def test_status_script_handles_only_the_expected_no_services_exception(self):
        class ClusterNotUpError(Exception):
            pass

        def execute(args, **kwargs):
            script = args[args.index("/opt/sky/bin/python") + 2]
            output = io.StringIO()
            with redirect_stdout(output), patch.object(sys, "argv", ["status", "centra10-d1"]):
                exec(script, {})
            return Mock(stdout=output.getvalue())

        sky = types.ModuleType("sky")
        sky.get = lambda value: value
        sky.exceptions = types.SimpleNamespace(ClusterNotUpError=ClusterNotUpError)
        sky.serve = types.SimpleNamespace(status=Mock())
        with patch.dict(sys.modules, {"sky": sky}):
            sky.serve.status.side_effect = ClusterNotUpError("No live services.")
            self.assertIsNone(sky_control.status(self.config(), execute))
            for exc in (ClusterNotUpError("Controller failed"), PermissionError("Forbidden")):
                sky.serve.status.side_effect = exc
                with self.assertRaises(type(exc)):
                    sky_control.status(self.config(), execute)

    def test_status_command_failure_preserves_original_stderr(self):
        command = Mock(side_effect=subprocess.CalledProcessError(1, ["kubectl"], stderr="Forbidden: missing exec permission"))
        with self.assertRaisesRegex(RuntimeError, "Forbidden: missing exec permission"):
            sky_control.status(self.config(), command)
