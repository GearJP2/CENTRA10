"""Real CLI child processes and localhost HTTP, with virtual warm-up time.

Only cluster CLIs and the model computation are simulated. No AWS credentials,
GPU libraries, model downloads, or external network calls are used.
"""
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from experiments import run
from experiments.analyze import summarize
from experiments.config import load


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value, status=200, content_type="application/json"):
        body = value.encode() if isinstance(value, str) else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.path.startswith("/api/v1/query?"):
            self.reply({"error": "unexpected path"}, 404)
            return
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(self.path).query)["query"][0]
        value = 0 if "waiting" in query or query == "queue" else 1
        items = [{"metric": {}, "value": [time.time(), str(value)]}]
        if self.server.scenario == "metrics_fail" and self.server.started:
            items = []
        self.reply({"status": "success", "data": {"resultType": "vector", "result": items}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body.get("stream"):
            self.server.started = True
            if self.server.scenario == "request_fail":
                self.reply({"error": "simulated inference failure"}, 500)
            else:
                self.reply('data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n',
                           content_type="text/event-stream")
        else:
            self.reply({"choices": [{"message": {"content": "warm"}}]})


class RunnerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.scenario, self.server.started = "success", False
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.config = load("aws/config.example.json")
        self.config.update(kube_context="simulated", k8s_url=self.url, prometheus_url=self.url,
            sky_pod_selector="test=simulated", sky_queries={k: k for k in ("replicas", "ready", "queue", "qps")},
            request_rate=2, duration_seconds=5, burst_phase_seconds=5, sample_seconds=5)
        self.config_path = self.folder / "config.json"
        run.save(self.config_path, self.config)
        self.results = self.folder / "results"
        fixture = Path("tests/fake_cli.py").resolve()
        for name in ("kubectl", "sky", "vllm"):
            executable = self.folder / name
            executable.write_text(f"#!{sys.executable}\nexec(compile(open({str(fixture)!r}).read(), {str(fixture)!r}, 'exec'))\n")
            executable.chmod(0o755)
        self.stack.enter_context(patch.dict(os.environ, {
            "PATH": str(self.folder) + os.pathsep + os.environ["PATH"],
            "CENTRA_SIM_DIR": str(self.folder), "CENTRA_SIM_URL": self.url,
            "CENTRA_SIM_SCENARIO": "success", "NO_PROXY": "127.0.0.1,localhost"}))
        # Only the initial stable-warm interval and polling sleeps are accelerated.
        self.ticks = 1
        sleep = time.sleep

        def advance(seconds):
            self.ticks += seconds
            sleep(.02)

        self.stack.enter_context(patch.object(run.time, "monotonic", side_effect=lambda: self.ticks))
        self.stack.enter_context(patch.object(run.time, "sleep", side_effect=advance))
        hub = types.ModuleType("huggingface_hub")
        hub.snapshot_download = lambda *a, **kw: "/synthetic/tokenizer"
        self.stack.enter_context(patch.dict(sys.modules, {"huggingface_hub": hub}))

    def invoke(self, *extra):
        argv = ["run", "--config", str(self.config_path), "--output", str(self.results), "--execute", *extra]
        with patch.object(sys, "argv", argv):
            run.main()

    def commands(self):
        return [json.loads(line) for line in (self.folder / "commands.jsonl").read_text().splitlines()]

    def scenario(self, name):
        self.server.scenario = name
        os.environ["CENTRA_SIM_SCENARIO"] = name

    def test_matrix_and_resume(self):
        self.invoke("--repeats", "1")
        folders = sorted(self.results.glob("*/run.json"))
        self.assertEqual(len(folders), 16)
        for path in folders:
            row, samples, _ = summarize(path.parent)
            self.assertEqual(row["failed"], 0)
            self.assertGreaterEqual(len(samples), 2)
            self.assertGreater(row["gpu_hours"], 0)
        before = len([c for c in self.commands() if c[:3] == ["vllm", "bench", "serve"]])
        self.invoke("--repeats", "1", "--resume")
        after = len([c for c in self.commands() if c[:3] == ["vllm", "bench", "serve"]])
        self.assertEqual(before, after)
        commands = self.commands()
        self.assertEqual(sum(c[:3] == ["sky", "serve", "down"] for c in commands), 4)
        self.assertTrue(any("B.yaml" in " ".join(c) for c in commands))
        self.assertTrue(any("C.yaml" in " ".join(c) for c in commands))

    def test_request_failures_are_slo_misses(self):
        self.scenario("request_fail")
        self.invoke("--modes", "A", "--workloads", "short", "--repeats", "1")
        row, _, _ = summarize(self.results / "short_A_run1")
        self.assertEqual(row["completed"], 0)
        self.assertEqual(row["slo_attainment"], 0)

    def assert_failed_and_released(self):
        meta = json.loads((self.results / "short_B_run1/run.json").read_text())
        self.assertEqual(meta["status"], "failed")
        self.assertTrue(any("--replicas=0" in c for c in self.commands()))

    def test_telemetry_failure_stops_and_cleans_up(self):
        self.scenario("metrics_fail")
        with self.assertRaises(ValueError):
            self.invoke("--modes", "B", "--workloads", "short", "--repeats", "1")
        self.assert_failed_and_released()

    def test_benchmark_exit_stops_and_cleans_up(self):
        self.scenario("benchmark_exit")
        with self.assertRaises(RuntimeError):
            self.invoke("--modes", "B", "--workloads", "short", "--repeats", "1")
        self.assert_failed_and_released()

    def test_timeout_terminates_process(self):
        self.scenario("hang")
        self.config["run_timeout_seconds"] = 10
        run.save(self.config_path, self.config)
        with self.assertRaises(TimeoutError):
            self.invoke("--modes", "B", "--workloads", "short", "--repeats", "1")
        self.assert_failed_and_released()

    def test_interrupt_cleans_up(self):
        sample = run.sample

        def interrupt(c, mode):
            if self.server.started:
                raise KeyboardInterrupt()
            return sample(c, mode)

        with patch.object(run, "sample", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.invoke("--modes", "B", "--workloads", "short", "--repeats", "1")
        self.assert_failed_and_released()

    def test_cleanup_failure_not_resumable(self):
        self.scenario("cleanup_fail")
        with self.assertRaises(Exception):
            self.invoke("--modes", "B", "--workloads", "short", "--repeats", "1")
        self.assert_failed_and_released()
        self.scenario("success")
        with self.assertRaises(FileExistsError):
            self.invoke("--modes", "B", "--workloads", "short", "--repeats", "1", "--resume")

    def test_bad_results_not_marked_complete(self):
        self.scenario("bad_result")
        with self.assertRaises((KeyError, ValueError)):
            self.invoke("--modes", "B", "--workloads", "short", "--repeats", "1")
        self.assert_failed_and_released()
