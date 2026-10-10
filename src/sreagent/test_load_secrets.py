"""Runs test_load_secrets.sh (the load-secrets.sh tests) as part of the unittest suite.

The script is bash, so its tests are too; this wrapper makes CI's
"Test: sreagent" job run them along with everything else.
"""

import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BASH = shutil.which("bash")


@unittest.skipIf(BASH is None, "bash not installed")
class LoadSecretsScriptTest(unittest.TestCase):
    def test_bash_suite(self):
        result = subprocess.run([BASH, "test_load_secrets.sh"], cwd=HERE,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"\d+ passed, 0 failed")


if __name__ == "__main__":
    unittest.main()
