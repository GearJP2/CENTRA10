import json
import threading
import tempfile
import unittest
from pathlib import Path

import yaml

from experiments.drain import consume, prepare
from experiments.render import manifests
from experiments.config import load


class DrainTests(unittest.TestCase):
    def test_preparation_keeps_diagnostic_separate_and_one_replica(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'probe'
            prepare(load('config.example.json'), 120, output)
            docs = list(yaml.safe_load_all((output / 'bundle.yaml').read_text()))
            deployment = next(d for d in docs if d['kind'] == 'Deployment')
            self.assertEqual(deployment['spec']['replicas'], 1)
            self.assertEqual(deployment['spec']['template']['spec']['terminationGracePeriodSeconds'], 150)
            job = next(d for d in docs if d['kind'] == 'Job')
            self.assertEqual(job['metadata']['name'], 'centra10-drain-120')
            self.assertIn('experiments.drain', job['spec']['template']['spec']['containers'][0]['args'][0])
            self.assertFalse(any(d['kind'] == 'ScaledObject' for d in docs))
            with self.assertRaises(FileExistsError):
                prepare(load('config.example.json'), 120, output)

    def test_stream_requires_done_usage_and_finish_evidence(self):
        first = threading.Event()
        events = [
            {"choices": [{"text": "hello", "finish_reason": None}]},
            {"choices": [{"text": "", "finish_reason": "length"}]},
            {"choices": [], "usage": {"completion_tokens": 512}},
        ]
        lines = [("data: " + json.dumps(e) + "\n").encode() for e in events]
        result = consume(lines + [b'data: [DONE]\n'], first)
        self.assertTrue(first.is_set())
        self.assertEqual(result, {"done": True, "finish_reason": "length", "completion_tokens": 512})
        self.assertFalse(consume(lines, threading.Event())["done"])
        with self.assertRaises(json.JSONDecodeError):
            consume([b'data: {broken\n'], threading.Event())

    def test_shutdown_is_opt_in_and_grace_exceeds_drain(self):
        config = load('config.example.json')
        spec = manifests(config)['serving.yaml'][0]['spec']['template']['spec']
        self.assertNotIn('terminationGracePeriodSeconds', spec)
        self.assertNotIn('--shutdown-timeout', spec['containers'][0]['args'])
        for timeout in (0, 120):
            spec = manifests({**config, 'shutdown_timeout_seconds': timeout})['serving.yaml'][0]['spec']['template']['spec']
            args = spec['containers'][0]['args']
            self.assertEqual(args[args.index('--shutdown-timeout') + 1], str(timeout))
            self.assertEqual(spec['terminationGracePeriodSeconds'], timeout + 30)
