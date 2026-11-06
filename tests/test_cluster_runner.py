import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


class ClusterRunnerTests(unittest.TestCase):
    def test_kubectl_checksum_verified_before_install(self):
        docs = list(yaml.safe_load_all(Path('aws/runner.example.yaml').read_text()))
        job = docs[-1]
        spec = job['spec']['template']['spec']
        self.assertEqual(job['spec']['backoffLimit'], 0)
        self.assertEqual(spec['nodeSelector'], {'centra10-role': 'system'})
        self.assertEqual(spec['serviceAccountName'], 'experiment-runner')
        script = spec['initContainers'][0]['args'][0]
        data = b'test binary'
        for valid in (True, False):
            checksum = hashlib.sha256(data).hexdigest() if valid else 'bad'
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / 'kubectl'
                with patch('urllib.request.urlopen', side_effect=[io.BytesIO(data), io.BytesIO(checksum.encode())]), \
                     patch('platform.machine', return_value='x86_64'), \
                     patch('pathlib.Path', return_value=target):
                    if valid:
                        exec(compile(script, '<kubectl-init>', 'exec'), {})
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'checksum'):
                            exec(compile(script, '<kubectl-init>', 'exec'), {})
                self.assertEqual(target.exists(), valid)
                if valid:
                    self.assertEqual(target.read_bytes(), data)
