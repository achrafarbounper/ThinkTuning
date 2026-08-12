import os
import tempfile
import unittest

from src.utils.config import load_config


class TestConfig(unittest.TestCase):
    def test_load_config_coerces_numeric_strings(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
            tmp.write("max_length: '128'\nlearning_rate: '0.0005'\n")
            tmp_path = tmp.name

        try:
            cfg = load_config(tmp_path)
            self.assertEqual(cfg["max_length"], 128)
            self.assertEqual(cfg["learning_rate"], 0.0005)
        finally:
            os.remove(tmp_path)
