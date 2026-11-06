import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from experiments.calibration import prepare
from experiments.config import load


class CalibrationTests(unittest.TestCase):
    def test_four_separate_candidates_share_lifecycle_and_load(self):
        with tempfile.TemporaryDirectory() as folder, patch('builtins.print'):
            output = Path(folder) / 'prepared'
            prepare(load('config.example.json'), output)
            plan = json.loads((output / 'plan.json').read_text())
            self.assertEqual([p['mode'] for p in plan], ['B', 'C', 'C', 'B'])
            self.assertEqual(len({p['job'] for p in plan}), 4)
            for trial in plan:
                docs = list(yaml.safe_load_all((output / trial['bundle']).read_text()))
                maps = [d for d in docs if d['kind'] == 'ConfigMap']
                self.assertTrue(all(d['immutable'] for d in maps))
                config = json.loads(next(d for d in maps if 'config.json' in d['data'])['data']['config.json'])
                settings = next(d for d in maps if 'config.json' in d['data'])
                self.assertEqual(settings['data']['requirements.txt'], Path('requirements.txt').read_text())
                self.assertEqual((config['request_rate'], config['duration_seconds']), (.5, 600))
                self.assertEqual(config['fixed_replicas'], 1)
                self.assertEqual(config['qps_threshold'], trial['qps_threshold'])
                self.assertEqual(config['queue_threshold'], trial['queue_threshold'])
                deployment = next(d for d in docs if d['kind'] == 'Deployment')
                spec = deployment['spec']['template']['spec']
                self.assertEqual(spec['terminationGracePeriodSeconds'], 150)
                argv = spec['containers'][0]['args']
                self.assertEqual(argv[argv.index('--shutdown-timeout') + 1], '120')
                job = next(d for d in docs if d['kind'] == 'Job')
                command = job['spec']['template']['spec']['containers'][0]['args'][0]
                self.assertIn('--modes ' + trial['mode'], command)
                self.assertIn(trial['results'], command)
                self.assertEqual(job['spec']['backoffLimit'], 0)
            with self.assertRaises(FileExistsError):
                prepare(load('config.example.json'), output)

    def test_retry_uses_new_jobs_configmaps_and_outputs(self):
        with tempfile.TemporaryDirectory() as folder, patch('builtins.print'):
            output = Path(folder) / 'attempt2'
            prepare(load('config.example.json'), output, attempt=2)
            plan = json.loads((output / 'plan.json').read_text())
            for trial in plan:
                self.assertTrue(trial['job'].endswith('-a2'))
                self.assertTrue(trial['results'].endswith('-a2'))
                docs = list(yaml.safe_load_all((output / trial['bundle']).read_text()))
                for doc in docs:
                    if doc['kind'] == 'ConfigMap':
                        self.assertTrue(doc['metadata']['name'].endswith('-a2'))
