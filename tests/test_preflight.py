import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from experiments.config import load
from experiments.validate import ChartLoader, verify


class PreflightTests(unittest.TestCase):
    def test_version_pins_match_profiles_and_installation(self):
        pins = json.loads(Path("versions.json").read_text())
        for profile in ("config.example.json", "aws/config.example.json"):
            config = load(profile)
            self.assertEqual(config["vllm_version"], pins["vllm"])
            for key in ("model", "model_revision"):
                self.assertEqual(config[key], pins[key])
        requirements = Path("requirements-serving.txt").read_text()
        self.assertIn(f"vllm=={pins['vllm']}", requirements)
        self.assertIn(f"skypilot[kubernetes]=={pins['skypilot']}", requirements)
        eks = yaml.safe_load(Path("aws/eks.example.yaml").read_text())
        self.assertEqual(eks["metadata"]["version"], pins["kubernetes"])
        self.assertFalse(eks["autoModeConfig"]["enabled"])
        guide = Path("aws/README.md").read_text()
        for chart in pins["charts"].values():
            self.assertIn("--version " + chart["version"], guide)

    def test_corrupt_download_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "archive"
            path.write_bytes(b"chart")
            checksum = hashlib.sha256(b"chart").hexdigest()
            verify(path, checksum)
            path.write_bytes(b"corrupted")
            with self.assertRaises(ValueError):
                verify(path, checksum)

    def test_chart_enum_equals_and_safe_loader(self):
        self.assertEqual(yaml.load("enum: [=]", Loader=ChartLoader), {"enum": ["="]})
        with self.assertRaises(yaml.constructor.ConstructorError):
            yaml.load("!!python/object:unknown {}", Loader=ChartLoader)
